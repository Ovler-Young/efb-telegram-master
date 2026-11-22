"""Durable outbound Telegram call queue, stored in SQLite or PostgreSQL."""

from __future__ import annotations

import copy
import fcntl
import inspect
import io
import itertools
import logging
import math
import numbers
import os
import pickle
import pickletools
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from functools import partial
import time
import urllib.request
from concurrent.futures import Executor, Future
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, IO, Iterable, Mapping, Optional, Protocol, TYPE_CHECKING, final
from urllib.parse import unquote, urlsplit

from telegram import (
    Animation, Audio, Document, InputFile, InputMedia, InputMediaAnimation, InputMediaAudio,
    InputMediaDocument, InputMediaLivePhoto, InputMediaPhoto, InputMediaVideo, PhotoSize,
    Sticker, Video, Voice,
)
from telegram.error import NetworkError, RetryAfter, TelegramError
import httpx

from .aggregation_states import (
    ContextEvent, ContextKind, ContextState, context_state, next_context_states,
)
from .delivery_states import (
    FAILED_ATTEMPTS_LIMIT, QUEUED, SENT_PENDING, Event, HoldKind, RowState, backing_off_sql,
    classify, error_detail, failing_sql, format_hold, next_state, ready_sql, waiting_sql,
)
from .transfer_timeout import media_size, transfer_timeout

if TYPE_CHECKING:
    from .aggregate import SourceMember


@dataclass(frozen=True)
class _DirectMediaArgument:
    index: int
    keyword: str
    telegram_type: Optional[type]


QUEUED_OPERATIONS = frozenset({
    "send_message", "send_document", "send_photo", "send_audio",
    "send_video", "send_animation", "send_voice", "send_sticker",
    "send_media_group", "copy_message", "forward_message",
    "edit_message_text", "edit_message_caption", "edit_message_media",
    "delete_message", "edit_message_reply_markup",
    "send_location", "send_venue", "create_forum_topic", "edit_forum_topic",
    "reopen_forum_topic", "set_chat_title", "set_chat_photo", "pin_chat_message",
    "set_chat_description",
})
# These calls create a remote object; retrying after a lost response can duplicate it.
MESSAGE_CREATING_OPERATIONS = frozenset(
    name for name in QUEUED_OPERATIONS
    if name.startswith("send_") or name in {"copy_message", "forward_message", "create_forum_topic"}
)

# Text/caption edits can also create a full-content attachment after their ACK.
EDIT_OPERATIONS = frozenset({"edit_message_text", "edit_message_caption"})
RETAINED_OPERATIONS = MESSAGE_CREATING_OPERATIONS | EDIT_OPERATIONS
# Retry reason of an edit whose result is unknown. Repeating an edit is harmless,
# so it is tried once more instead of being held like a send.
UNCONFIRMED_EDIT_RETRY = "unconfirmed_edit"
# Longest Telegram error description kept with a held row.
HOLD_REASON_LIMIT = 200


def telegram_error_text(error: BaseException) -> str | None:
    """Telegram's error description for a hold, cut to ``HOLD_REASON_LIMIT``.

    Only a Telegram error (the error itself or its cause) has one; any other
    exception text may carry request contents and is never kept.
    """
    for candidate in (error, error.__cause__):
        if isinstance(candidate, TelegramError) and candidate.message:
            return str(candidate.message)[:HOLD_REASON_LIMIT]
    return None


def transport_definitely_not_sent(error: BaseException) -> bool:
    """Only connection establishment/pool failures prove no request was sent."""
    return (
        isinstance(error, NetworkError)
        and isinstance(error.__cause__, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
    )


HISTORY_REPLAY_KEY = "_history_replay"
SUPPLEMENTAL_KEY = "_full_content_attachment"
HISTORY_SOURCE_PREFIX = "__history__:"


def queued_thread_id(
    kwargs: Mapping[str, object], log_context: Optional[bytes] = None
) -> Optional[int]:
    """The forum topic a queued call targets, if any.

    Topic serialization is per topic, not per chat: two topics of one group are
    ordered only within themselves, so they must be allowed to run concurrently.
    Aggregated containers carry their topic inside the log context instead of the
    request kwargs, and both must resolve to the same topic or the same topic gets
    two identities and can be dispatched twice in parallel.
    """
    value = kwargs.get("message_thread_id")
    if value is None:
        api_kwargs = kwargs.get("api_kwargs")
        if isinstance(api_kwargs, Mapping):
            value = api_kwargs.get("message_thread_id")
    if value is None and log_context and log_context[:1] == b"\x03":
        from .queued_log import decode_aggregation
        try:
            key = decode_aggregation(log_context)["key"]
        except Exception:
            return None
        value = key[2] if len(key) > 2 else None
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


REQUIRED_SENDER_OPERATIONS = frozenset({
    "edit_message_text", "edit_message_caption", "edit_message_media", "delete_message",
})
SCHEDULER_KEYS = frozenset({"_send_mode", "_slave_id", "_required_sender_bot_id"})
_DIRECT_MEDIA_ARGUMENTS = {
    "send_animation": _DirectMediaArgument(1, "animation", Animation),
    "send_audio": _DirectMediaArgument(1, "audio", Audio),
    "send_document": _DirectMediaArgument(1, "document", Document),
    "send_photo": _DirectMediaArgument(1, "photo", PhotoSize),
    "send_sticker": _DirectMediaArgument(1, "sticker", Sticker),
    "send_video": _DirectMediaArgument(1, "video", Video),
    "send_voice": _DirectMediaArgument(1, "voice", Voice),
    "set_chat_photo": _DirectMediaArgument(1, "photo", None),
}
_THUMBNAIL_OPERATIONS = frozenset({"send_animation", "send_audio", "send_document", "send_video"})
_KEYWORD_MEDIA_ARGUMENTS = {"send_video": ("cover",)}
_NESTED_MEDIA_ARGUMENTS = {
    "edit_message_media": (0, "media"),
    "send_media_group": (1, "media"),
}
_NESTED_MEDIA_TYPES = {
    InputMediaAnimation: Animation,
    InputMediaAudio: Audio,
    InputMediaDocument: Document,
    InputMediaLivePhoto: PhotoSize,
    InputMediaPhoto: PhotoSize,
    InputMediaVideo: Video,
}
_MEDIA_GROUP_TYPES = frozenset({
    InputMediaAudio, InputMediaDocument, InputMediaLivePhoto, InputMediaPhoto, InputMediaVideo,
})
_INPUT_MEDIA_ATTACHMENT_FIELDS = {
    InputMediaAnimation: ("thumbnail",),
    InputMediaAudio: ("thumbnail",),
    InputMediaDocument: ("thumbnail",),
    InputMediaLivePhoto: ("photo",),
    InputMediaPhoto: (),
    InputMediaVideo: ("thumbnail", "cover"),
}


class QueueError(RuntimeError):
    pass


class QueueEnqueueError(QueueError):
    pass


class SchedulerStoppedError(QueueError):
    pass


class QueuePersistenceError(QueueError):
    pass


class IllegalDeliveryTransitionError(QueuePersistenceError):
    """A row's delivery state does not accept the requested event; nothing was written."""


class IllegalAggregationTransitionError(QueuePersistenceError):
    """An aggregation row's context does not accept the requested event; nothing was written."""

    def __init__(self, row_id: int, kind: object, event: ContextEvent, reason: str = ""):
        self.row_id, self.kind, self.event = row_id, kind, event
        message = f"Aggregation queue row {row_id} of kind {kind!r} does not accept {event.value}; the row was left unchanged."
        super().__init__(f"{message} {reason}" if reason else message)


def check_context_transition(
    row_id: int, context: dict | None, event: ContextEvent, target: dict | None = None, *, reason: str = "",
) -> ContextState:
    """Check ``event`` (and the resulting ``target`` context, when given) before a write.

    Returns the current state; raises ``IllegalAggregationTransitionError`` when
    the transition table does not allow it. A row without an aggregation
    context (None) is not an aggregation row: only its delivery state applies.
    """
    if context is None:
        return ContextState.ABSENT
    state = context_state(context)
    allowed = next_context_states(state, event)
    kind = context.get("kind")
    if not allowed:
        raise IllegalAggregationTransitionError(row_id, kind, event, reason)
    if target is not None and context_state(target) not in allowed:
        raise IllegalAggregationTransitionError(
            row_id, kind, event, f"It cannot become {context_state(target).value}.")
    return state


class OversizedQueuedPayloadError(QueuePersistenceError):
    """A queued row exceeds the replay memory budget and remains retained."""


class DeliveryUncertainError(QueueError):
    """Remote send may have succeeded; preserve the row and require confirmation."""
    pass


class InvalidTelegramResponseError(QueueError):
    """A send returned no usable primary receipt; the delivery must be held."""


class InvalidQueuedPayloadError(QueueError):
    pass


class MissingQueuedExternalMediaError(InvalidQueuedPayloadError):
    pass


class RequiredSenderUnavailableError(QueueError):
    pass


class ExecutorSubmitError(QueueError):
    pass


def _fresh_exception(error: BaseException) -> BaseException:
    try:
        return type(error)(*error.args)
    except Exception:
        return RuntimeError(str(error))


@dataclass(frozen=True)
class QueueRequest:
    operation: str
    args: tuple
    kwargs: dict
    log_context: Optional[bytes] = None
    cleanup_files: tuple[str, ...] = ()
    predecessor_id: Optional[int] = None


@dataclass(frozen=True)
class QueuedDeliveryResult:
    """An accepted primary response with an attachment still to enqueue."""

    result: object
    supplement: QueueRequest
    receipt: bytes


@dataclass(frozen=True)
class QueuedCall:
    id: int
    priority: int
    telegram_chat_id: int
    operation: str
    payload: bytes
    slave_id: Optional[str]
    required_sender_bot_id: Optional[str]
    created_at: float
    log_context: Optional[bytes]
    delivery_state: str
    completion_receipt: Optional[bytes]
    stored_bytes: int = 0
    retry_after: float = 0
    predecessor_id: Optional[int] = None
    thread_id: Optional[int] = None
    waiting_rows: int = 0


@dataclass(frozen=True)
class SenderSelection:
    sender: object
    sender_bot_id: Optional[str]


@dataclass(frozen=True)
class SenderSelectionResult:
    selection: Optional[SenderSelection] = None
    retry_at: Optional[float] = None
    terminal_error_class: Optional[str] = None


class QueuedCompletionDecision(Protocol):
    @property
    def kind(self) -> str:
        ...

    @property
    def retry_at(self) -> Optional[float]:
        ...

    @property
    def retry_reason(self) -> Optional[str]:
        ...


@dataclass
class SubmittedCall:
    row: QueuedCall
    selection: SenderSelection
    future: Future
    dispatched_at: float
    closeables: tuple[object, ...] = ()
    completion_retry_at: float = 0
    completion_attempts: int = 0


@dataclass(frozen=True)
class BlockingMediaRetry:
    """An in-memory retry for one blocking media edit.

    Blocking rows are intentionally removed before submission, so this state
    must never survive a scheduler restart.
    """

    row: QueuedCall
    selection: SenderSelection
    retry_at: float
    deadline: float
    error: RetryAfter


def _restore_inline_media(
    content: bytes,
    filename: Optional[str],
    input_file: bool = False,
    attach_name: Optional[str] = None,
    mimetype: Optional[str] = None,
) -> object:
    if input_file:
        restored = InputFile(content, filename=filename, attach=attach_name is not None)
        restored.attach_name = attach_name
        if mimetype is not None:
            restored.mimetype = mimetype
        return restored
    stream = io.BytesIO(content)
    if filename is not None:
        stream.name = filename
    return stream


@dataclass(frozen=True)
class _InlineMediaSnapshot:
    content: bytes
    filename: Optional[str]
    input_file: bool = False
    attach_name: Optional[str] = None
    mimetype: Optional[str] = None

    def __reduce__(self):
        return _restore_inline_media, (
            self.content, self.filename, self.input_file, self.attach_name, self.mimetype
        )


@dataclass(frozen=True)
class _StoredMediaSnapshot:
    """Durable media reference kept outside the SQLite payload BLOB."""

    storage_name: Optional[str]
    filename: Optional[str]
    external_uri: Optional[str] = None
    cleanup_external: bool = False
    input_file: bool = False
    attach_name: Optional[str] = None
    mimetype: Optional[str] = None


@dataclass(frozen=True)
class _LegacyInlineBlob:
    """A v1 pickle byte string copied directly from SQLite into a sidecar."""

    storage_name: str


class _LegacyBytesIO:
    """Receive a legacy BytesIO pickle state without materializing its bytes."""

    def __setstate__(self, state) -> None:
        try:
            content = state[0]
        except (IndexError, TypeError) as error:
            raise InvalidQueuedPayloadError("Legacy BytesIO has an invalid state.") from error
        if not isinstance(content, _LegacyInlineBlob):
            raise InvalidQueuedPayloadError("Legacy BytesIO was not recovered as a sidecar.")
        attributes = state[2] if len(state) > 2 else None
        name = attributes.get("name") if isinstance(attributes, dict) else None
        filename = Path(name).name if isinstance(name, (str, Path)) else None
        self.snapshot = _StoredMediaSnapshot(content.storage_name, filename)


def _restore_legacy_inline_blob(storage_name: str) -> _LegacyInlineBlob:
    return _LegacyInlineBlob(storage_name)


class _LegacyRecoveryUnpickler(pickle.Unpickler):
    """Map v1 BytesIO objects to a state holder while decoding a filtered pickle."""

    def find_class(self, module: str, name: str):
        if module == "_io" and name == "BytesIO":
            return _LegacyBytesIO
        return super().find_class(module, name)


# Python 3.10 can embed SQLite without exporting its C symbols. This helper
# loads SQLite only in a separate process, so closing its files cannot release
# the queue process's transaction locks. stdout carries a length then BLOB bytes.
_SQLITE_BLOB_HELPER = r"""
import ctypes
import ctypes.util
import os
import sys

name = ctypes.util.find_library("sqlite3")
if not name:
    raise RuntimeError("SQLite incremental BLOB access is unavailable")
library = ctypes.CDLL(name)
library.sqlite3_open_v2.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int, ctypes.c_char_p]
library.sqlite3_close.argtypes = [ctypes.c_void_p]
library.sqlite3_busy_timeout.argtypes = [ctypes.c_void_p, ctypes.c_int]
library.sqlite3_blob_open.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_longlong, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
library.sqlite3_blob_bytes.argtypes = [ctypes.c_void_p]
library.sqlite3_blob_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
library.sqlite3_blob_close.argtypes = [ctypes.c_void_p]
database = ctypes.c_void_p()
blob = ctypes.c_void_p()
try:
    if library.sqlite3_open_v2(os.fsencode(sys.argv[1]), ctypes.byref(database), 1, None):
        raise RuntimeError("Cannot open queue")
    library.sqlite3_busy_timeout(database, 5000)
    if library.sqlite3_blob_open(database, b"main", b"outbound_queue", b"payload", int(sys.argv[2]), 0, ctypes.byref(blob)):
        raise RuntimeError("Cannot open queued BLOB")
    length = library.sqlite3_blob_bytes(blob)
    sys.stdout.buffer.write(length.to_bytes(8, "little"))
    sys.stdout.buffer.flush()
    buffer = ctypes.create_string_buffer(64 * 1024)
    for offset in range(0, length, len(buffer)):
        size = min(len(buffer), length - offset)
        if library.sqlite3_blob_read(blob, buffer, size, offset):
            raise RuntimeError("Cannot read queued BLOB")
        sys.stdout.buffer.write(buffer.raw[:size])
    sys.stdout.buffer.flush()
finally:
    if blob.value:
        library.sqlite3_blob_close(blob)
    if database.value:
        library.sqlite3_close(database)
"""


class _SQLiteBlobReader:
    """Read one SQLite BLOB incrementally while preserving queue writer locks."""

    def __init__(self, database: Path, row_id: int):
        self._offset = 0
        self._connection: Optional[sqlite3.Connection] = None
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._stream: Optional[IO[bytes]] = None
        try:
            if hasattr(sqlite3.Connection, "blobopen"):
                self._connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
                blob = getattr(self._connection, "blobopen")("outbound_queue", "payload", row_id, readonly=True)
                self._stream = blob
                self.length = len(blob)
            else:
                self._process = subprocess.Popen(
                    [sys.executable, "-I", "-S", "-c", _SQLITE_BLOB_HELPER, str(database), str(row_id)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                self._stream = self._process.stdout
                assert self._stream is not None
                header = self._stream.read(8)
                if len(header) != 8:
                    raise QueuePersistenceError("Unable to open queued BLOB in the recovery process.")
                self.length = int.from_bytes(header, "little")
        except BaseException:
            self.close()
            raise

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self.length - self._offset
        size = min(size, self.length - self._offset)
        if size <= 0:
            return b""
        assert self._stream is not None
        value = self._stream.read(size)
        if len(value) != size:
            raise QueuePersistenceError("Unable to read queued BLOB incrementally.")
        self._offset += size
        return value

    def readline(self, size: int = -1) -> bytes:
        # Pickle only uses this for old text protocol payloads, which v1 never wrote.
        return self.read(size)

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._process is not None:
                if self._process.poll() is None:
                    self._process.terminate()
                self._process.wait()
                self._process = None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class _PostgresqlConnection:
    """The part of ``sqlite3.Connection`` the queue uses, on one PostgreSQL session.

    The session runs in autocommit, so a read never leaves a transaction open.
    ``BEGIN`` with ``commit()``/``rollback()``, or ``with connection:``, bound
    each write transaction as they do on SQLite. Queue SQL uses ``?``
    placeholders and contains no literal ``?`` or ``%``.
    """

    def __init__(self, connection):
        import psycopg2
        import psycopg2.extensions

        connection.autocommit = True
        # BYTEA arrives as memoryview; queued payloads, contexts and receipts are bytes.
        as_bytes = psycopg2.extensions.new_type(
            psycopg2.BINARY.values, "ETM_QUEUE_BYTES",
            lambda value, cursor: None if value is None else bytes(psycopg2.BINARY(value, cursor)),
        )
        psycopg2.extensions.register_type(as_bytes, connection)
        self._connection = connection
        self._idle = psycopg2.extensions.TRANSACTION_STATUS_IDLE

    @staticmethod
    def _translate(sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql: str, parameters: Iterable[object] = ()):
        if sql == "BEGIN IMMEDIATE":
            # Writers are serialised by the queue owner; rows are locked explicitly.
            sql = "BEGIN"
        values = tuple(parameters)
        cursor = self._connection.cursor()
        if values:
            cursor.execute(self._translate(sql), values)
        else:
            cursor.execute(sql)
        return cursor

    def executemany(self, sql: str, parameters: Iterable[Iterable[object]]):
        cursor = self._connection.cursor()
        cursor.executemany(self._translate(sql), (tuple(values) for values in parameters))
        return cursor

    def _in_transaction(self) -> bool:
        return self._connection.get_transaction_status() != self._idle

    def commit(self) -> None:
        if self._in_transaction():
            self._connection.cursor().execute("COMMIT")

    def rollback(self) -> None:
        if self._in_transaction():
            self._connection.cursor().execute("ROLLBACK")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self):
        if not self._in_transaction():
            self._connection.cursor().execute("BEGIN")
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


class _PostgresqlBlobReader:
    """Read one queued payload in bounded chunks on the queue's own session.

    The payload column is stored uncompressed, so each chunk reads only its own
    part of the value.
    """

    CHUNK_BYTES = 1024 * 1024

    def __init__(self, connection: _PostgresqlConnection, row_id: int):
        self._connection = connection
        self._row_id = row_id
        self._offset = 0
        self._buffer = b""
        self._position = 0
        row = connection.execute(
            "SELECT octet_length(payload) FROM outbound_queue WHERE id = ?", (row_id,)
        ).fetchone()
        if row is None:
            raise QueuePersistenceError(f"Queued row {row_id} has no payload to read.")
        self.length = int(row[0])

    def read(self, size: int = -1) -> bytes:
        remaining = self.length - self._offset
        size = remaining if size < 0 else min(size, remaining)
        if size <= 0:
            return b""
        buffered = len(self._buffer) - self._position
        if buffered < size:
            wanted = min(max(size - buffered, self.CHUNK_BYTES), remaining - buffered)
            row = self._connection.execute(
                "SELECT substring(payload FROM ? FOR ?) FROM outbound_queue WHERE id = ?",
                (self._offset + buffered + 1, wanted, self._row_id),
            ).fetchone()
            chunk = row[0] if row is not None and row[0] is not None else b""
            self._buffer = self._buffer[self._position:] + chunk
            self._position = 0
            if len(self._buffer) < size:
                raise QueuePersistenceError("Unable to read queued BLOB incrementally.")
        value = self._buffer[self._position:self._position + size]
        self._position += size
        self._offset += size
        return value

    def readline(self, size: int = -1) -> bytes:
        # Pickle only uses this for old text protocol payloads, which v1 never wrote.
        return self.read(size)

    def close(self) -> None:
        self._buffer = b""
        self._position = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()


_BlobReader = _SQLiteBlobReader | _PostgresqlBlobReader
_QueueConnection = sqlite3.Connection | _PostgresqlConnection


class _BlobSlice:
    def __init__(self, source: _BlobReader, length: int):
        self.source = source
        self.remaining = length

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = self.remaining
        value = self.source.read(min(size, self.remaining))
        self.remaining -= len(value)
        return value


class _NamedMediaFile(io.BufferedReader):
    def __init__(self, raw, filename: Optional[str]):
        super().__init__(raw)
        self._filename = filename

    @property
    def name(self):
        if self._filename is None:
            raise AttributeError("Unnamed media stream")
        return self._filename


class QueueAdapter(Protocol):
    @staticmethod
    def _rewrite_queued_chat_id(
        operation: str, args: tuple, kwargs: dict, new_chat_id: int
    ) -> tuple[tuple, dict]:
        ...

    def _queue_operation(self, operation: str) -> Callable[..., object]:
        ...

    def select_sender(self, row: QueuedCall, now: float) -> SenderSelectionResult:
        ...

    def acquire_sender_limits(self, selection: SenderSelection, telegram_chat_id: int) -> bool:
        ...

    def reopen_aggregation(self, row: QueuedCall) -> None:
        ...

    def finish_discarded_delivery(self, row: QueuedCall) -> None:
        ...

    def execute_queued_call(
        self, row: QueuedCall, args: tuple, kwargs: dict, selection: SenderSelection
    ) -> object:
        ...

    def record_queued_failure(
        self, row: QueuedCall, error: BaseException, selection: SenderSelection
    ) -> QueuedCompletionDecision:
        ...

    def record_queued_retry_after(
        self, row: QueuedCall, error: RetryAfter, selection: SenderSelection
    ) -> None:
        ...

    def record_queued_success(
        self, row: QueuedCall, result: object, selection: SenderSelection
    ) -> QueuedCompletionDecision:
        ...

    def reconcile_queued_delivery(self, row: QueuedCall) -> bool:
        ...

    def queued_aggregation_deadline(self, row: QueuedCall) -> float | None:
        """When ``row`` may be sent; None means now.

        ``math.inf`` means it waits for its predecessor, which keeps failing, to
        reach Telegram first.
        """
        ...

    def materialize_queued_aggregation(
        self, row: QueuedCall, selection: SenderSelection
    ) -> QueuedCall:
        ...

    def encode_queued_completion_receipt(
        self, result: object, selection: SenderSelection
    ) -> bytes:
        ...


class QueueMetrics(Protocol):
    def record_aggregation_source(self) -> None:
        ...

    def record_aggregation_container(self) -> None:
        ...

    def record_aggregation_rpc(self, operation: str, purpose: str, sender_kind: str) -> None:
        ...

    def record_aggregation_batch(self, members: int) -> None:
        ...

    def record_aggregation_confirmation(self, seconds: float) -> None:
        ...

    def record_aggregation_payload(self, size_bytes: int) -> None:
        ...

    def record_enqueued(self, priority: int, operation: str) -> None:
        ...

    def set_queue_depth(self, depth: int) -> None:
        ...

    def record_removal(self, priority: int, operation: str, outcome: str, residence_seconds: float) -> None:
        ...

    def record_dequeued(self, priority: int, operation: str) -> None:
        ...

    def record_dispatch_failure(self, priority: int, operation: str) -> None:
        ...

    def increment_in_flight(self, priority: int, operation: str, sender_kind: str) -> None:
        ...

    def decrement_in_flight(self, priority: int, operation: str, sender_kind: str) -> None:
        ...

    def record_completion(self, priority: int, operation: str, sender_kind: str, outcome: str) -> None:
        ...

    def record_queue_dispatch(self, outcome: str) -> None:
        ...

    def record_queue_wait(self, priority: int, operation: str, seconds: float) -> None:
        ...

    def record_executor_attempt_duration(
        self, priority: int, operation: str, outcome: str, seconds: float
    ) -> None:
        ...

    def record_queue_lifetime(self, priority: int, operation: str, outcome: str, seconds: float) -> None:
        ...

    def record_retry(self, priority: int, operation: str, reason: str) -> None:
        ...

    def record_failure(self, priority: int, operation: str, stage: str) -> None:
        ...


_QUEUE_INDEXES = (
    ("CREATE INDEX IF NOT EXISTS outbound_queue_destination_priority_id "
     "ON outbound_queue (telegram_chat_id, priority DESC, id ASC)"),
    ("CREATE INDEX IF NOT EXISTS outbound_queue_state_destination_priority_id "
     "ON outbound_queue (delivery_state, telegram_chat_id, priority DESC, id)"),
    ("CREATE INDEX IF NOT EXISTS outbound_queue_reconciliation_due "
     "ON outbound_queue (delivery_state, reconcile_after, id)"),
    # A dispatch pass elects one head per destination, so it reads every
    # waiting row's state, hold, destination, topic, priority and
    # predecessor. Covering them halves that scan on a large backlog.
    ("CREATE INDEX IF NOT EXISTS outbound_queue_ready_heads "
     "ON outbound_queue (delivery_state, delivery_hold, telegram_chat_id, thread_id, "
     "priority, id, predecessor_id)"),
)
_QUEUE_TABLES = ("outbound_queue", "history_ownership", "source_cancellations")
# Session lock key base for the PostgreSQL queue owner; the schema's OID is
# added, so queues in different schemas of one database do not exclude each other.
_POSTGRESQL_OWNER_LOCK = 0x4554_4D51_0000_0000
# Written next to the SQLite queue once its rows are in PostgreSQL.
POSTGRESQL_QUEUE_MARKER = ".outbound-queue-postgresql.json"


def create_postgresql_queue_tables(execute: Callable[[str], object]) -> None:
    """Create the outbound queue tables in the current PostgreSQL schema.

    Runs in the database startup transaction; the queue never creates them
    itself. Columns match the SQLite queue after all of its upgrades.
    Telegram chat ids need BIGINT, and timestamps need DOUBLE PRECISION because
    guarded updates compare ``created_at`` exactly. A new queued operation needs
    this CHECK replaced on existing PostgreSQL tables.
    """
    operations = ", ".join(repr(name) for name in sorted(QUEUED_OPERATIONS))
    execute(
        "CREATE TABLE IF NOT EXISTS outbound_queue ("
        "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
        "priority INTEGER NOT NULL CHECK (priority IN (0, 1)), "
        "telegram_chat_id BIGINT NOT NULL, "
        f"operation TEXT NOT NULL CHECK (operation IN ({operations})), "
        "payload BYTEA NOT NULL, slave_id TEXT NULL, "
        "required_sender_bot_id TEXT NULL, created_at DOUBLE PRECISION NOT NULL, "
        "log_context BYTEA NULL, "
        "delivery_state TEXT NOT NULL DEFAULT 'queued' "
        "CHECK (delivery_state IN ('queued', 'sent_pending')), "
        "completion_receipt BYTEA NULL, "
        "reconcile_after DOUBLE PRECISION NOT NULL DEFAULT 0, "
        "reconcile_attempts INTEGER NOT NULL DEFAULT 0, "
        "delivery_hold TEXT NULL, attempt_sender_bot_id TEXT NULL, "
        "attempt_started_at DOUBLE PRECISION NULL, predecessor_id BIGINT NULL, "
        "thread_id BIGINT NULL, hold_reason TEXT NULL, unconfirmed_at DOUBLE PRECISION NULL)"
    )
    # Tables created by an earlier version lack these columns. Adding a column
    # takes the same exclusive lock as the ALTER below, so only do it once.
    for name, kind in (("hold_reason", "TEXT"), ("unconfirmed_at", "DOUBLE PRECISION")):
        execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_attribute "
            f"WHERE attrelid = 'outbound_queue'::regclass AND attname = '{name}' AND NOT attisdropped) THEN "
            f"ALTER TABLE outbound_queue ADD COLUMN {name} {kind} NULL; END IF; END $$"
        )
    # Payloads are read in chunks (legacy recovery, media sweeps, large
    # deletes). Compressed values would be decompressed whole for every chunk.
    # The ALTER needs an ACCESS EXCLUSIVE lock, which waits for any reader such
    # as pg_dump and queues the running owner's writes behind it; run it only
    # once, on a table that does not have the setting yet.
    execute(
        "DO $$ BEGIN IF (SELECT attstorage FROM pg_attribute "
        "WHERE attrelid = 'outbound_queue'::regclass AND attname = 'payload') <> 'e' THEN "
        "ALTER TABLE outbound_queue ALTER COLUMN payload SET STORAGE EXTERNAL; END IF; END $$"
    )
    execute("CREATE TABLE IF NOT EXISTS history_ownership (entry_key TEXT PRIMARY KEY)")
    execute(
        "CREATE TABLE IF NOT EXISTS source_cancellations ("
        "origin_uid TEXT NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL, "
        "log_context BYTEA NOT NULL, PRIMARY KEY (origin_uid, source_id))"
    )
    for statement in _QUEUE_INDEXES:
        execute(statement)


class OutboundQueue:
    """Own the queue connection, codec, and transactional row mutations."""

    filename = "outbound-queue.sqlite3"
    # Protect restart from already-inflated historical rows before reading their
    # BLOBs. Oversized rows are retained and reported, never silently discarded.
    MAX_REPLAY_BYTES = 128 * 1024 * 1024
    # The orphan sweep opens one blob reader per queued row, so its cost scales
    # with the backlog rather than with the number of stored media files. It is
    # housekeeping only: skip it on a large queue and keep the sidecars.
    MAX_ORPHAN_SCAN_ROWS = 2000
    # Repairing a legacy row's topic means decoding its payload; never pull an
    # oversized row into memory just to learn it. Such rows keep their topic NULL
    # until their own recovery path re-encodes them.
    MAX_TOPIC_REPAIR_BYTES = 1024 * 1024
    # Deleting a row inspects its payload to release sidecars; above this size the
    # payload is streamed instead of fetched.
    MAX_INSPECT_BYTES = 1024 * 1024
    # Legacy v1 rows may contain one complete media file inline. Recover one
    # such row at a time, rewrite it to v2 sidecar storage, then let normal
    # replay budgeting apply to the compact row.

    def __init__(self, channel_data_path: Path | str, metrics: Optional[QueueMetrics] = None,
                 transfer_min_mbps: Optional[float] = None, database: Mapping | None = None):
        """``database`` is ETM's ``database`` setting; the queue uses the same backend."""
        backend = (database or {}).get("type", "sqlite")
        if backend not in ("sqlite", "postgresql"):
            raise ValueError(f"Unknown database type: {backend!r}")
        self._postgresql_config = database if backend == "postgresql" else None
        self._database_errors: tuple[type[BaseException], ...] = (sqlite3.Error,)
        self._empty_blob = "X''" if self._postgresql_config is None else "''::bytea"
        self._lock_row_sql = "" if self._postgresql_config is None else " FOR UPDATE"
        self.transfer_min_mbps = transfer_min_mbps
        self.path = Path(channel_data_path) / self.filename
        self.media_dir = Path(channel_data_path) / "outbound-media"
        self._lock = threading.RLock()
        self._connection: _QueueConnection | None = None
        self._ownership_file: Optional[IO[bytes]] = None
        self.metrics = metrics
        self.waiters: dict[int, Future] = {}
        # Every mutation that can move a destination's head records it here, so a
        # dispatch pass can re-elect that destination alone instead of the queue.
        self._dirty_destinations: set[tuple[int, Optional[int]]] = set()
        self._open()

    def _open(self) -> None:
        connection: _QueueConnection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Own media preparation, publication and startup reclamation before
            # acquiring database locks. Keep this file in place across owners.
            self._ownership_file = (self.path.parent / ".outbound-queue.lock").open("a+b")
            try:
                fcntl.flock(self._ownership_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise QueuePersistenceError("Outbound queue is already in use by another owner.") from None
            self.media_dir.mkdir(parents=True, exist_ok=True)
            if self._postgresql_config is not None:
                connection = self._open_postgresql(self._postgresql_config)
            else:
                if (self.path.parent / POSTGRESQL_QUEUE_MARKER).exists():
                    raise QueuePersistenceError(
                        "This outbound queue was moved to PostgreSQL; refusing to resume its retained "
                        "SQLite copy, which would send its rows again."
                    )
                connection = self._open_sqlite()
            self._connection = connection
            self._cleanup_orphan_media()
            repaired = self.repair_legacy_thread_ids()
            if repaired:
                logging.getLogger(__name__).info(
                    "Recovered the forum topic of %d queued row(s) that stored it only in the "
                    "request; those rows no longer share the chat's main lane.", repaired,
                )
            self.refresh_depth()
        except BaseException:
            try:
                if connection is not None:
                    try:
                        connection.rollback()
                    finally:
                        connection.close()
            finally:
                self._connection = None
                if self._ownership_file is not None:
                    self._ownership_file.close()
                    self._ownership_file = None
            raise

    def _open_sqlite(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN")
            operations = ", ".join(repr(name) for name in sorted(QUEUED_OPERATIONS))
            connection.execute(
                "CREATE TABLE IF NOT EXISTS outbound_queue ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "priority INTEGER NOT NULL CHECK (priority IN (0, 1)), "
                "telegram_chat_id INTEGER NOT NULL, "
                f"operation TEXT NOT NULL CHECK (operation IN ({operations})), "
                "payload BLOB NOT NULL, slave_id TEXT NULL, "
                "required_sender_bot_id TEXT NULL, created_at REAL NOT NULL, "
                "log_context BLOB NULL, "
                "delivery_state TEXT NOT NULL DEFAULT 'queued' "
                "CHECK (delivery_state IN ('queued', 'sent_pending')), "
                "completion_receipt BLOB NULL)"
            )
            self._migrate_schema(connection)
            self._release_interrupted_attempts(connection)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS history_ownership (entry_key TEXT PRIMARY KEY)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS source_cancellations ("
                "origin_uid TEXT NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL, "
                "log_context BLOB NOT NULL, PRIMARY KEY (origin_uid, source_id))"
            )
            for statement in _QUEUE_INDEXES:
                connection.execute(statement)
            connection.commit()
        except BaseException:
            try:
                connection.rollback()
            finally:
                connection.close()
            raise
        return connection

    def _open_postgresql(self, config: Mapping) -> _PostgresqlConnection:
        import psycopg2

        from .db_runtime import postgresql_connection

        self._database_errors = (sqlite3.Error, psycopg2.Error)
        connection = _PostgresqlConnection(postgresql_connection(config))
        try:
            # Fail a blocked statement as SQLite's busy timeout does, instead of waiting forever.
            connection.execute("SET lock_timeout = '5s'")
            present = {row[0] for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
                "AND tablename IN (" + ", ".join("?" for _ in _QUEUE_TABLES) + ")", _QUEUE_TABLES,
            )}
            missing = [name for name in _QUEUE_TABLES if name not in present]
            if missing:
                raise QueuePersistenceError(
                    "Outbound queue table(s) " + ", ".join(missing) + " are missing from the "
                    "PostgreSQL schema. ETM's database startup creates them; the queue never "
                    "recreates a missing table, which could drop pending sends."
                )
            # Exclude a second owner using the same schema from another directory
            # or host. The lock lasts as long as this session.
            owned = connection.execute(
                "SELECT pg_try_advisory_lock(?::bigint + oid::bigint) FROM pg_namespace "
                "WHERE nspname = current_schema()", (_POSTGRESQL_OWNER_LOCK,),
            ).fetchone()
            if not owned or not owned[0]:
                raise QueuePersistenceError("Outbound queue is already in use by another owner.")
            with connection:
                self._repair_history_sender_holds(connection)
                self._release_interrupted_attempts(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def repair_legacy_thread_ids(self, batch: int = 512) -> int:
        """Fill ``thread_id`` for rows that kept their topic only inside the request.

        Rows written before the topic column existed carry the topic in the payload
        kwargs, and an aggregated container carries it in its context key. Without
        this they all look like the chat's main topic and share one lane, so a topic
        runs at half speed next to its own backlog and its two lanes can be
        dispatched in parallel. Idempotent: only ``NULL`` topics are written.
        """
        repaired = 0
        after_id = 0
        while True:
            with self._lock:
                rows = self.connection.execute(
                    "SELECT id, payload, log_context FROM outbound_queue "
                    "WHERE id > ? AND delivery_state = 'queued' AND thread_id IS NULL "
                    "AND length(payload) <= ? "
                    f"AND length(COALESCE(log_context, {self._empty_blob})) <= ? "
                    "ORDER BY id LIMIT ?",
                    (after_id, self.MAX_TOPIC_REPAIR_BYTES, self.MAX_TOPIC_REPAIR_BYTES, batch),
                ).fetchall()
            if not rows:
                return repaired
            updates: list[tuple[int, int]] = []
            for row_id, payload, log_context in rows:
                after_id = int(row_id)
                if not payload or bytes(payload[:1]) != b"\x02":
                    # Legacy inline rows are re-encoded by their own recovery path.
                    continue
                try:
                    _args, kwargs = self.decode_payload_raw(bytes(payload))
                except InvalidQueuedPayloadError:
                    continue
                thread_id = queued_thread_id(kwargs, log_context)
                if thread_id is not None:
                    updates.append((thread_id, int(row_id)))
            if updates:
                with self._lock, self.connection:
                    self.connection.executemany(
                        "UPDATE outbound_queue SET thread_id = ? WHERE id = ? AND thread_id IS NULL",
                        updates,
                    )
                repaired += len(updates)

    @staticmethod
    def _migrate_schema(connection: sqlite3.Connection) -> None:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(outbound_queue)")}
        if "log_context" not in columns:
            connection.execute("ALTER TABLE outbound_queue ADD COLUMN log_context BLOB NULL")
        if "delivery_state" not in columns:
            connection.execute(
                "ALTER TABLE outbound_queue ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'queued'"
            )
        if "completion_receipt" not in columns:
            connection.execute("ALTER TABLE outbound_queue ADD COLUMN completion_receipt BLOB NULL")
        if "reconcile_after" not in columns:
            connection.execute(
                "ALTER TABLE outbound_queue ADD COLUMN reconcile_after REAL NOT NULL DEFAULT 0"
            )
        if "reconcile_attempts" not in columns:
            connection.execute(
                "ALTER TABLE outbound_queue ADD COLUMN reconcile_attempts INTEGER NOT NULL DEFAULT 0"
            )
        # Additive metadata: do not rebuild the large queue or change its legacy CHECK.
        for name, kind in (("delivery_hold", "TEXT"), ("attempt_sender_bot_id", "TEXT"),
                           ("attempt_started_at", "REAL"), ("predecessor_id", "INTEGER"),
                           ("thread_id", "INTEGER"), ("hold_reason", "TEXT"),
                           ("unconfirmed_at", "REAL")):
            if name not in columns:
                connection.execute(f"ALTER TABLE outbound_queue ADD COLUMN {name} {kind} NULL")
        OutboundQueue._repair_history_sender_holds(connection)

    @staticmethod
    def _repair_history_sender_holds(connection: _QueueConnection) -> None:
        # Undo only pre-submission holds caused by the removed history sender
        # constraint. Attempted/uncertain deliveries must never be auto-replayed.
        repaired_hold = format_hold(HoldKind.HISTORY_FAILED, RequiredSenderUnavailableError.__name__)
        connection.execute(
            "UPDATE outbound_queue SET delivery_hold=NULL "
            "WHERE substr(slave_id, 1, ?) = ? AND delivery_state=? "
            "AND delivery_hold=? AND attempt_started_at IS NULL",
            (len(HISTORY_SOURCE_PREFIX), HISTORY_SOURCE_PREFIX, QUEUED, repaired_hold),
        )

    @staticmethod
    def _release_interrupted_attempts(connection: _QueueConnection) -> None:
        """A send left in flight by a restart stops deferring the rows after it.

        Nothing is in flight while the queue opens, so the outcome of such a send
        is unknown and it is never replayed. It keeps its hold for /confirm_send;
        counting it as failed often enough lets later rows go out meanwhile.
        """
        interrupted = connection.execute(
            "SELECT id, telegram_chat_id FROM outbound_queue "
            "WHERE delivery_state=? AND delivery_hold=? AND reconcile_attempts < ? ORDER BY id",
            (QUEUED, HoldKind.IN_FLIGHT.value, FAILED_ATTEMPTS_LIMIT),
        ).fetchall()
        if not interrupted:
            return
        connection.execute(
            "UPDATE outbound_queue SET reconcile_attempts=? "
            "WHERE delivery_state=? AND delivery_hold=? AND reconcile_attempts < ?",
            (FAILED_ATTEMPTS_LIMIT, QUEUED, HoldKind.IN_FLIGHT.value, FAILED_ATTEMPTS_LIMIT),
        )
        for row_id, chat_id in interrupted:
            logging.getLogger(__name__).warning(
                "Queue row %s for chat %s was interrupted by a restart while sending; counted as "
                "%d failed attempts, later rows no longer wait for it. It stays held for /confirm_send.",
                row_id, chat_id, FAILED_ATTEMPTS_LIMIT,
            )

    @property
    def connection(self) -> _QueueConnection:
        if self._connection is None:
            raise QueuePersistenceError("Outbound queue is closed.")
        return self._connection

    def _insert_row(self, sql: str, parameters: tuple) -> int | None:
        """Insert one queue row and return its id."""
        connection = self.connection
        if isinstance(connection, _PostgresqlConnection):
            return int(connection.execute(sql + " RETURNING id", parameters).fetchone()[0])
        # RETURNING needs SQLite 3.35; older supported versions report lastrowid.
        return connection.execute(sql, parameters).lastrowid

    def _payload_reader(self, row_id: int) -> _BlobReader:
        """Read one row's payload incrementally without loading it whole."""
        connection = self.connection
        if isinstance(connection, _PostgresqlConnection):
            return _PostgresqlBlobReader(connection, row_id)
        return _SQLiteBlobReader(self.path, row_id)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._ownership_file is not None:
                self._ownership_file.close()
                self._ownership_file = None

    def observe_metrics(self, observation: Callable[[QueueMetrics], None]) -> None:
        """Keep telemetry failures outside committed queue and delivery work."""
        if self.metrics is not None:
            try:
                observation(self.metrics)
            except Exception:
                logging.getLogger(__name__).exception("Unable to observe outbound metrics.")

    def refresh_depth(self) -> None:
        with self._lock:
            if self.metrics is not None:
                depth = self.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0]
                self.observe_metrics(lambda metrics: metrics.set_queue_depth(int(depth)))

    def _observe_removed(self, priority: int, operation: str, created_at: float, outcome: str) -> None:
        residence = max(0.0, time.time() - created_at)
        self.observe_metrics(lambda metrics: metrics.record_removal(priority, operation, outcome, residence))

    def has_queued_calls(self) -> bool:
        """Return whether a durable call is waiting without loading its payload."""
        with self._lock:
            return self.connection.execute(
                "SELECT 1 FROM outbound_queue WHERE delivery_state = 'queued' LIMIT 1"
            ).fetchone() is not None

    def record_removal(self, row: QueuedCall, outcome: str) -> None:
        self._observe_removed(row.priority, row.operation, row.created_at, outcome)
        self.refresh_depth()

    def _store_media_stream(
        self,
        source,
        filename: Optional[str],
        *,
        input_file: bool = True,
        attach_name: Optional[str] = None,
        mimetype: Optional[str] = None,
        created_media: Optional[set[str]] = None,
    ) -> _StoredMediaSnapshot:
        suffix = Path(filename).suffix if filename else ""
        fd, path = tempfile.mkstemp(prefix="media-", suffix=suffix, dir=self.media_dir)
        try:
            with os.fdopen(fd, "wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            try:
                directory_fd = os.open(self.media_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            raise
        storage_name = Path(path).name
        snapshot = _StoredMediaSnapshot(
            storage_name,
            filename,
            input_file=input_file,
            attach_name=attach_name,
            mimetype=mimetype,
        )
        if created_media is not None:
            created_media.add(storage_name)
        return snapshot

    def _snapshot_media_value(
        self,
        value: object,
        cleanup_files: frozenset[str] = frozenset(),
        created_media: Optional[set[str]] = None,
    ) -> object:
        if isinstance(value, _StoredMediaSnapshot):
            return value
        if isinstance(value, _LegacyInlineBlob):
            return _StoredMediaSnapshot(value.storage_name, None)
        if isinstance(value, _LegacyBytesIO):
            return value.snapshot
        if isinstance(value, bytes):
            return self._store_media_stream(io.BytesIO(value), None, created_media=created_media)
        local_path = self._local_media_path(value)
        if local_path is not None:
            try:
                parsed = urlsplit(value) if isinstance(value, str) else None
                if parsed is not None and parsed.scheme.lower() == "file":
                    assert isinstance(value, str)
                    resolved = str(local_path.resolve())
                    normalized_cleanup_files = {
                        str(Path(path).resolve()) for path in cleanup_files
                    }
                    if resolved in normalized_cleanup_files:
                        return _StoredMediaSnapshot(
                            None,
                            local_path.name,
                            external_uri=value,
                            cleanup_external=True,
                        )
                with local_path.open("rb") as source:
                    return self._store_media_stream(source, local_path.name, created_media=created_media)
            except (OSError, ValueError, QueueEnqueueError) as error:
                raise QueueEnqueueError("Unable to serialize queued Telegram call.") from error
        if isinstance(value, str):
            return value
        if isinstance(value, InputFile):
            stored_content = value.input_file_content
            if isinstance(stored_content, bytes):
                byte_stream = io.BytesIO(stored_content)
                return self._store_media_stream(
                    byte_stream,
                    value.filename,
                    input_file=True,
                    attach_name=value.attach_name,
                    mimetype=value.mimetype,
                    created_media=created_media,
                )
            captured = self._snapshot_media_value(stored_content, cleanup_files, created_media)
            if isinstance(captured, _StoredMediaSnapshot):
                return replace(
                    captured,
                    filename=value.filename or captured.filename,
                    input_file=True,
                    attach_name=value.attach_name,
                    mimetype=value.mimetype,
                )
            raise QueueEnqueueError("Unable to serialize queued Telegram call.")
        tell = getattr(value, "tell", None)
        seek = getattr(value, "seek", None)
        read = getattr(value, "read", None)
        if not callable(tell) or not callable(seek) or not callable(read):
            raise QueueEnqueueError("Unable to serialize queued Telegram call.")

        previous_position: Optional[int] = None
        snapshot: Optional[_StoredMediaSnapshot] = None
        try:
            previous_position = tell()
            seek(0)
            source_name = getattr(value, "name", None)
            filename = Path(source_name).name if isinstance(source_name, (str, Path)) else None
            snapshot = self._store_media_stream(value, filename or None, created_media=created_media)
        except Exception as error:
            raise QueueEnqueueError("Unable to serialize queued Telegram call.") from error
        finally:
            if previous_position is not None:
                try:
                    seek(previous_position)
                except Exception as restore_error:
                    if snapshot is not None and snapshot.storage_name is not None:
                        try:
                            (self.media_dir / snapshot.storage_name).unlink()
                        except FileNotFoundError:
                            pass
                    raise QueueEnqueueError("Unable to serialize queued Telegram call.") from restore_error
        assert snapshot is not None
        return snapshot

    @staticmethod
    def _local_media_path(value: object) -> Optional[Path]:
        if isinstance(value, Path):
            return value
        if not isinstance(value, str) or value.startswith(("http://", "https://")):
            return None
        try:
            uri = urlsplit(value)
        except ValueError:
            return None
        if uri.scheme.lower() == "file":
            if uri.netloc or uri.query or uri.fragment or not uri.path.startswith("/"):
                return None
            return Path(unquote(uri.path))
        try:
            path = Path(value)
            return path if path.is_file() else None
        except OSError:
            return None

    @classmethod
    def _needs_media_snapshot(cls, value: object) -> bool:
        return isinstance(value, (_StoredMediaSnapshot, _LegacyInlineBlob, _LegacyBytesIO, bytes)) or cls._local_media_path(value) is not None or isinstance(value, InputFile) or any(
            callable(getattr(value, name, None)) for name in ("tell", "seek", "read")
        )

    @classmethod
    def _validate_media_value(cls, value: object, telegram_type: Optional[type] = None) -> None:
        if isinstance(value, (bytes, str, Path, InputFile)):
            return
        if telegram_type is not None and isinstance(value, telegram_type):
            return
        if cls._needs_media_snapshot(value):
            return
        raise QueueEnqueueError("Unable to serialize queued Telegram call.")

    def _normalize_input_media(
        self, value: object, cleanup_files: frozenset[str], created_media: Optional[set[str]] = None,
    ) -> object:
        if not isinstance(value, InputMedia):
            return value
        normalized = copy.copy(value)
        self._validate_media_value(value.media, _NESTED_MEDIA_TYPES.get(type(value)))
        if self._needs_media_snapshot(value.media):
            object.__setattr__(
                normalized, "media", self._snapshot_media_value(value.media, cleanup_files, created_media)
            )
        for field in _INPUT_MEDIA_ATTACHMENT_FIELDS.get(type(value), ()):
            attachment = getattr(value, field, None)
            if attachment is None:
                continue
            self._validate_media_value(attachment)
            if self._needs_media_snapshot(attachment):
                object.__setattr__(
                    normalized, field, self._snapshot_media_value(attachment, cleanup_files, created_media)
                )
        return normalized

    def _normalize_direct_media(
        self, operation: str, args: tuple, kwargs: dict, cleanup_files: frozenset[str],
        created_media: Optional[set[str]] = None,
    ) -> tuple[tuple, dict]:
        argument = _DIRECT_MEDIA_ARGUMENTS.get(operation)
        if argument is None:
            return args, kwargs
        index, keyword = argument.index, argument.keyword
        positional = len(args) > index
        if not positional and keyword not in kwargs:
            return args, kwargs
        value = args[index] if positional else kwargs[keyword]
        self._validate_media_value(value, argument.telegram_type)
        if not self._needs_media_snapshot(value):
            return args, kwargs
        snapshot = self._snapshot_media_value(value, cleanup_files, created_media)
        explicit_filename = kwargs.get("filename")
        if explicit_filename is not None and not isinstance(explicit_filename, str):
            raise QueueEnqueueError("Unable to serialize queued Telegram call.")
        if isinstance(snapshot, _StoredMediaSnapshot) and explicit_filename is not None:
            snapshot = replace(snapshot, filename=explicit_filename)
        if positional:
            normalized_args = list(args)
            normalized_args[index] = snapshot
            return tuple(normalized_args), kwargs
        normalized_kwargs = dict(kwargs)
        normalized_kwargs[keyword] = snapshot
        return args, normalized_kwargs

    def _normalize_nested_media(
        self, operation: str, args: tuple, kwargs: dict, cleanup_files: frozenset[str],
        created_media: Optional[set[str]] = None,
    ) -> tuple[tuple, dict]:
        argument = _NESTED_MEDIA_ARGUMENTS.get(operation)
        if argument is None:
            return args, kwargs
        index, keyword = argument
        positional = len(args) > index
        if not positional and keyword not in kwargs:
            return args, kwargs
        value = args[index] if positional else kwargs[keyword]
        normalized: object
        if operation == "send_media_group":
            if not isinstance(value, (list, tuple)) or not value or not all(
                type(item) in _MEDIA_GROUP_TYPES for item in value
            ):
                raise QueueEnqueueError("Unable to serialize queued Telegram call.")
            normalized = type(value)(self._normalize_input_media(item, cleanup_files, created_media) for item in value)
        else:
            if not isinstance(value, InputMedia):
                raise QueueEnqueueError("Unable to serialize queued Telegram call.")
            normalized = self._normalize_input_media(value, cleanup_files, created_media)
        if positional:
            normalized_args = list(args)
            normalized_args[index] = normalized
            return tuple(normalized_args), kwargs
        normalized_kwargs = dict(kwargs)
        normalized_kwargs[keyword] = normalized
        return args, normalized_kwargs

    def _normalize_thumbnail(
        self, operation: str, kwargs: dict, cleanup_files: frozenset[str],
        created_media: Optional[set[str]] = None,
    ) -> dict:
        thumbnail = kwargs.get("thumbnail")
        if operation not in _THUMBNAIL_OPERATIONS or thumbnail is None:
            return kwargs
        self._validate_media_value(thumbnail)
        if not self._needs_media_snapshot(thumbnail):
            return kwargs
        normalized_kwargs = dict(kwargs)
        snapshot = self._snapshot_media_value(thumbnail, cleanup_files, created_media)
        if isinstance(snapshot, _StoredMediaSnapshot) and not snapshot.input_file:
            attachment = InputFile(b"", attach=True)
            snapshot = replace(
                snapshot, input_file=True, attach_name=attachment.attach_name
            )
        normalized_kwargs["thumbnail"] = snapshot
        return normalized_kwargs

    def _normalize_keyword_media(
        self, operation: str, kwargs: dict, cleanup_files: frozenset[str],
        created_media: Optional[set[str]] = None,
    ) -> dict:
        keywords = _KEYWORD_MEDIA_ARGUMENTS.get(operation, ())
        normalized_kwargs = kwargs
        for keyword in keywords:
            value = normalized_kwargs.get(keyword)
            if value is None:
                continue
            self._validate_media_value(value)
            if not self._needs_media_snapshot(value):
                continue
            snapshot = self._snapshot_media_value(value, cleanup_files, created_media)
            if normalized_kwargs is kwargs:
                normalized_kwargs = dict(kwargs)
            normalized_kwargs[keyword] = snapshot
        return normalized_kwargs

    @staticmethod
    def encode_payload(args: tuple, kwargs: dict) -> bytes:
        try:
            return b"\x02" + pickle.dumps((args, kwargs), protocol=5)
        except Exception as error:
            raise QueueEnqueueError("Unable to serialize queued Telegram call.") from error

    def blocking_timeout(self, payload: bytes) -> float:
        if self.transfer_min_mbps is None:
            return 300.0
        args, kwargs = self.decode_payload_raw(payload)
        references: list[_StoredMediaSnapshot] = []
        self._collect_media_references((args, kwargs), references)
        size = media_size((args, kwargs)) + sum(
            media_size(self.media_dir / ref.storage_name if ref.storage_name else ref.external_uri)
            for ref in references
        )
        return transfer_timeout(300.0, size, self.transfer_min_mbps)

    def _restore_media_references(self, value: object, opened: list[object]) -> object:
        if isinstance(value, _StoredMediaSnapshot):
            if value.external_uri is not None:
                external_path = self._local_media_path(value.external_uri)
                if external_path is None or not external_path.is_file():
                    # Same handling as the first receive: fetch it and let the
                    # normal media path store it, instead of dropping the row.
                    if urlsplit(value.external_uri).scheme.lower() in {"http", "https"}:
                        try:
                            with urllib.request.urlopen(value.external_uri, timeout=20) as response:
                                body = response.read(self.MAX_REPLAY_BYTES + 1)
                        except Exception:
                            body = b""
                        if 0 < len(body) <= self.MAX_REPLAY_BYTES:
                            return self._store_media_stream(io.BytesIO(body), value.filename)
                    raise MissingQueuedExternalMediaError(
                        f"Queued external media file {value.external_uri!r} is missing."
                    )
                return value.external_uri
            if value.storage_name is None:
                raise InvalidQueuedPayloadError("Queued media reference has no storage path.")
            path = self.media_dir / value.storage_name
            try:
                raw = path.open("rb", buffering=0)
            except OSError as error:
                raise InvalidQueuedPayloadError(
                    f"Queued media file {value.storage_name!r} is missing."
                ) from error
            opened.append(raw)
            stream = _NamedMediaFile(raw, value.filename)
            opened.append(stream)
            if not value.input_file:
                return stream
            input_file = InputFile(
                stream,
                filename=value.filename or "file",
                attach=value.attach_name is not None,
                read_file_handle=False,
            )
            input_file.attach_name = value.attach_name
            if value.mimetype is not None:
                input_file.mimetype = value.mimetype
            return input_file
        if isinstance(value, tuple):
            return tuple(self._restore_media_references(item, opened) for item in value)
        if isinstance(value, list):
            return [self._restore_media_references(item, opened) for item in value]
        if isinstance(value, dict):
            return {key: self._restore_media_references(item, opened) for key, item in value.items()}
        if isinstance(value, InputMedia):
            restored_media = copy.copy(value)
            object.__setattr__(restored_media, "media", self._restore_media_references(value.media, opened))
            for field in _INPUT_MEDIA_ATTACHMENT_FIELDS.get(type(value), ()):
                attachment = getattr(value, field, None)
                if attachment is not None:
                    object.__setattr__(
                        restored_media, field, self._restore_media_references(attachment, opened)
                    )
            return restored_media
        return value

    @staticmethod
    def decode_payload_raw(payload: bytes) -> tuple[tuple, dict]:
        if not payload or payload[0] not in (1, 2):
            raise InvalidQueuedPayloadError("Queued payload has an unknown version.")
        try:
            value = pickle.loads(payload[1:])
        except Exception as error:
            raise InvalidQueuedPayloadError("Queued payload cannot be decoded.") from error
        if not isinstance(value, tuple) or len(value) != 2:
            raise InvalidQueuedPayloadError("Queued payload has an invalid outer shape.")
        args, kwargs = value
        if not isinstance(args, tuple) or not isinstance(kwargs, dict):
            raise InvalidQueuedPayloadError("Queued payload has invalid arguments.")
        return args, kwargs

    def decode_payload(self, payload: bytes) -> tuple[tuple, dict]:
        args, kwargs = self.decode_payload_raw(payload)
        if payload[0] == 2:
            opened: list[object] = []
            try:
                restored_args = self._restore_media_references(args, opened)
                restored_kwargs = self._restore_media_references(kwargs, opened)
                assert isinstance(restored_args, tuple) and isinstance(restored_kwargs, dict)
                return restored_args, restored_kwargs
            except BaseException:
                self.close_payload_resources(tuple(reversed(opened)))
                raise
        return args, kwargs

    @classmethod
    def _collect_media_references(cls, value: object, found: list[_StoredMediaSnapshot]) -> None:
        if isinstance(value, _StoredMediaSnapshot):
            found.append(value)
            return
        if isinstance(value, (tuple, list)):
            for item in value:
                cls._collect_media_references(item, found)
            return
        if isinstance(value, dict):
            for item in value.values():
                cls._collect_media_references(item, found)
            return
        if isinstance(value, InputMedia):
            cls._collect_media_references(value.media, found)
            for field in _INPUT_MEDIA_ATTACHMENT_FIELDS.get(type(value), ()):
                attachment = getattr(value, field, None)
                if attachment is not None:
                    cls._collect_media_references(attachment, found)

    def _media_reference_keys(self, payload: bytes) -> frozenset[tuple[str, str]]:
        if not payload or payload[0] != 2:
            return frozenset()
        try:
            value = pickle.loads(payload[1:])
        except Exception as error:
            raise InvalidQueuedPayloadError("Queued media references cannot be inspected.") from error
        references: list[_StoredMediaSnapshot] = []
        self._collect_media_references(value, references)
        keys: set[tuple[str, str]] = set()
        for reference in references:
            if reference.storage_name is not None:
                keys.add(("stored", reference.storage_name))
            elif reference.cleanup_external and reference.external_uri is not None:
                keys.add(("external", reference.external_uri))
        return frozenset(keys)

    def cleanup_payload_media(self, payload: bytes) -> None:
        if not payload or payload[0] != 2:
            return
        try:
            value = pickle.loads(payload[1:])
        except Exception:
            return
        references: list[_StoredMediaSnapshot] = []
        self._collect_media_references(value, references)
        for reference in references:
            path: Optional[Path] = None
            if reference.storage_name is not None:
                path = self.media_dir / reference.storage_name
            elif reference.cleanup_external and reference.external_uri is not None:
                path = self._local_media_path(reference.external_uri)
            if path is None:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _cleanup_orphan_media(self) -> None:
        referenced: set[str] = set()
        try:
            queued = self.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0]
            if queued > self.MAX_ORPHAN_SCAN_ROWS:
                logging.getLogger(__name__).warning(
                    "Skipping the outbound media sweep for %d queued rows (limit %d); "
                    "unreferenced sidecar files are retained.",
                    int(queued), self.MAX_ORPHAN_SCAN_ROWS,
                )
                return
            cursor = self.connection.execute("SELECT id FROM outbound_queue")
            for (row_id,) in cursor:
                with self._payload_reader(int(row_id)) as payload:
                    version = payload.read(1)
                    if version == b"\x01":
                        continue
                    if version != b"\x02":
                        # Unknown encodings may still own any of the sidecars.
                        return
                    # A v2 row can still contain oversized opaque data.  Keep
                    # all sidecars until bounded recovery can inspect it.
                    if payload.length > self.MAX_REPLAY_BYTES:
                        return
                    try:
                        value = pickle.Unpickler(payload).load()
                    except Exception:
                        # A corrupt live row may still reference any sidecar name.
                        # Preserve all files rather than risk deleting its media.
                        return
                references: list[_StoredMediaSnapshot] = []
                self._collect_media_references(value, references)
                for item in references:
                    if item.storage_name is not None:
                        path = self.media_dir / item.storage_name
                        if not path.is_file():
                            raise QueuePersistenceError(
                                f"Queued media file {item.storage_name!r} is missing; row retained."
                            )
                        referenced.add(item.storage_name)
                    # Temporary external files may be cleaned on restart. Dispatch
                    # validates them and discards only the affected queue row.
        except self._database_errors:
            return
        try:
            for path in self.media_dir.iterdir():
                if path.is_file() and path.name not in referenced:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    @classmethod
    def streaming_uploads(cls, value: object) -> object:
        """PTB otherwise reads a raw file handle completely before making HTTP."""
        if isinstance(value, _NamedMediaFile):
            return InputFile(value, filename=getattr(value, "name", None), attach=True, read_file_handle=False)
        if isinstance(value, tuple):
            return tuple(cls.streaming_uploads(item) for item in value)
        if isinstance(value, list):
            return [cls.streaming_uploads(item) for item in value]
        if isinstance(value, dict):
            return {key: cls.streaming_uploads(item) for key, item in value.items()}
        if isinstance(value, InputMedia):
            media = copy.copy(value)
            object.__setattr__(media, "media", cls.streaming_uploads(value.media))
            for field in _INPUT_MEDIA_ATTACHMENT_FIELDS.get(type(value), ()):
                attachment = getattr(value, field, None)
                if attachment is not None:
                    object.__setattr__(media, field, cls.streaming_uploads(attachment))
            return media
        return value

    @classmethod
    def payload_closeables(cls, *values: object) -> tuple[object, ...]:
        result: list[object] = []
        seen: set[int] = set()

        def collect(value: object) -> None:
            if id(value) in seen:
                return
            seen.add(id(value))
            if isinstance(value, InputFile):
                collect(value.input_file_content)
                return
            if isinstance(value, InputMedia):
                collect(value.media)
                for field in _INPUT_MEDIA_ATTACHMENT_FIELDS.get(type(value), ()):
                    attachment = getattr(value, field, None)
                    if attachment is not None:
                        collect(attachment)
                return
            if isinstance(value, dict):
                for item in value.values():
                    collect(item)
                return
            if isinstance(value, (tuple, list)):
                for item in value:
                    collect(item)
                return
            close = getattr(value, "close", None)
            if callable(close):
                result.append(value)

        for value in values:
            collect(value)
        return tuple(result)

    @staticmethod
    def close_payload_resources(closeables: tuple[object, ...]) -> None:
        for value in closeables:
            close = getattr(value, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:
                pass

    @staticmethod
    def _validate_metadata(operation: str, kwargs: Mapping[str, object]) -> tuple[dict, int, Optional[str], Optional[str]]:
        if operation not in QUEUED_OPERATIONS:
            raise QueueEnqueueError(f"Unsupported queued operation: {operation}")
        telegram_kwargs = dict(kwargs)
        send_mode = telegram_kwargs.pop("_send_mode", "eventual")
        slave_id = telegram_kwargs.pop("_slave_id", None)
        required_sender = telegram_kwargs.pop("_required_sender_bot_id", None)
        if send_mode == "blocking":
            priority = 1
        elif send_mode == "eventual":
            priority = 0
        else:
            raise QueueEnqueueError("_send_mode must be 'blocking' or 'eventual'.")
        if slave_id is not None and (not isinstance(slave_id, str) or not slave_id):
            raise QueueEnqueueError("_slave_id must be a non-empty string when supplied.")
        if required_sender is not None and (not isinstance(required_sender, str) or not required_sender):
            raise QueueEnqueueError("_required_sender_bot_id must be a non-empty string when supplied.")
        if operation in REQUIRED_SENDER_OPERATIONS:
            if required_sender is None:
                raise QueueEnqueueError(f"{operation} requires _required_sender_bot_id.")
        elif required_sender is not None and required_sender != "__main__" and operation != "copy_message":
            raise QueueEnqueueError(f"{operation} cannot require a sender.")
        return telegram_kwargs, priority, slave_id, required_sender

    @staticmethod
    def _destination(operation: Callable[..., object], args: tuple, kwargs: dict) -> int:
        try:
            bound = inspect.signature(operation).bind(*args, **kwargs)
        except (TypeError, ValueError) as error:
            raise QueueEnqueueError("Queued arguments do not bind to the Telegram operation.") from error
        if "chat_id" not in bound.arguments:
            raise QueueEnqueueError("Queued Telegram operation has no chat_id.")
        chat_id = bound.arguments["chat_id"]
        if isinstance(chat_id, bool) or not isinstance(chat_id, numbers.Integral):
            raise QueueEnqueueError("chat_id must be a non-Boolean integral value.")
        return int(chat_id)

    def _prepare(self, request: QueueRequest, operation: Callable[..., object]) -> tuple[str, tuple, dict, int, int, Optional[str], Optional[str], bytes, Optional[bytes], set[str]]:
        if not isinstance(request.operation, str) or not isinstance(request.args, tuple) or not isinstance(request.kwargs, dict):
            raise QueueEnqueueError("Queue request must be (operation: str, args: tuple, kwargs: dict).")
        cleanup_files = frozenset(
            str(Path(path).resolve()) for path in request.cleanup_files
        )
        created_media: set[str] = set()
        try:
            telegram_kwargs, priority, slave_id, required_sender = self._validate_metadata(
                request.operation, request.kwargs
            )
            supplement = telegram_kwargs.pop(SUPPLEMENTAL_KEY, False)
            history_replay = telegram_kwargs.pop(HISTORY_REPLAY_KEY, None)
            if history_replay is not None and not isinstance(history_replay, dict):
                raise QueueEnqueueError("History replay metadata must be a mapping.")
            telegram_args, telegram_kwargs = self._normalize_direct_media(
                request.operation, request.args, telegram_kwargs, cleanup_files, created_media
            )
            telegram_args, telegram_kwargs = self._normalize_nested_media(
                request.operation, telegram_args, telegram_kwargs, cleanup_files, created_media
            )
            telegram_kwargs = self._normalize_thumbnail(
                request.operation, telegram_kwargs, cleanup_files, created_media
            )
            telegram_kwargs = self._normalize_keyword_media(
                request.operation, telegram_kwargs, cleanup_files, created_media
            )
            chat_id = self._destination(operation, telegram_args, telegram_kwargs)
            if history_replay is not None:
                telegram_kwargs[HISTORY_REPLAY_KEY] = history_replay
            if supplement:
                telegram_kwargs[SUPPLEMENTAL_KEY] = True
            payload = self.encode_payload(telegram_args, telegram_kwargs)
            if request.log_context is not None and not isinstance(request.log_context, bytes):
                raise QueueEnqueueError("Queued log context must be bytes when supplied.")
            return (
                request.operation, telegram_args, telegram_kwargs, chat_id, priority,
                slave_id, required_sender, payload, request.log_context, created_media,
            )
        except BaseException:
            self._cleanup_created_media(created_media)
            raise

    def _cleanup_created_media(self, storage_names: Iterable[str]) -> None:
        for storage_name in storage_names:
            try:
                (self.media_dir / storage_name).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def enqueue_many(
        self, requests: Iterable[QueueRequest], operation_resolver: Callable[[str], Callable[..., object]],
        *, history_keys: Iterable[str] = (), logical_split: Optional[dict] = None,
    ) -> tuple[int, Future]:
        request_list = list(requests)
        if not request_list:
            raise QueueEnqueueError("Queued request sequence cannot be empty.")
        with self._lock:
            self.connection  # Reject closed queues before creating sidecars.
            prepared = []
            try:
                if logical_split and logical_split.get("after"):
                    from .queued_log import encode_aggregation
                    after = logical_split["after"]
                    request_list.append(QueueRequest("send_message", (),
                        {"chat_id": after["key"][1], "text": "", "_slave_id": after["key"][0], "_send_mode": "eventual"},
                        encode_aggregation(after)))
                for request in request_list:
                    prepared.append(self._prepare(request, operation_resolver(request.operation)))
            except Exception:
                for item in prepared:
                    self._cleanup_created_media(item[9])
                raise
            destinations = {(item[3], item[4]) for item in prepared}
            if len(destinations) != 1:
                for item in prepared:
                    self._cleanup_created_media(item[9])
                raise QueueEnqueueError("Queued request sequence must share chat_id and priority.")
            try:
                self.connection.execute("BEGIN")
                if logical_split:
                    from .queued_log import decode_aggregation, encode_aggregation
                    original = self.connection.execute("SELECT predecessor_id, log_context, priority, operation, created_at FROM outbound_queue WHERE "
                        "id=? AND " + waiting_sql(), (logical_split["row_id"],)).fetchone()
                    if original is None:
                        raise QueuePersistenceError("Source batch was submitted before its split.")
                    before = logical_split.get("before")
                    # The batch keeps the members before the source, or is
                    # withdrawn when the split rows replace all of it.
                    original_context = decode_aggregation(original[1])
                    split_event = ContextEvent.REPLACE if before else ContextEvent.WITHDRAW
                    if check_context_transition(logical_split["row_id"], original_context, split_event,
                                                before) is not ContextState.LOGICAL:
                        raise IllegalAggregationTransitionError(
                            logical_split["row_id"], (original_context or {}).get("kind"), split_event,
                            "Only a logical batch is split.")
                    if before:
                        self._mark_row_destinations_dirty(logical_split["row_id"], dependents=True)
                    else:
                        # The split rows replace the batch row, which is removed below.
                        self._transition(logical_split["row_id"], Event.WITHDRAW, dependents=True)
                    predecessor = logical_split["row_id"] if before else original[0]
                    if before:
                        self.connection.execute("UPDATE outbound_queue SET log_context=? WHERE id=?",
                                                (encode_aggregation(before), logical_split["row_id"]))
                # Receipts outlive queue-row completion until the source staging
                # records have been durably removed from either main DB backend.
                self.connection.executemany(
                    "INSERT INTO history_ownership (entry_key) VALUES (?)",
                    ((key,) for key in history_keys),
                )
                identifiers: list[int] = []
                now = time.time()
                for request, item in zip(request_list, prepared):
                    operation, _args, _kwargs, chat_id, priority, slave_id, required_sender, payload, log_context, _created_media = item
                    _kwargs_thread = queued_thread_id(_kwargs, log_context)
                    identifier = self._insert_row(
                        "INSERT INTO outbound_queue "
                        "(priority, telegram_chat_id, operation, payload, slave_id, required_sender_bot_id, "
                        "created_at, log_context, predecessor_id, thread_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (priority, chat_id, operation, payload, slave_id, required_sender, now, log_context,
                         predecessor if logical_split else request.predecessor_id,
                         queued_thread_id(_kwargs, log_context)),
                    )
                    if identifier is None:
                        raise QueueEnqueueError("SQLite did not return an inserted queue row ID.")
                    identifiers.append(identifier)
                    self.mark_destination_dirty(chat_id, _kwargs_thread)
                    if logical_split:
                        predecessor = identifier
                if logical_split:
                    self.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE predecessor_id=? AND id NOT IN (" +
                        ",".join("?" for _ in identifiers) + ")", (identifiers[-1], logical_split["row_id"], *identifiers))
                    if not before:
                        self.connection.execute("DELETE FROM outbound_queue WHERE id=?", (logical_split["row_id"],))
                self.connection.commit()
            except Exception as error:
                try:
                    self.connection.rollback()
                except self._database_errors:
                    pass
                for item in prepared:
                    self._cleanup_created_media(item[9])
                raise QueueEnqueueError("Unable to commit queued Telegram call.") from error
            if self.metrics is not None:
                for operation, _args, _kwargs, _chat_id, priority, _slave_id, _required_sender, _payload, _log_context, _created_media in prepared:
                    self.observe_metrics(lambda metrics: metrics.record_enqueued(priority, operation))
            if logical_split and not before:
                self._observe_removed(original[2], original[3], original[4], "replaced")
                self.fail_waiter(logical_split["row_id"], QueueError(
                    f"Queue row {logical_split['row_id']} was replaced by its split before it was sent."))
            self.refresh_depth()
            waiter: Future = Future()
            self.waiters[identifiers[0]] = waiter
            return identifiers[0], waiter

    def contains(self, row_id: int) -> bool:
        with self._lock:
            return self.connection.execute("SELECT 1 FROM outbound_queue WHERE id=?", (row_id,)).fetchone() is not None

    def aggregation_context(self, row_id: int) -> Optional[bytes]:
        with self._lock:
            row = self.connection.execute(
                "SELECT log_context FROM outbound_queue WHERE id=? AND substr(log_context, 1, 1)=?",
                (row_id, b"\x03"),
            ).fetchone()
        return row[0] if row else None

    def aggregation_rows(self) -> list[QueuedCall]:
        """Read unfinished logical/frozen aggregation rows without their payloads.

        Recovery and source lookups read `log_context`, identity and ordering only.
        Reading the payload here materialised every stored file (and every legacy
        inline media row) on each recovery pass and on each source lookup.
        """
        with self._lock:
            rows = self.connection.execute(
                f"SELECT id, priority, telegram_chat_id, operation, {self._empty_blob}, slave_id, "
                "required_sender_bot_id, created_at, log_context, delivery_state, completion_receipt, "
                "predecessor_id, thread_id "
                "FROM outbound_queue WHERE substr(log_context, 1, 1)=? ORDER BY id", (b"\x03",),
            ).fetchall()
        return [QueuedCall(row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                           row[7], row[8], row[9], row[10], predecessor_id=row[11],
                           thread_id=row[12]) for row in rows]

    def cancelled_source(self, identity: tuple[str, str]) -> Optional[dict]:
        """Read a confirmed unpublished withdrawal independently of destination."""
        from .queued_log import decode_aggregation
        with self._lock:
            row = self.connection.execute(
                "SELECT log_context FROM source_cancellations WHERE origin_uid=? AND source_id=?", identity,
            ).fetchone()
        return decode_aggregation(row[0]) if row else None

    def _record_source_cancellation(self, key: tuple, member: SourceMember) -> None:
        from .queued_log import encode_aggregation
        context = encode_aggregation(dict(format_version=1, kind=ContextKind.CANCELLATION, key=tuple(key), member=member))
        self.connection.execute(
            "INSERT INTO source_cancellations (origin_uid, source_id, source_revision, log_context) "
            "VALUES (?, ?, ?, ?) ON CONFLICT (origin_uid, source_id) DO UPDATE SET "
            "source_revision=excluded.source_revision, log_context=excluded.log_context "
            "WHERE excluded.source_revision > source_cancellations.source_revision",
            (member["origin_uid"], member["source_id"], member["source_revision"], context),
        )

    def close_aggregation_destination(self, chat_id: int, topic: Optional[str]) -> None:
        """Persist an incoming-message boundary without changing attempted requests."""
        from .queued_log import decode_aggregation, encode_aggregation
        with self._lock, self.connection:
            rows = self.connection.execute(
                "SELECT id, log_context FROM outbound_queue WHERE telegram_chat_id=? "
                "AND substr(log_context, 1, 1)=?",
                (chat_id, b"\x03"),
            ).fetchall()
            for row_id, payload in rows:
                context = decode_aggregation(payload)
                key = context.get("key")
                if not key or int(key[1]) != chat_id or key[2] != topic:
                    continue
                # Rows without append flags (independent outputs, boundaries) do not take it.
                if not next_context_states(context_state(context), ContextEvent.CLOSE_DESTINATION):
                    continue
                if context["kind"] == ContextKind.LOGICAL:
                    context.update(base=None, inherit=False, appendable=False, incoming_boundary=True)
                elif context["kind"] in {ContextKind.AGGREGATE, ContextKind.MEMBER_UPDATE}:
                    context.update(appendable=False, incoming_boundary=True)
                    retry = context.get("retry_context")
                    if retry:
                        retry.update(base=None, inherit=False, appendable=False, incoming_boundary=True)
                else:
                    continue
                self._mark_row_destinations_dirty(row_id)
                self.connection.execute("UPDATE outbound_queue SET log_context=? WHERE id=?",
                                        (encode_aggregation(context), row_id))

    def replace_logical_aggregation(self, row_id: int, context: bytes, *, cancelled_member=None) -> bool:
        from .queued_log import decode_aggregation
        with self._lock, self.connection:
            self._mark_row_destinations_dirty(row_id)
            row = self.connection.execute(
                "SELECT log_context FROM outbound_queue WHERE id=? AND " + waiting_sql(), (row_id,),
            ).fetchone()
            if row is None:
                return False
            try:
                check_context_transition(row_id, decode_aggregation(row[0]), ContextEvent.REPLACE,
                                         decode_aggregation(context))
            except IllegalAggregationTransitionError as error:
                logging.getLogger(__name__).warning("Aggregation batch not replaced: %s", error)
                return False
            if cancelled_member is not None:
                self._record_source_cancellation(tuple(decode_aggregation(row[0])["key"]), cancelled_member)
            self.connection.execute("UPDATE outbound_queue SET log_context=? WHERE id=?", (context, row_id))
            return True

    def materialize_aggregation(self, row_id: int, operation: str, kwargs: dict,
                                context: bytes, selection: SenderSelection) -> QueuedCall:
        """Freeze the selected operation and sender before executor submission."""
        from .queued_log import decode_aggregation
        payload = self.encode_payload((), kwargs)
        with self._lock, self.connection:
            self._transition(row_id, Event.MATERIALIZE)
            stored = self.connection.execute("SELECT log_context FROM outbound_queue WHERE id=?", (row_id,)).fetchone()
            check_context_transition(row_id, decode_aggregation(stored[0]), ContextEvent.MATERIALIZE,
                                     decode_aggregation(context))
            cursor = self.connection.execute(
                "UPDATE outbound_queue SET operation=?, payload=?, log_context=?, required_sender_bot_id=?, "
                "attempt_sender_bot_id=?, delivery_hold=?, attempt_started_at=? "
                "WHERE id=? AND " + waiting_sql(),
                (operation, payload, context, selection.sender_bot_id or "__main__", selection.sender_bot_id,
                 HoldKind.IN_FLIGHT.value, time.time(), row_id),
            )
            if cursor.rowcount != 1:
                raise QueuePersistenceError("Aggregation batch is no longer available for dispatch.")
        return self.load_queued(row_id)

    def handoff_aggregation(self, row_id: int, parent_context: bytes,
                            update_successor: Callable[[bytes], bytes], *,
                            requests: Iterable[QueueRequest] = (), operation_resolver=None) -> tuple[list[int], bool]:
        """Publish the confirmed base and parent handoff in one queue transaction."""
        from .queued_log import decode_aggregation
        with self._lock:
            with self.connection:
                previous = self.connection.execute("SELECT log_context FROM outbound_queue WHERE id=?", (row_id,)).fetchone()
                if previous is None:
                    return [], False
                parent = decode_aggregation(parent_context)
                if check_context_transition(row_id, decode_aggregation(previous[0]), ContextEvent.RECEIPT,
                                            parent) is ContextState.HANDED_OFF:
                    # Applied already; a retry after a failed row removal changes nothing.
                    return [], False
                self._mark_row_destinations_dirty(row_id, dependents=True)
                identifiers = []
                inserted = []
                for request in requests:
                    item = self._prepare(request, operation_resolver(request.operation))
                    operation, _, kwargs, chat_id, priority, slave_id, required_sender, payload, context, _ = item
                    identifier = self._insert_row(
                        "INSERT INTO outbound_queue (priority, telegram_chat_id, operation, payload, slave_id, "
                        "required_sender_bot_id, created_at, log_context, predecessor_id, thread_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (priority, chat_id, operation, payload, slave_id, required_sender, time.time(), context,
                         request.predecessor_id, queued_thread_id(kwargs, context)))
                    if identifier is None:
                        raise QueuePersistenceError("SQLite did not return the aggregation successor ID.")
                    self.mark_destination_dirty(chat_id, queued_thread_id(kwargs, context))
                    identifiers.append(identifier)
                    inserted.append((priority, operation))
                rows = self.connection.execute(
                    "SELECT id, log_context FROM outbound_queue WHERE predecessor_id=? OR predecessor_id=?",
                    (row_id, parent.get("supplement_id")),
                ).fetchall()
                for identifier, context in rows:
                    self._mark_row_destinations_dirty(identifier)
                    if context and context[:1] == b"\x03":
                        updated = update_successor(context)
                        owner = decode_aggregation(updated).get("required_sender")
                        self.connection.execute("UPDATE outbound_queue SET log_context=? WHERE id=?",
                                                (updated, identifier))
                        if owner is not None:
                            self.connection.execute("UPDATE outbound_queue SET required_sender_bot_id=? WHERE id=?", (owner, identifier))
                self.connection.execute("UPDATE outbound_queue SET log_context=? WHERE id=?",
                                        (parent_context, row_id))
                self.connection.execute("UPDATE outbound_queue SET predecessor_id=NULL WHERE predecessor_id=?", (row_id,))
            for priority, operation in inserted:
                self.observe_metrics(lambda metrics: metrics.record_enqueued(priority, operation))
            self.refresh_depth()
        return identifiers, True

    def cancel_logical_aggregation(self, row_id: int, *, cancelled_member=None) -> Optional[int]:
        """Remove an unsubmitted empty batch while retaining its predecessor barrier."""
        with self._lock:
            with self.connection:
                self._transition(row_id, Event.WITHDRAW, dependents=True)
                row = self.connection.execute("SELECT predecessor_id, log_context, priority, operation, created_at FROM outbound_queue WHERE id=? AND "
                                              + waiting_sql(), (row_id,)).fetchone()
                if row is None:
                    raise QueuePersistenceError("The logical batch was already submitted.")
                from .queued_log import decode_aggregation
                check_context_transition(row_id, decode_aggregation(row[1]), ContextEvent.WITHDRAW)
                if cancelled_member is not None:
                    self._record_source_cancellation(tuple(decode_aggregation(row[1])["key"]), cancelled_member)
                self.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE predecessor_id=?", (row[0], row_id))
                self.connection.execute("DELETE FROM outbound_queue WHERE id=?", (row_id,))
            self._observe_removed(row[2], row[3], row[4], "cancelled")
            self.fail_waiter(row_id, QueueError(f"Queue row {row_id} was withdrawn before it was sent."))
            self.refresh_depth()
        return row[0]

    def cancel_pending_source(self, key: tuple, member: SourceMember) -> dict[int, Optional[int]]:
        """Withdraw unsubmitted standalone versions and preserve successor ordering."""
        from .aggregate import member_identity
        from .queued_log import decode_aggregation
        with self._lock, self.connection:
            rows = self.connection.execute(
                "SELECT id, log_context, delivery_state, delivery_hold, attempt_started_at "
                "FROM outbound_queue WHERE substr(log_context, 1, 1)=? ORDER BY id", (b"\x03",),
            ).fetchall()
            matching = []
            for row in rows:
                context = decode_aggregation(row[1])
                if (tuple(context["key"]) == tuple(key)
                        and context["kind"] in {ContextKind.SOURCE, ContextKind.REDIRECT}
                        and member_identity(context["member"]) == member_identity(member)):
                    if next_state(classify(row[2], row[3]), Event.WITHDRAW) is None or row[4] is not None:
                        return {}
                    try:
                        check_context_transition(row[0], context, ContextEvent.WITHDRAW)
                    except IllegalAggregationTransitionError as error:
                        logging.getLogger(__name__).warning("Pending source not withdrawn: %s", error)
                        return {}
                    matching.append(row[0])
            if not matching:
                return {}
            self._record_source_cancellation(tuple(key), member)
            predecessors = {}
            payloads = []
            removed = []
            for row_id in matching:
                predecessor, payload, priority, operation, created_at = self.connection.execute(
                    "SELECT predecessor_id, payload, priority, operation, created_at FROM outbound_queue WHERE id=?", (row_id,),
                ).fetchone()
                self._transition(row_id, Event.WITHDRAW, dependents=True)
                self.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE predecessor_id=?", (predecessor, row_id))
                self.connection.execute("DELETE FROM outbound_queue WHERE id=?", (row_id,))
                predecessors[row_id] = predecessor
                payloads.append(payload)
                removed.append((priority, operation, created_at))
        for priority, operation, created_at in removed:
            self._observe_removed(priority, operation, created_at, "cancelled")
        for payload in payloads:
            self.cleanup_payload_media(payload)
        for row_id in predecessors:
            self.fail_waiter(row_id, QueueError(f"Queue row {row_id} was withdrawn before it was sent."))
        self.refresh_depth()
        return predecessors

    def release_dependents(self, row_id: int) -> None:
        with self._lock, self.connection:
            self._mark_row_destinations_dirty(row_id, dependents=True)
            self.connection.execute("UPDATE outbound_queue SET predecessor_id=NULL WHERE predecessor_id=?", (row_id,))

    def hold_failed_aggregation(self, row_id: int, error: BaseException) -> bool:
        """Retain a failed aggregation row, or a row others are ordered behind.

        Returns False, without writing, for any other row: the caller then
        retains or discards it as an ordinary failure.
        """
        with self._lock, self.connection:
            self._transition(row_id, Event.HOLD_FAILED_AGGREGATION, dependents=True)
            cursor = self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=?, hold_reason=? WHERE id=? AND delivery_state=? "
                "AND (substr(log_context, 1, 1)=? "
                "OR EXISTS (SELECT 1 FROM outbound_queue child WHERE child.predecessor_id=outbound_queue.id))",
                (format_hold(HoldKind.AGGREGATION_FAILED, error_detail(error)), telegram_error_text(error),
                 row_id, QUEUED, b"\x03"),
            )
            return cursor.rowcount == 1

    def history_ownership_page(self, after: str = "", limit: int = 100) -> list[str]:
        with self._lock:
            return [row[0] for row in self.connection.execute(
                "SELECT entry_key FROM history_ownership WHERE entry_key > ? ORDER BY entry_key LIMIT ?",
                (after, limit),
            )]

    def owned_history_entries(self, keys: Iterable[str]) -> set[str]:
        keys = list(keys)
        if not keys:
            return set()
        with self._lock:
            return {row[0] for row in self.connection.execute(
                "SELECT entry_key FROM history_ownership WHERE entry_key IN (" +
                ",".join("?" for _ in keys) + ")", keys,
            )}

    def forget_history_entries(self, keys: Iterable[str]) -> None:
        """Called only after source staging deletion has committed."""
        with self._lock, self.connection:
            self.connection.executemany(
                "DELETE FROM history_ownership WHERE entry_key = ?", ((key,) for key in keys)
            )

    def heads(
        self, *, include_payload: bool = True, excluded_ids: Iterable[int] = (),
        ready_only: bool = False, respect_predecessors: bool = True,
    ) -> list[QueuedCall]:
        payload = "q.payload" if include_payload else self._empty_blob
        context = ("q.log_context" if include_payload
                   else f"CASE WHEN q.log_context IS NULL THEN NULL ELSE {self._empty_blob} END")
        receipt = "q.completion_receipt" if include_payload else "NULL"
        excluded = tuple(excluded_ids)
        candidates_sql = f"delivery_state = '{QUEUED}'"
        if ready_only:
            # Only a predecessor that is itself still waiting defers delivery. A
            # missing predecessor or a permanent hold cannot be an ordering
            # barrier: waiting for it either already happened or can never happen,
            # so a held row must not strand everything queued behind it.
            candidates_sql = ready_sql("outbound_queue") if respect_predecessors else waiting_sql()
            candidates_sql += " AND NOT " + backing_off_sql("outbound_queue", "?")
        exclusion_sql = ""
        parameters: tuple[object, ...] = (time.time(),) if ready_only else ()
        if excluded:
            exclusion_sql += " AND id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            parameters += tuple(excluded)
        with self._lock:
            # Select one runnable head per destination before reading media BLOBs.
            # Quarantined historical rows may be skipped explicitly so one
            # unreplayable attachment does not strand all newer traffic for the
            # same Telegram destination. Queues are then offered head slots one
            # depth tier at a time: the shallowest waiting destination first, and a
            # tier only gets a second slot once every destination with waiting rows
            # has had one. Draining short queues still shrinks the waiting set
            # fastest, but the whole pass cannot settle on the shallow tiers and
            # leave a deep destination without a worker. Equal depths keep FIFO
            # order by head ID.
            rows = self.connection.execute(
                f"SELECT q.id, q.priority, q.telegram_chat_id, q.operation, {payload}, q.slave_id, "
                f"q.required_sender_bot_id, q.created_at, {context}, q.delivery_state, {receipt}, "
                "length(q.payload) + COALESCE(length(q.log_context), 0) + COALESCE(length(q.completion_receipt), 0), "
                "q.reconcile_after, q.thread_id, heads.waiting "
                "FROM outbound_queue AS q JOIN ("
                "SELECT COALESCE(MIN(CASE WHEN priority = 1 THEN id END), MIN(id)) AS head_id, "
                "COUNT(*) AS waiting "
                "FROM outbound_queue WHERE " + candidates_sql + exclusion_sql +
                " GROUP BY telegram_chat_id, COALESCE(thread_id, 0)"
                ") AS heads ON q.id = heads.head_id "
                "ORDER BY ROW_NUMBER() OVER (PARTITION BY heads.waiting ORDER BY heads.head_id), "
                "heads.waiting",
                parameters,
            ).fetchall()
        return [
            QueuedCall(
                row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8],
                row[9], row[10], stored_bytes=int(row[11]), retry_after=float(row[12]),
                thread_id=row[13], waiting_rows=int(row[14]),
            )
            for row in rows
        ]

    def head_for(
        self, telegram_chat_id: int, thread_id: Optional[int], *,
        exclude_ids: Iterable[int] = (), ready_only: bool = True,
    ) -> Optional[QueuedCall]:
        """One destination's next runnable row, without scanning the whole queue.

        Same election rule as `heads()`: the lowest priority-1 id wins, else the
        lowest id, among rows that are not held, not waiting for a predecessor
        and not backing off after repeated failures.
        """
        sql = (
            f"SELECT q.id, q.priority, q.telegram_chat_id, q.operation, {self._empty_blob}, q.slave_id, "
            "q.required_sender_bot_id, q.created_at, "
            f"CASE WHEN q.log_context IS NULL THEN NULL ELSE {self._empty_blob} END, q.delivery_state, NULL, "
            "length(q.payload) + COALESCE(length(q.log_context), 0) + COALESCE(length(q.completion_receipt), 0), "
            "q.reconcile_after, q.thread_id, COUNT(*) OVER () "
            "FROM outbound_queue AS q "
            "WHERE q.delivery_state = 'queued' AND q.telegram_chat_id = ? "
            "AND COALESCE(q.thread_id, 0) = ?"
        )
        parameters: list[object] = [int(telegram_chat_id), int(thread_id) if thread_id is not None else 0]
        if ready_only:
            sql += " AND " + ready_sql("q") + " AND NOT " + backing_off_sql("q", "?")
            parameters.append(time.time())
        excluded = tuple(exclude_ids)
        if excluded:
            sql += " AND q.id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            parameters.extend(excluded)
        sql += " ORDER BY q.priority DESC, q.id ASC LIMIT 1"
        with self._lock:
            row = self.connection.execute(sql, parameters).fetchone()
        if row is None:
            return None
        return QueuedCall(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8],
            row[9], row[10], stored_bytes=int(row[11]), retry_after=float(row[12]),
            thread_id=row[13], waiting_rows=int(row[14]),
        )

    def _row_state(self, row_id: int) -> RowState:
        """The stored delivery state of one row, ABSENT when it does not exist."""
        with self._lock:
            row = self.connection.execute(
                "SELECT delivery_state, delivery_hold, reconcile_after FROM outbound_queue WHERE id=?",
                (row_id,),
            ).fetchone()
        return RowState.ABSENT if row is None else classify(row[0], row[1], row[2], time.time())

    def _transition(self, row_id: int, event: Event, *, dependents: bool = False) -> None:
        """Check ``event`` against the row's stored state before the caller writes.

        An event the state does not accept raises, and nothing is written. An
        accepted one marks the row's destination (and, when requested, its
        dependents' destinations) for head re-election. The caller's UPDATE
        keeps its own WHERE guard against a concurrent change.
        """
        current = self._row_state(row_id)
        if next_state(current, event) is None:
            raise IllegalDeliveryTransitionError(
                f"Queue row {row_id} is {current.value}; {event.value} is not allowed "
                "there, so the row was left unchanged."
            )
        self._mark_row_destinations_dirty(row_id, dependents=dependents)

    def _mark_row_destinations_dirty(self, row_id: int, *, dependents: bool = False) -> None:
        """Re-elect changed metadata, including dependency eligibility when requested."""
        sql = "SELECT telegram_chat_id, thread_id FROM outbound_queue WHERE id=?"
        parameters: tuple[int, ...] = (row_id,)
        if dependents:
            sql += " OR predecessor_id=?"
            parameters += (row_id,)
        for chat_id, thread_id in self.connection.execute(sql, parameters):
            self.mark_destination_dirty(chat_id, thread_id)

    def take_dirty_destinations(self) -> set[tuple[int, Optional[int]]]:
        """Destinations whose head election may have changed since the last pass."""
        with self._lock:
            dirty, self._dirty_destinations = self._dirty_destinations, set()
            return dirty

    def mark_destination_dirty(self, telegram_chat_id: int, thread_id: Optional[int]) -> None:
        self._dirty_destinations.add(
            (int(telegram_chat_id), int(thread_id) if thread_id is not None else None)
        )

    def has_waiting_rows(self) -> bool:
        """Whether any row is still waiting for delivery, dispatchable or not."""
        with self._lock:
            return self.connection.execute(
                "SELECT 1 FROM outbound_queue WHERE " + waiting_sql() + " LIMIT 1"
            ).fetchone() is not None

    def has_unresolvable_dependency(self) -> bool:
        """Whether every waiting row defers to another waiting row.

        Deferring to a row that is already delivered, held or gone resolves on its
        own, and so does deferring to a row that keeps failing: it is retried, and
        a row that needs its Telegram message waits for that retry. Deferring only
        to other waiting rows cannot: it is a cycle, and no dispatch pass can ever
        pick a head from it.
        """
        with self._lock:
            resolvable = self.connection.execute(
                "SELECT 1 FROM outbound_queue AS waiting "
                "JOIN outbound_queue AS predecessor ON predecessor.id = waiting.predecessor_id "
                "WHERE " + waiting_sql("waiting") + " AND (NOT (" + waiting_sql("predecessor") + ") OR "
                + failing_sql("predecessor") + ") LIMIT 1"
            ).fetchone()
        return resolvable is None

    def load_queued(self, row_id: int) -> QueuedCall:
        """Load upload bytes only once a worker and sender can accept this row."""
        with self._lock:
            size = self.connection.execute(
                "SELECT length(payload) + COALESCE(length(log_context), 0) + COALESCE(length(completion_receipt), 0) "
                "FROM outbound_queue WHERE id = ? AND delivery_state = 'queued'", (row_id,),
            ).fetchone()
            if size is not None:
                self.check_replay_size(row_id, size[0])
            row = self.connection.execute(
                "SELECT id, priority, telegram_chat_id, operation, payload, slave_id, "
                "required_sender_bot_id, created_at, log_context, delivery_state, completion_receipt, thread_id "
                "FROM outbound_queue WHERE id = ? AND delivery_state = 'queued'", (row_id,),
            ).fetchone()
        if row is None:
            raise QueuePersistenceError(f"Queued row {row_id} disappeared before dispatch.")
        return replace(
            QueuedCall(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
                       row[8], row[9], row[10], thread_id=row[11]),
            stored_bytes=int(size[0]),
        )

    def queued_stored_size(self, row_id: int) -> Optional[int]:
        with self._lock:
            row = self.connection.execute(
                "SELECT length(payload) + COALESCE(length(log_context), 0) + "
                "COALESCE(length(completion_receipt), 0) "
                "FROM outbound_queue WHERE id = ? AND delivery_state = 'queued'",
                (row_id,),
            ).fetchone()
        return None if row is None else int(row[0])

    def queued_payload_version(self, row_id: int) -> Optional[int]:
        with self._lock:
            row = self.connection.execute(
                "SELECT 1 FROM outbound_queue "
                "WHERE id = ? AND delivery_state = 'queued'",
                (row_id,),
            ).fetchone()
        if row is None:
            return None
        # The PostgreSQL reader runs on the shared queue session, so it must
        # not interleave with another thread's transaction there.
        shared_session = isinstance(self.connection, _PostgresqlConnection)
        with self._lock if shared_session else nullcontext(), self._payload_reader(row_id) as payload:
            marker = payload.read(1)
        return marker[0] if marker else None

    @staticmethod
    def _legacy_marker_pickle(storage_name: str) -> bytes:
        module = b"efb_telegram_master.outbound"
        name = b"_restore_legacy_inline_blob"
        encoded_name = storage_name.encode("utf-8")
        if len(encoded_name) > 255:
            raise QueuePersistenceError("Legacy media storage name is unexpectedly long.")
        return (
            b"c" + module + b"\n" + name + b"\n" +
            b"\x8c" + bytes((len(encoded_name),)) + encoded_name + b"\x85R"
        )

    def _stream_legacy_pickle(
        self, source: _BlobReader, *, max_output_bytes: Optional[int] = None,
    ) -> tuple[tuple, dict, set[str]]:
        """Replace v1 ``BytesIO`` data while retaining bounded non-media values.

        ``pickletools`` supplies the complete opcode table.  Argument sizes are
        checked before a variable payload is read, so bytes inside ordinary
        values are never mistaken for opcodes and cannot inflate recovery.
        """
        if max_output_bytes is None:
            max_output_bytes = self.MAX_REPLAY_BYTES
        output = bytearray()
        opcodes = {operation.code: operation for operation in pickletools.opcodes}
        created: set[str] = set()
        marker = object()
        other = object()
        bytesio_class = object()
        bytesio_instance = object()
        stack: list[object] = []
        memo: dict[int, object] = {}
        expecting_bytesio_content = False
        fixed_readers = {
            "uint1": 1, "uint2": 2, "uint4": 4, "int4": 4,
            "uint8": 8, "float8": 8,
        }
        sized_readers = {
            "long1": 1, "long4": 4,
            "string1": 1, "string4": 4,
            "bytes1": 1, "bytes4": 4, "bytes8": 8,
            "bytearray8": 8,
            "unicodestring1": 1, "unicodestring4": 4,
            "unicodestring8": 8,
        }
        newline_readers = {
            "decimalnl_short", "decimalnl_long", "stringnl",
            "unicodestringnl", "floatnl", "stringnl_noescape",
            "stringnl_noescape_pair",
        }

        def take(size: int) -> bytes:
            value = source.read(size)
            if len(value) != size:
                raise InvalidQueuedPayloadError("Legacy queued payload is truncated.")
            return value

        def reserve(size: int) -> None:
            if len(output) + size > max_output_bytes:
                raise OversizedQueuedPayloadError(
                    f"Legacy payload needs more than the {self.MAX_REPLAY_BYTES}-byte replay budget. "
                    "Row retained; offline recovery is required before it can be loaded safely."
                )

        def append(value: bytes) -> None:
            reserve(len(value))
            output.extend(value)

        def pop() -> object:
            return stack.pop() if stack else other

        def pop_mark() -> list[object]:
            items: list[object] = []
            while stack:
                item = stack.pop()
                if item is marker:
                    return items
                items.append(item)
            return items

        def memoize(index: int) -> None:
            memo[index] = stack[-1] if stack else other

        def copy_newline() -> list[bytes]:
            parts: list[bytes] = []
            for _ in range(2):
                part = bytearray()
                while True:
                    reserve(1)
                    character = take(1)
                    output.extend(character)
                    if character == b"\n":
                        break
                    part.extend(character)
                parts.append(bytes(part))
                if current_reader != "stringnl_noescape_pair":
                    break
            return parts

        def copy_nonmedia(prefix: bytes, size: int, *, note_unicode: bool) -> object:
            reserve(len(prefix) + size)
            output.extend(prefix)
            remembered = bytearray() if note_unicode and size <= 256 else None
            remaining = size
            while remaining:
                chunk = take(min(64 * 1024, remaining))
                output.extend(chunk)
                if remembered is not None:
                    remembered.extend(chunk)
                remaining -= len(chunk)
            return bytes(remembered) if remembered is not None else other

        try:
            while source._offset < source.length:
                opcode = take(1)
                operation = opcodes.get(opcode.decode("latin1"))
                if operation is None:
                    raise InvalidQueuedPayloadError("Legacy queued payload contains an unknown pickle opcode.")
                name = operation.name
                current_reader = operation.arg.reader.__name__.removeprefix("read_") if operation.arg else ""
                if name == "FRAME":
                    take(8)
                    continue
                if expecting_bytesio_content and name not in {
                    "SHORT_BINBYTES", "BINBYTES", "BINBYTES8", "BYTEARRAY8", "MEMOIZE",
                    "BINPUT", "LONG_BINPUT",
                }:
                    expecting_bytesio_content = False
                append(opcode)
                argument: object = None
                if current_reader in fixed_readers:
                    raw = take(fixed_readers[current_reader])
                    append(raw)
                    argument = int.from_bytes(raw, "little") if current_reader.startswith("uint") else None
                elif current_reader in sized_readers:
                    prefix_size = sized_readers[current_reader]
                    prefix = take(prefix_size)
                    size = int.from_bytes(prefix, "little")
                    if expecting_bytesio_content and name in {
                        "SHORT_BINBYTES", "BINBYTES", "BINBYTES8", "BYTEARRAY8",
                    }:
                        snapshot = self._store_media_stream(_BlobSlice(source, size), None, input_file=False)
                        assert snapshot.storage_name is not None
                        created.add(snapshot.storage_name)
                        output[-1:] = self._legacy_marker_pickle(snapshot.storage_name)
                        expecting_bytesio_content = False
                        argument = other
                    else:
                        argument = copy_nonmedia(prefix, size, note_unicode=current_reader.startswith("unicodestring"))
                elif current_reader in newline_readers:
                    argument = copy_newline()
                elif operation.arg is not None:
                    raise InvalidQueuedPayloadError("Legacy queued payload has an unsupported pickle argument.")

                if name == "MARK":
                    stack.append(marker)
                elif name in {"SHORT_BINUNICODE", "BINUNICODE", "BINUNICODE8", "UNICODE"}:
                    stack.append(argument if isinstance(argument, bytes) else other)
                elif name in {"GLOBAL", "INST"}:
                    parts = argument if isinstance(argument, list) else []
                    stack.append(bytesio_class if parts == [b"_io", b"BytesIO"] else other)
                elif name == "STACK_GLOBAL":
                    class_name, module = pop(), pop()
                    stack.append(bytesio_class if (module, class_name) == (b"_io", b"BytesIO") else other)
                elif name in {"BINGET", "LONG_BINGET", "GET", "PUT", "BINPUT", "LONG_BINPUT"}:
                    if isinstance(argument, list):
                        argument = int(argument[0])
                    if not isinstance(argument, int):
                        raise InvalidQueuedPayloadError("Legacy pickle memo index is invalid.")
                    if name in {"BINGET", "LONG_BINGET", "GET"}:
                        stack.append(memo.get(argument, other))
                    else:
                        memoize(argument)
                elif name == "MEMOIZE":
                    memoize(len(memo))
                elif name == "NEWOBJ":
                    pop()
                    cls = pop()
                    stack.append(bytesio_instance if cls is bytesio_class else other)
                    expecting_bytesio_content = cls is bytesio_class
                elif name == "NEWOBJ_EX":
                    pop(); pop()
                    cls = pop()
                    stack.append(bytesio_instance if cls is bytesio_class else other)
                    expecting_bytesio_content = cls is bytesio_class
                elif name == "OBJ":
                    values = pop_mark()
                    cls = values[-1] if values else other
                    stack.append(bytesio_instance if cls is bytesio_class else other)
                    expecting_bytesio_content = cls is bytesio_class
                elif name == "BUILD":
                    pop()
                elif name in {"TUPLE", "LIST", "DICT", "FROZENSET"}:
                    pop_mark(); stack.append(other)
                elif name in {"TUPLE1", "TUPLE2", "TUPLE3"}:
                    for _ in range(int(name[-1])): pop()
                    stack.append(other)
                elif name in {"EMPTY_TUPLE", "EMPTY_LIST", "EMPTY_DICT", "EMPTY_SET"}:
                    stack.append(other)
                elif name in {"REDUCE", "RREDUCE"}:
                    pop(); pop(); stack.append(other)
                elif name == "APPEND":
                    pop()
                elif name in {"APPENDS", "SETITEMS", "ADDITEMS"}:
                    pop_mark()
                elif name == "SETITEM":
                    pop(); pop()
                elif name == "DUP":
                    stack.append(stack[-1] if stack else other)
                elif name == "POP":
                    pop()
                elif name == "POP_MARK":
                    pop_mark()
                elif name in {"PERSID", "BINPERSID", "EXT1", "EXT2", "EXT4"}:
                    if name == "BINPERSID": pop()
                    stack.append(other)
                elif name not in {"PROTO", "STOP", "NEXT_BUFFER", "READONLY_BUFFER"}:
                    stack.append(other)
            value = _LegacyRecoveryUnpickler(io.BytesIO(output)).load()
            if not isinstance(value, tuple) or len(value) != 2:
                raise InvalidQueuedPayloadError("Queued payload has an invalid outer shape.")
            args, kwargs = value
            if not isinstance(args, tuple) or not isinstance(kwargs, dict):
                raise InvalidQueuedPayloadError("Queued payload has invalid arguments.")
            return args, kwargs, created
        except Exception:
            self._cleanup_created_media(created)
            raise

    def _replace_legacy_media_references(
        self, value: object, memo: Optional[dict[int, object]] = None,
    ) -> object:
        """Replace streamed legacy objects everywhere they are aliased in a pickle."""
        if memo is None:
            memo = {}
        cached = memo.get(id(value))
        if cached is not None:
            return cached
        if isinstance(value, _LegacyInlineBlob):
            snapshot = _StoredMediaSnapshot(value.storage_name, None)
            memo[id(value)] = snapshot
            return snapshot
        if isinstance(value, _LegacyBytesIO):
            memo[id(value)] = value.snapshot
            return value.snapshot
        if isinstance(value, tuple):
            restored = tuple(self._replace_legacy_media_references(item, memo) for item in value)
            memo[id(value)] = restored
            return restored
        if isinstance(value, list):
            restored_list: list[object] = []
            memo[id(value)] = restored_list
            restored_list.extend(self._replace_legacy_media_references(item, memo) for item in value)
            return restored_list
        if isinstance(value, dict):
            restored_dict: dict[object, object] = {}
            memo[id(value)] = restored_dict
            restored_dict.update(
                (key, self._replace_legacy_media_references(item, memo))
                for key, item in value.items()
            )
            return restored_dict
        if isinstance(value, InputMedia):
            restored_media = copy.copy(value)
            memo[id(value)] = restored_media
            object.__setattr__(
                restored_media, "media", self._replace_legacy_media_references(value.media, memo)
            )
            for field in _INPUT_MEDIA_ATTACHMENT_FIELDS.get(type(value), ()):
                attachment = getattr(value, field, None)
                if attachment is not None:
                    object.__setattr__(
                        restored_media, field,
                        self._replace_legacy_media_references(attachment, memo),
                    )
            return restored_media
        return value

    def recover_legacy_media_payload(self, row_id: int) -> QueuedCall:
        """Compact one v1 media row without ever loading its inline BLOB."""
        with self._lock:
            created: set[str] = set()
            try:
                # A reserved writer transaction keeps the metadata snapshot and
                # incremental BLOB read coherent with the guarded replacement.
                self.connection.execute("BEGIN IMMEDIATE")
                metadata_size = self.connection.execute(
                    "SELECT COALESCE(length(log_context), 0) + COALESCE(length(completion_receipt), 0) "
                    "FROM outbound_queue WHERE id = ? AND delivery_state = 'queued'" + self._lock_row_sql,
                    (row_id,),
                ).fetchone()
                if metadata_size is None:
                    raise QueuePersistenceError(f"Queued row {row_id} disappeared before recovery.")
                self.check_replay_size(row_id, int(metadata_size[0]))
                metadata = self.connection.execute(
                    "SELECT id, priority, telegram_chat_id, operation, slave_id, required_sender_bot_id, "
                    "created_at, log_context, delivery_state, completion_receipt "
                    "FROM outbound_queue WHERE id = ? AND delivery_state = 'queued'", (row_id,),
                ).fetchone()
                if metadata is None:
                    raise QueuePersistenceError(f"Queued row {row_id} disappeared before recovery.")
                with self._payload_reader(row_id) as source:
                    if source.read(1) != b"\x01":
                        raise QueuePersistenceError(f"Queued row {row_id} is not a legacy v1 payload.")
                    metadata_bytes = len(metadata[7] or b"") + len(metadata[9] or b"")
                    self.check_replay_size(row_id, metadata_bytes)
                    args, kwargs, created = self._stream_legacy_pickle(
                        source, max_output_bytes=self.MAX_REPLAY_BYTES - metadata_bytes,
                    )
                restored: dict[int, object] = {}
                restored_args = self._replace_legacy_media_references(args, restored)
                restored_kwargs = self._replace_legacy_media_references(kwargs, restored)
                assert isinstance(restored_args, tuple) and isinstance(restored_kwargs, dict)
                operation = str(metadata[3])
                normalized_args, normalized_kwargs = self._normalize_direct_media(
                    operation, restored_args, restored_kwargs, frozenset(), created
                )
                normalized_args, normalized_kwargs = self._normalize_nested_media(
                    operation, normalized_args, normalized_kwargs, frozenset(), created
                )
                normalized_kwargs = self._normalize_thumbnail(
                    operation, normalized_kwargs, frozenset(), created
                )
                normalized_kwargs = self._normalize_keyword_media(
                    operation, normalized_kwargs, frozenset(), created
                )
                payload = self.encode_payload(normalized_args, normalized_kwargs)
                references = self._media_reference_keys(payload)
                self._cleanup_created_media(
                    created - {name for kind, name in references if kind == "stored"}
                )
                cursor = self.connection.execute(
                    f"UPDATE outbound_queue SET payload = ? WHERE id = ? AND delivery_state = '{QUEUED}' "
                    "AND created_at = ?", (payload, row_id, metadata[6]),
                )
                if cursor.rowcount != 1:
                    raise QueuePersistenceError(
                        f"Queued row {row_id} changed before legacy recovery could commit."
                    )
                self.connection.commit()
            except Exception:
                try:
                    self.connection.rollback()
                except self._database_errors:
                    pass
                self._cleanup_created_media(created)
                raise
        return QueuedCall(
            int(metadata[0]), int(metadata[1]), int(metadata[2]), operation, payload, metadata[4], metadata[5],
            float(metadata[6]), metadata[7], str(metadata[8]), metadata[9],
            len(payload) + len(metadata[7] or b"") + len(metadata[9] or b""),
        )

    def check_replay_size(self, row_id: int, size: int) -> None:
        if size > self.MAX_REPLAY_BYTES:
            raise OversizedQueuedPayloadError(
                f"Queue row {row_id} needs {size} encoded bytes, exceeding the "
                f"{self.MAX_REPLAY_BYTES}-byte replay budget. Row retained; "
                "offline recovery is required before it can be loaded safely."
            )

    def destination_snapshot(self, limit: int) -> list[tuple[str, int, float]]:
        """Return ranked queue destinations without exposing Telegram chat IDs."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT telegram_chat_id, COUNT(*), MIN(created_at) FROM outbound_queue "
                "GROUP BY telegram_chat_id "
                "ORDER BY COUNT(*) DESC, telegram_chat_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        now = time.time()
        return [
            (f"rank_{rank}", int(depth), max(0.0, now - float(oldest_created_at)))
            for rank, (_chat_id, depth, oldest_created_at) in enumerate(rows, start=1)
        ]

    def sent_pending(
        self, *, due_before: Optional[float] = None, limit: int = -1,
        row_id: Optional[int] = None,
    ) -> list[QueuedCall]:
        return list(self.iter_sent_pending(due_before=due_before, limit=limit, row_id=row_id))

    def iter_sent_pending(
        self, *, due_before: Optional[float] = None, limit: int = -1,
        row_id: Optional[int] = None,
    ):
        filters = [f"delivery_state = '{SENT_PENDING}'"]
        parameters: list[object] = []
        if due_before is not None:
            filters.append("reconcile_after <= ?")
            parameters.append(due_before)
        if row_id is not None:
            filters.append("id = ?")
            parameters.append(row_id)
        # A negative limit means all rows; PostgreSQL rejects a negative LIMIT.
        limit_sql = ""
        if limit >= 0:
            limit_sql = " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            # Read IDs first: 32 historical contexts can themselves occupy GBs.
            identifiers = self.connection.execute(
                "SELECT id, COALESCE(length(log_context), 0) + COALESCE(length(completion_receipt), 0) "
                "FROM outbound_queue WHERE " + " AND ".join(filters) +
                " ORDER BY reconcile_after, id" + limit_sql, parameters,
            ).fetchall()
        for identifier, size in identifiers:
            self.check_replay_size(identifier, size)
            with self._lock:
                row = self.connection.execute(
                    f"SELECT id, priority, telegram_chat_id, operation, {self._empty_blob}, slave_id, "
                    "required_sender_bot_id, created_at, log_context, delivery_state, completion_receipt "
                    f"FROM outbound_queue WHERE id = ? AND delivery_state = '{SENT_PENDING}'",
                    (identifier,),
                ).fetchone()
            if row is not None:
                yield QueuedCall(*row)
            del row

    def next_reconciliation_time(self) -> Optional[float]:
        with self._lock:
            return self.connection.execute(
                f"SELECT MIN(reconcile_after) FROM outbound_queue WHERE delivery_state = '{SENT_PENDING}'"
            ).fetchone()[0]

    def defer_reconciliation(self, row_ids: list[int], now: float) -> None:
        """Persist exponential retry delays (1s to 60s), including across restarts."""
        if not row_ids:
            return
        with self._lock:
            deferred = []
            for row_id in row_ids:
                state = self._row_state(row_id)
                if next_state(state, Event.DEFER_RECONCILIATION) is None:
                    logging.getLogger(__name__).warning(
                        "Queue row %s is %s; its receipt reconciliation was not deferred.",
                        row_id, state.value,
                    )
                else:
                    deferred.append(row_id)
            if not deferred:
                return
            try:
                self.connection.execute("BEGIN")
                self.connection.executemany(
                    "UPDATE outbound_queue SET "
                    "reconcile_after = ? + CASE WHEN reconcile_attempts >= 6 THEN 60 "
                    "ELSE 1 << reconcile_attempts END, "
                    "reconcile_attempts = CASE WHEN reconcile_attempts >= 6 THEN 7 "
                    "ELSE reconcile_attempts + 1 END "
                    "WHERE id = ? AND delivery_state = ?",
                    [(now, row_id, SENT_PENDING) for row_id in deferred],
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def begin_delivery_attempt(self, row_id: int, selection: SenderSelection) -> None:
        """Persist before submission: an interrupted send is not safe to replay."""
        with self._lock, self.connection:
            frozen = self.connection.execute(
                "SELECT 1 FROM outbound_queue WHERE id=? AND delivery_hold=? "
                "AND substr(log_context, 1, 1)=?", (row_id, HoldKind.IN_FLIGHT.value, b"\x03"),
            ).fetchone()
            if frozen is not None:
                # Materializing an aggregation row already recorded this attempt.
                self._mark_row_destinations_dirty(row_id)
                return
            self._transition(row_id, Event.BEGIN_ATTEMPT)
            cursor = self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=?, attempt_sender_bot_id=?, "
                "attempt_started_at=? WHERE id=? AND " + waiting_sql(),
                (HoldKind.IN_FLIGHT.value, selection.sender_bot_id, time.time(), row_id),
            )
            if cursor.rowcount != 1:
                raise QueuePersistenceError(f"Queue row {row_id} is not available for a send attempt.")

    def release_delivery_attempt(self, row_id: int) -> None:
        """Use only when submission failed or Telegram definitely rejected the call.

        A row that is not in flight is left unchanged and reported: releasing
        it would clear an operator hold or rewind a delivered aggregate.
        """
        from .queued_log import decode_aggregation, encode_aggregation
        with self._lock, self.connection:
            try:
                self._transition(row_id, Event.RELEASE_ATTEMPT)
            except IllegalDeliveryTransitionError as error:
                logging.getLogger(__name__).warning("Send attempt not released: %s", error)
                return
            row = self.connection.execute("SELECT log_context FROM outbound_queue WHERE id=?", (row_id,)).fetchone()
            context = decode_aggregation(row[0]) if row and row[0] else None
            if context is not None:
                try:
                    check_context_transition(row_id, context, ContextEvent.RELEASE,
                                             context.get("retry_context") or context)
                except IllegalAggregationTransitionError as error:
                    logging.getLogger(__name__).warning("Send attempt not released: %s", error)
                    return
            if context and context.get("retry_context"):
                logical = context["retry_context"]
                self.connection.execute(
                    "UPDATE outbound_queue SET operation='send_message', payload=?, log_context=?, "
                    "required_sender_bot_id=NULL WHERE id=?",
                    (self.encode_payload((), {"chat_id": logical["key"][1], "text": ""}),
                     encode_aggregation(logical), row_id),
                )
            self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=NULL WHERE id=? AND delivery_state=? AND delivery_hold=?",
                (row_id, QUEUED, HoldKind.IN_FLIGHT.value),
            )

    def _hold(self, row_id: int, event: Event, hold: str, reason: str | None = None) -> None:
        """Retain a row under ``hold``; it stops being an ordering barrier for its dependents.

        ``reason`` is Telegram's error description, kept for the operator.
        """
        with self._lock, self.connection:
            self._transition(row_id, event, dependents=True)
            cursor = self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=?, hold_reason=? WHERE id=? AND delivery_state=?",
                (hold, reason, row_id, QUEUED),
            )
            if cursor.rowcount != 1:
                raise QueuePersistenceError(f"Queue row {row_id} disappeared before it could be held as {hold}.")

    def hold_uncertain_delivery(self, row_id: int, error: BaseException) -> None:
        self._hold(row_id, Event.HOLD_UNCERTAIN,
                   format_hold(HoldKind.UNCERTAIN, error_detail(error, with_cause=True)),
                   telegram_error_text(error))

    @classmethod
    def edited_message_id(cls, operation: str, payload) -> int | None:
        try:
            args, kwargs = cls.decode_payload_raw(bytes(payload))
        except InvalidQueuedPayloadError:
            return None
        message_id = kwargs.get("message_id")
        # Where ``message_id`` sits when it is passed positionally.
        index = 2 if operation == "edit_message_text" else 1
        if message_id is None and len(args) > index:
            message_id = args[index]
        return message_id if isinstance(message_id, int) else None

    def edit_unconfirmed(self, row_id: int) -> bool:
        """Whether Telegram did not confirm an earlier try of this edit."""
        with self._lock:
            row = self.connection.execute(
                "SELECT unconfirmed_at FROM outbound_queue WHERE id=?", (row_id,)
            ).fetchone()
        return row is not None and row[0] is not None

    def unconfirmed_edit_superseded(self, row_id: int) -> str | None:
        """Why an edit whose result is unknown is dropped instead of tried again, or None.

        It is dropped when Telegram did not confirm an earlier try of it either,
        or when a later row edits the same message with its complete text:
        replaying the old edit after that would put old text over new. A later
        combined-message update is not such an edit; it is rendered from the
        confirmed combined message and would not carry this edit's content.
        """
        from .queued_log import decode_aggregation
        with self._lock:
            row = self.connection.execute(
                "SELECT telegram_chat_id, operation, payload, unconfirmed_at FROM outbound_queue WHERE id=?",
                (row_id,),
            ).fetchone()
            if row is None or row[1] not in EDIT_OPERATIONS:
                return None
            if row[3] is not None:
                return "Telegram did not confirm its earlier try either"
            message_id = self.edited_message_id(row[1], row[2])
            if message_id is None:
                return None
            later = self.connection.execute(
                "SELECT operation, payload, log_context FROM outbound_queue WHERE telegram_chat_id=? AND id>? "
                "AND operation IN (?, ?) AND length(payload)<=? ORDER BY id",
                (row[0], row_id, *sorted(EDIT_OPERATIONS), self.MAX_INSPECT_BYTES),
            ).fetchall()
        for operation, payload, context in later:
            if self.edited_message_id(operation, payload) != message_id:
                continue
            kind = (decode_aggregation(bytes(context)) or {}).get("kind") if context else None
            if kind not in {ContextKind.LOGICAL, ContextKind.MEMBER_UPDATE, ContextKind.AGGREGATE}:
                return "a newer edit of the same message is queued"
        return None

    def hold_invalid_payload(self, row_id: int) -> None:
        """Retain undecodable work while allowing later rows to dispatch."""
        self._hold(row_id, Event.HOLD_INVALID_PAYLOAD, format_hold(HoldKind.INVALID_PAYLOAD))

    def hold_failed_history(self, row_id: int, error: BaseException, *, supplement: bool = False) -> None:
        """Keep rejected history or supplemental work available for repair."""
        if supplement:
            self._hold(row_id, Event.HOLD_FAILED_SUPPLEMENT,
                       format_hold(HoldKind.SUPPLEMENT_FAILED, error_detail(error)), telegram_error_text(error))
        else:
            self._hold(row_id, Event.HOLD_FAILED_HISTORY,
                       format_hold(HoldKind.HISTORY_FAILED, error_detail(error)), telegram_error_text(error))

    def resend_uncertain(self, row_id: int) -> None:
        """Send an uncertain row again: its chat shows it never arrived. Only the hold is cleared."""
        with self._lock, self.connection:
            self._transition(row_id, Event.RESEND, dependents=True)
            cursor = self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=NULL WHERE id=? AND delivery_state=? AND delivery_hold LIKE ?",
                (row_id, QUEUED, HoldKind.UNCERTAIN.value + "%"),
            )
            if cursor.rowcount != 1:
                raise QueuePersistenceError(f"Queue row {row_id} changed before it could be sent again.")

    def discard_held(self, row_id: int) -> list[QueuedCall]:
        """Remove a held row that will never be sent, and the container edits that need it.

        A held or waiting edit of a container that does not exist yet, ordered
        behind a removed row, is removed with it: it cannot be sent without the
        container. A queue-owned media file is deleted only when no remaining row
        names it; external media files are never deleted. Returns the removed
        rows, the held row first.
        """
        from .queued_log import decode_aggregation
        columns = ("id, priority, telegram_chat_id, operation, payload, slave_id, required_sender_bot_id, "
                   "created_at, log_context, delivery_state, completion_receipt, predecessor_id, thread_id")
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self._transition(row_id, Event.DISCARD, dependents=True)
                removed: list[QueuedCall] = []
                pending = [row_id]
                while pending:
                    current = pending.pop()
                    row = self.connection.execute(
                        f"SELECT {columns} FROM outbound_queue WHERE id=?", (current,)).fetchone()
                    removed.append(QueuedCall(
                        row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9], row[10],
                        predecessor_id=row[11], thread_id=row[12]))
                    for child, state, hold, retry_after, context in self.connection.execute(
                        "SELECT id, delivery_state, delivery_hold, reconcile_after, log_context "
                        "FROM outbound_queue WHERE predecessor_id=? AND id!=? ORDER BY id", (current, current),
                    ).fetchall():
                        child_state = classify(state, hold, retry_after or 0.0, time.time())
                        if child_state not in (RowState.WAITING, RowState.DEFERRED) and next_state(
                                child_state, Event.DISCARD) is None:
                            continue
                        try:
                            own = decode_aggregation(bytes(context)) if context is not None else None
                        except Exception:  # noqa: BLE001 - a damaged context is not a container edit
                            own = None
                        if own and own.get("kind") == ContextKind.MEMBER_UPDATE and not own.get("container_id"):
                            self._mark_row_destinations_dirty(child, dependents=True)
                            pending.append(child)
                ids = [row.id for row in removed]
                self.connection.executemany("DELETE FROM outbound_queue WHERE id=?", [(i,) for i in ids])
                sidecars: set[str] = set()
                for row in removed:
                    try:
                        sidecars.update(name for kind, name in self._media_reference_keys(bytes(row.payload))
                                        if kind == "stored")
                    except InvalidQueuedPayloadError:
                        pass  # Its file names cannot be read: its files are kept.
                if sidecars:
                    sidecars = self._unnamed_sidecars(sidecars)
                self.connection.commit()
            except Exception:
                try:
                    self.connection.rollback()
                except self._database_errors:
                    pass
                raise
        for name in sidecars:
            try:
                (self.media_dir / name).unlink()
            except OSError:
                pass
        return removed

    def _unnamed_sidecars(self, names: set[str]) -> set[str]:
        """The names among ``names`` that no stored row's payload contains; none when a row cannot be read."""
        for row_id, version, size in self.connection.execute(
            "SELECT id, substr(payload, 1, 1), length(payload) FROM outbound_queue"
        ).fetchall():
            if bytes(version) == b"\x01":
                continue  # Older rows keep their media inline, not in sidecars.
            if bytes(version) != b"\x02" or size > self.MAX_INSPECT_BYTES:
                return set()
            payload = bytes(self.connection.execute(
                "SELECT payload FROM outbound_queue WHERE id=?", (row_id,)).fetchone()[0])
            names = {name for name in names if name.encode() not in payload}
            if not names:
                break
        return names

    def defer_acquisition(self, row_id: int, minimum_delay: float = 0, *, counted: bool = True) -> float:
        """Persist capped backoff for an acquisition that never attempted a send.

        ``counted`` False keeps the failed-attempt count: a flood wait is not a
        failure of this row.
        """
        with self._lock, self.connection:
            self._transition(row_id, Event.DEFER_ACQUISITION)
            attempts = self.connection.execute(
                "SELECT reconcile_attempts FROM outbound_queue WHERE id=?", (row_id,)
            ).fetchone()[0]
            delay = max(min(60, 2 ** min(attempts, 6)), minimum_delay)
            updated = min(attempts + 1, 6) if counted else attempts
            self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=NULL, reconcile_attempts=?, reconcile_after=? "
                "WHERE id=? AND delivery_state=?",
                (updated, time.time() + delay, row_id, QUEUED),
            )
            self._note_failed_attempts(row_id, attempts, updated)
        return float(delay)

    def defer_failed_send(self, row_id: int, retry_at: float, *, unconfirmed: bool = False) -> None:
        """Count a send that failed with a retryable error; retry at ``retry_at`` (Unix time).

        The deadline is stored once the row is failing. ``unconfirmed`` records
        that Telegram may have applied this try (an edit whose result is unknown).

        Flood waits are not counted: they hold back the whole chat, not this row.
        A row that is not waiting (its attempt was not released) is left
        unchanged and reported.
        """
        with self._lock, self.connection:
            try:
                self._transition(row_id, Event.DEFER_FAILED_SEND)
            except IllegalDeliveryTransitionError as error:
                logging.getLogger(__name__).warning("Failed send not counted: %s", error)
                return
            attempts = self.connection.execute(
                "SELECT reconcile_attempts FROM outbound_queue WHERE id=?", (row_id,)
            ).fetchone()[0]
            updated = min(attempts + 1, 6)
            # Below the limit the row stays its destination's head and the
            # scheduler's own deadline paces it; only a failing row's backoff is
            # stored, so head election can step past it.
            self.connection.execute(
                "UPDATE outbound_queue SET reconcile_attempts=?, "
                "reconcile_after=COALESCE(?, reconcile_after), unconfirmed_at=COALESCE(?, unconfirmed_at) "
                "WHERE id=? AND " + waiting_sql(),
                (updated, retry_at if updated >= FAILED_ATTEMPTS_LIMIT else None,
                 time.time() if unconfirmed else None, row_id),
            )
            self._note_failed_attempts(row_id, attempts, updated)

    def _note_failed_attempts(self, row_id: int, before: int, after: int) -> None:
        """Report the attempt that made a row stop deferring the rows after it."""
        if before >= FAILED_ATTEMPTS_LIMIT or after < FAILED_ATTEMPTS_LIMIT:
            return
        self._mark_row_destinations_dirty(row_id, dependents=True)
        chat_id = self.connection.execute(
            "SELECT telegram_chat_id FROM outbound_queue WHERE id=?", (row_id,)
        ).fetchone()[0]
        logging.getLogger(__name__).warning(
            "Queue row %s for chat %s failed %d attempts in a row; later rows no longer wait "
            "for it. It keeps retrying and is delivered late if a retry succeeds.",
            row_id, chat_id, after,
        )

    def failing_predecessor(self, row_id: int) -> tuple[int, bytes | None] | None:
        """The predecessor ``row_id`` no longer waits for because it keeps failing.

        Returns its id and stored context, or None when the row's predecessor is
        missing, delivered, held or still an ordering barrier.
        """
        with self._lock:
            row = self.connection.execute(
                "SELECT predecessor.id, predecessor.log_context FROM outbound_queue AS dependent "
                "JOIN outbound_queue AS predecessor ON predecessor.id = dependent.predecessor_id "
                "WHERE dependent.id = ? AND predecessor.id != dependent.id AND " + failing_sql("predecessor"),
                (row_id,),
            ).fetchone()
        return None if row is None else (int(row[0]), row[1])

    def still_waiting_for_failing(self, row_ids: Iterable[int]) -> set[int]:
        """Those of ``row_ids`` that are waiting and whose predecessor still keeps failing."""
        identifiers = tuple(row_ids)
        if not identifiers:
            return set()
        with self._lock:
            return {
                int(row[0])
                for row in self.connection.execute(
                    "SELECT dependent.id FROM outbound_queue AS dependent "
                    "JOIN outbound_queue AS predecessor ON predecessor.id = dependent.predecessor_id "
                    "WHERE dependent.id IN (" + ",".join("?" for _ in identifiers) + ") "
                    "AND predecessor.id != dependent.id AND " + waiting_sql("dependent") +
                    " AND " + failing_sql("predecessor"),
                    identifiers,
                )
            }

    def failing_retries(self) -> list[tuple[int, int, int | None, float]]:
        """Failing rows waiting for a retry: id, chat, topic and Unix retry time."""
        with self._lock:
            return [
                (int(row[0]), int(row[1]), row[2], float(row[3]))
                for row in self.connection.execute(
                    "SELECT id, telegram_chat_id, thread_id, reconcile_after FROM outbound_queue "
                    "WHERE delivery_state = ? AND reconcile_after > 0 AND delivery_hold IS NULL "
                    "AND reconcile_attempts >= ?",
                    (QUEUED, FAILED_ATTEMPTS_LIMIT),
                )
            ]

    def record_telegram_completion(
        self, row_id: int, receipt: bytes, *, supplement: Optional[QueueRequest] = None,
        operation_resolver: Optional[Callable[[str], Callable[..., object]]] = None,
        operator_confirmed: bool = False,
    ) -> None:
        """Save Telegram's receipt; ``operator_confirmed`` resolves a held row from an observed message."""
        if not isinstance(receipt, bytes):
            raise QueuePersistenceError("Queued Telegram completion receipt must be bytes.")
        with self._lock:
            self.connection  # Preparation must finish before ownership is released.
            prepared = None
            if supplement is not None:
                assert operation_resolver is not None
                prepared = self._prepare(supplement, operation_resolver(supplement.operation))
            try:
                self.connection.execute("BEGIN")
                self._transition(
                    row_id, Event.OPERATOR_CONFIRM if operator_confirmed else Event.RECORD_COMPLETION,
                    dependents=True,
                )
                from .queued_log import decode_aggregation, encode_aggregation
                parent_context = self.connection.execute("SELECT log_context FROM outbound_queue WHERE id=?", (row_id,)).fetchone()[0]
                parent = decode_aggregation(parent_context) if parent_context else None
                check_context_transition(row_id, parent, ContextEvent.RECEIPT)
                if prepared is not None:
                    operation, _args, _kwargs, chat_id, priority, slave_id, required_sender, payload, log_context, _created_media = prepared
                    if parent is not None:
                        log_context = encode_aggregation(dict(format_version=1, kind=ContextKind.BOUNDARY, key=parent["key"],
                            generation=parent.get("generation", 0), recent=parent.get("recent", []),
                            last_new=parent.get("last_new", 0), members=[], legacy_context=log_context))
                    supplement_id = self._insert_row(
                        "INSERT INTO outbound_queue "
                        "(priority, telegram_chat_id, operation, payload, slave_id, required_sender_bot_id, "
                        "created_at, log_context, predecessor_id, thread_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (priority, chat_id, operation, payload, slave_id, required_sender, time.time(), log_context,
                         row_id if parent is not None else None,
                         queued_thread_id(_kwargs, log_context)),
                    )
                    self.mark_destination_dirty(chat_id, queued_thread_id(_kwargs, log_context))
                    if parent is not None:
                        parent["supplement_id"] = supplement_id
                        self.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE predecessor_id=? AND id!=?",
                            (supplement_id, row_id, supplement_id))
                        self.connection.execute("UPDATE outbound_queue SET log_context=? WHERE id=?", (encode_aggregation(parent), row_id))
                cursor = self.connection.execute(
                    "UPDATE outbound_queue SET delivery_state = ?, completion_receipt = ?, delivery_hold = NULL, "
                    "reconcile_after=0, reconcile_attempts=0 "
                    "WHERE id = ? AND delivery_state = ?",
                    (SENT_PENDING, receipt, row_id, QUEUED),
                )
                if cursor.rowcount != 1:
                    raise QueuePersistenceError(f"Queued row {row_id} cannot record Telegram completion.")
                self.connection.commit()
            except Exception:
                try:
                    self.connection.rollback()
                except self._database_errors:
                    pass
                if prepared is not None:
                    self._cleanup_created_media(prepared[9])
                raise
            if prepared is not None:
                self.observe_metrics(lambda metrics: metrics.record_enqueued(prepared[4], prepared[0]))
                self.refresh_depth()

    def record_history_fallback(self, row_id: int, operation: Optional[str]) -> None:
        """Record which history RPC may have been accepted, before making that RPC."""
        with self._lock:
            row = self.load_queued(row_id)
            args, kwargs = self.decode_payload_raw(row.payload)
            replay = dict(kwargs[HISTORY_REPLAY_KEY])
            if operation is None:
                replay.pop("attempted_fallback", None)
            else:
                replay["attempted_fallback"] = operation
            kwargs[HISTORY_REPLAY_KEY] = replay
            self.retarget(row_id, row.telegram_chat_id, args, kwargs)

    def retarget(self, row_id: int, new_chat_id: int, args: tuple, kwargs: dict) -> None:
        """Persist queued arguments and destination without changing durable media references."""
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                existing = self.connection.execute(
                    f"SELECT payload FROM outbound_queue WHERE id = ? AND delivery_state = '{QUEUED}'",
                    (row_id,),
                ).fetchone()
                if existing is None:
                    raise QueuePersistenceError(f"Queued row {row_id} cannot be retargeted.")
                old_payload = existing[0]
                if not old_payload or old_payload[0] not in (1, 2):
                    raise QueuePersistenceError(f"Queued row {row_id} has an unknown payload version.")
                try:
                    new_payload = bytes((old_payload[0],)) + pickle.dumps((args, kwargs), protocol=5)
                except Exception as encode_error:
                    raise QueuePersistenceError(
                        f"Queued row {row_id} retarget payload cannot be encoded."
                    ) from encode_error
                if self._media_reference_keys(old_payload) != self._media_reference_keys(new_payload):
                    raise QueuePersistenceError(
                        f"Queued row {row_id} retarget attempted to change durable media references."
                    )
                self._mark_row_destinations_dirty(row_id)
                cursor = self.connection.execute(
                    "UPDATE outbound_queue SET telegram_chat_id = ?, payload = ? "
                    f"WHERE id = ? AND delivery_state = '{QUEUED}'",
                    (new_chat_id, new_payload, row_id),
                )
                if cursor.rowcount != 1:
                    raise QueuePersistenceError(f"Queued row {row_id} cannot be retargeted.")
                self._mark_row_destinations_dirty(row_id)
                self.connection.commit()
            except Exception as error:
                try:
                    self.connection.rollback()
                except self._database_errors:
                    pass
                if isinstance(error, QueuePersistenceError):
                    raise
                raise QueuePersistenceError(
                    f"Queued row {row_id} retarget persistence failed."
                ) from error

    def delete(self, row_id: int, *, cleanup_media: bool = True, successful: bool = False) -> None:
        payload: Optional[bytes] = None
        with self._lock:
            try:
                if cleanup_media:
                    # One read answers existence, payload version and media cleanup
                    # for the ordinary case. Payloads are small: media lives in
                    # sidecars. Only a row too large to hold in memory falls back to
                    # the streaming blob read, which costs a second connection.
                    row = self.connection.execute(
                        "SELECT payload FROM outbound_queue WHERE id = ? AND length(payload) <= ?",
                        (row_id, self.MAX_INSPECT_BYTES),
                    ).fetchone()
                    if row is not None:
                        if bytes(row[0][:1]) == b"\x02":
                            payload = row[0]
                    elif self.connection.execute(
                        "SELECT 1 FROM outbound_queue WHERE id = ?", (row_id,)
                    ).fetchone() is not None:
                        with self._payload_reader(row_id) as blob:
                            if blob.read(1) == b"\x02":
                                payload = self.connection.execute(
                                    "SELECT payload FROM outbound_queue WHERE id = ?", (row_id,)
                                ).fetchone()[0]
                self._transition(row_id, Event.REMOVE, dependents=True)
                self.connection.execute("BEGIN")
                if successful:
                    self.connection.execute("UPDATE outbound_queue SET predecessor_id=NULL WHERE predecessor_id=?", (row_id,))
                cursor = self.connection.execute("DELETE FROM outbound_queue WHERE id = ?", (row_id,))
                if cursor.rowcount != 1:
                    raise QueuePersistenceError(f"Queued row {row_id} disappeared before deletion.")
                self.connection.commit()
            except Exception:
                try:
                    self.connection.rollback()
                except self._database_errors:
                    pass
                raise
        if payload is not None:
            self.cleanup_payload_media(payload)

    def complete_waiter(self, row_id: int, result: object, selection: SenderSelection) -> None:
        waiter = self.waiters.pop(row_id, None)
        if waiter is not None and not waiter.done():
            setattr(waiter, "sender_bot_id", selection.sender_bot_id)
            waiter.set_result(result)

    def fail_waiter(self, row_id: int, error: BaseException) -> None:
        waiter = self.waiters.pop(row_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_exception(error)

    def fail_all_waiters(self, error: BaseException) -> None:
        for row_id in tuple(self.waiters):
            self.fail_waiter(row_id, _fresh_exception(error))


@final
class ReservedSlot:
    """One worker slot reserved for a single dispatch attempt.

    ``own(row_id)`` hands the slot to the row that was submitted; leaving the
    ``with`` block without that releases it, on every exit path.
    """

    __slots__ = ("_settled", "_slots")

    def __init__(self, slots: WorkerSlots) -> None:
        self._slots = slots
        self._settled = False

    def own(self, row_id: int) -> None:
        if self._settled:
            self._slots.report("own", row_id)
            return
        # A rejected ownership leaves the reservation for the block's exit to release.
        self._settled = self._slots._own(row_id)

    def release(self) -> None:
        if not self._settled:
            self._settled = True
            self._slots._release_reservation()

    def __enter__(self) -> ReservedSlot:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


class WorkerSlots:
    """Outbound worker capacity: one slot per call that may run at once.

    A slot is free, reserved (``reserve``: a pass is preparing one row), or
    owned by an in-flight row (``ReservedSlot.own``). An owned slot is freed
    by ``release(row_id)`` when its row leaves ``in_flight``; a reservation is
    freed when its block ends unowned. A release or ownership that does not
    match a reservation is reported and ignored, never applied.
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._reserved = 0
        self._owners: set[int] = set()
        self._lock = threading.Lock()

    @property
    def free(self) -> int:
        with self._lock:
            return self.capacity - self._reserved - len(self._owners)

    def reserve(self) -> ReservedSlot | None:
        """Reserve a free slot, or return ``None`` when every slot is taken."""
        with self._lock:
            if self._reserved + len(self._owners) >= self.capacity:
                return None
            self._reserved += 1
        return ReservedSlot(self)

    def release(self, row_id: int) -> None:
        """Free the slot an in-flight row owned."""
        with self._lock:
            if row_id in self._owners:
                self._owners.discard(row_id)
                return
        self.report("release", row_id)

    def reset(self) -> None:
        """Free every slot; the backstop for slots that nothing can release."""
        with self._lock:
            self._reserved = 0
            self._owners.clear()

    def report(self, event: str, row_id: int | None) -> None:
        logging.getLogger(__name__).error(
            "Outbound worker slot %s for queue row %s has no matching reservation; ignored.",
            event, row_id,
        )

    def _own(self, row_id: int) -> bool:
        with self._lock:
            if self._reserved > 0 and row_id not in self._owners:
                self._reserved -= 1
                self._owners.add(row_id)
                return True
        self.report("own", row_id)
        return False

    def _release_reservation(self) -> None:
        with self._lock:
            if self._reserved > 0:
                self._reserved -= 1
                return
        self.report("release", None)


class OutboundQueueScheduler:
    """Schedule one in-flight call per chat and retain eventual rows through retries."""

    # A pass that leaves runnable rows behind without a retry deadline reschedules
    # itself instead of waiting for an event that may never arrive.
    STALL_RESCAN_SECONDS = 30.0
    # A full sender bucket frees one slot every few seconds; re-asking every
    # 250 ms only re-scanned the queue four times a second.
    LOCAL_LIMIT_RETRY_SECONDS = 1.0
    # Enqueues and completions wake the worker; report a persistent stall once a
    # minute instead of once per pass.
    STALL_REPORT_INTERVAL = 60.0
    # A completion only moves one destination's head, so re-elect that destination
    # instead of reading every waiting row. A periodic full scan repairs anything
    # the incremental path did not see.
    HEADS_FULL_RESCAN_SECONDS = 30.0

    def __init__(self, queue: OutboundQueue, adapter: QueueAdapter, executor: Executor, worker_count: int):
        self.queue = queue
        self.adapter = adapter
        self.executor = executor
        self.slots = WorkerSlots(worker_count)
        self._lock = threading.RLock()
        self._stall_reported_at = 0.0
        self.wake_event = threading.Event()
        self.stopping = False
        self.failure: Optional[QueuePersistenceError] = None
        self.in_flight: dict[int, SubmittedCall] = {}
        # One in-flight call per Telegram topic. Topics of the same group keep
        # their own order but must not block each other.
        self.in_flight_destinations: set[tuple[int, Optional[int]]] = set()
        self.blocking_media_retries: dict[int, BlockingMediaRetry] = {}
        self.quarantined_rows: dict[int, str] = {}
        self._row_not_before: dict[int, float] = {}
        self.next_deadline: Optional[float] = None
        self._reconciliation_not_before = 0.0
        # Rows whose receipt their aggregation kind cannot take; reported once each.
        self._illegal_receipt_rows: set[int] = set()
        self._head_rows: dict[tuple[int, Optional[int]], QueuedCall] = {}
        # Rows that need a predecessor which keeps failing on Telegram first. They
        # are left out of head election until it stops failing, so the rest of
        # their destination is sent meanwhile.
        self._waiting_for_failing: dict[int, tuple[int, int | None]] = {}
        self._heads_scanned_at = 0.0
        self._heal_heads = True

    def _dispatch_heads(self) -> list[QueuedCall]:
        """Runnable heads in dispatch order, re-electing only what changed."""
        now = time.monotonic()
        self._schedule_failing_retries()
        self._readmit_waiting_for_failing()
        excluded = self._election_excluded()
        dirty = self.queue.take_dirty_destinations()
        if (self._heal_heads or not self._head_rows
                or now - self._heads_scanned_at >= self.HEADS_FULL_RESCAN_SECONDS):
            heads = self.queue.heads(
                include_payload=False, excluded_ids=excluded, ready_only=True
            )
            self._head_rows = {self._destination_key(row): row for row in heads}
            self._heads_scanned_at = now
            self._heal_heads = False
        else:
            for key in dirty:
                row = self.queue.head_for(
                    key[0], key[1], exclude_ids=excluded, ready_only=True
                )
                if row is None:
                    self._head_rows.pop(key, None)
                    continue
                self._head_rows[key] = row
        # One slot per depth tier before a tier gets a second one, oldest head
        # first inside a tier: the same order the full scan used to produce in SQL.
        tiers: dict[int, list[QueuedCall]] = {}
        for row in sorted(self._head_rows.values(), key=lambda row: (row.waiting_rows, row.id)):
            tiers.setdefault(row.waiting_rows, []).append(row)
        return [
            row for layer in itertools.zip_longest(*tiers.values()) for row in layer if row is not None
        ]

    def _election_excluded(self) -> set[int]:
        return {*self.quarantined_rows, *self._waiting_for_failing}

    def _readmit_waiting_for_failing(self) -> None:
        """Put rows back into head election once their predecessor stopped failing."""
        if not self._waiting_for_failing:
            return
        still = self.queue.still_waiting_for_failing(self._waiting_for_failing)
        for row_id in tuple(self._waiting_for_failing):
            if row_id not in still:
                self.queue.mark_destination_dirty(*self._waiting_for_failing.pop(row_id))

    def _schedule_failing_retries(self) -> None:
        """Wake for each failing row's retry, and re-elect its destination once it is due.

        A failing row is passed over while its retry deadline is ahead, so no
        head carries that deadline and no completion of its own will wake us.
        """
        now = time.time()
        for row_id, chat_id, thread_id, retry_at in self.queue.failing_retries():
            if retry_at > now:
                self._schedule_retry(time.monotonic() + retry_at - now)
                continue
            head = self._head_rows.get((chat_id, thread_id))
            if head is None or head.id != row_id:
                self.queue.mark_destination_dirty(chat_id, thread_id)

    @staticmethod
    def _destination_key(row: QueuedCall) -> tuple[int, Optional[int]]:
        """Serialization unit: one Telegram topic, not one whole group."""
        return (row.telegram_chat_id, row.thread_id)

    @staticmethod
    def _sender_kind(selection: SenderSelection) -> str:
        return "main" if selection.sender_bot_id is None else "auxiliary"

    @staticmethod
    def _expected_sender_kind(row: QueuedCall) -> str:
        return "auxiliary" if row.required_sender_bot_id is not None else "main"

    def _record_submitted_removal(self, row: QueuedCall) -> None:
        self.queue.record_removal(row, "submitted")
        self.queue.observe_metrics(
            lambda metrics: metrics.record_dequeued(row.priority, row.operation)
        )

    def _retain_failed_history(self, row: QueuedCall, error: BaseException) -> bool:
        history = row.slave_id is not None and row.slave_id.startswith(HISTORY_SOURCE_PREFIX)
        if not history:
            payload = row.payload or self.queue.load_queued(row.id).payload
            try:
                _args, kwargs = self.queue.decode_payload_raw(payload)
            except InvalidQueuedPayloadError:
                return False
            if not kwargs.get(SUPPLEMENTAL_KEY):
                return False
        self.queue.hold_failed_history(row.id, error, supplement=not history)
        self.queue.fail_waiter(row.id, _fresh_exception(error))
        self._row_not_before.pop(row.id, None)
        self.wake_event.set()
        return True

    def _record_terminal_discard(self, row: QueuedCall) -> None:
        self.queue.record_removal(row, "terminal_discard")
        self.wake_event.set()  # A new head at this destination may now be runnable.

    def _record_dispatch(self, outcome: str) -> None:
        self.queue.observe_metrics(
            lambda metrics: metrics.record_queue_dispatch(outcome)
        )

    def _record_dispatch_attempt(self, row: QueuedCall) -> float:
        dispatched_at = time.monotonic()
        self.queue.observe_metrics(
            lambda metrics: metrics.record_queue_wait(
                row.priority, row.operation, max(0.0, time.time() - row.created_at)
            )
        )
        return dispatched_at

    def _record_executor_attempt_duration(self, submitted: SubmittedCall, outcome: str) -> None:
        self.queue.observe_metrics(
            lambda metrics: metrics.record_executor_attempt_duration(
                submitted.row.priority,
                submitted.row.operation,
                outcome,
                max(0.0, time.monotonic() - submitted.dispatched_at),
            )
        )

    def _record_terminal_completion(
        self, row: QueuedCall, selection: SenderSelection | None, outcome: str
    ) -> None:
        sender_kind = self._sender_kind(selection) if selection is not None else self._expected_sender_kind(row)
        self.queue.observe_metrics(
            lambda metrics: metrics.record_completion(
                row.priority,
                row.operation,
                sender_kind,
                outcome,
            )
        )
        self.queue.observe_metrics(
            lambda metrics: metrics.record_queue_lifetime(
                row.priority, row.operation, outcome, max(0.0, time.time() - row.created_at)
            )
        )

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self.in_flight)

    def _reopen_aggregation(self, row: QueuedCall) -> None:
        """Tell the master a retained row will never reach Telegram.

        The media row was removed without being delivered, so the aggregation
        boundary it created must not split the surrounding text run.
        """
        if row.log_context:
            try:
                self.adapter.reopen_aggregation(row)
            except Exception:
                logging.getLogger(__name__).exception("Unable to reopen an aggregation stream.")

    def _schedule_retry(self, retry_at: float) -> None:
        self.next_deadline = min(self.next_deadline, retry_at) if self.next_deadline else retry_at

    def _wake_on_future_completion(self, _future: Future) -> None:
        self.wake_event.set()

    def _close_payload_on_future_completion(
        self, _future: Future, *, closeables: tuple[object, ...]
    ) -> None:
        self.queue.close_payload_resources(closeables)

    @staticmethod
    def _retry_after_seconds(error: RetryAfter) -> float:
        retry_after = error.retry_after
        return retry_after.total_seconds() if hasattr(retry_after, "total_seconds") else float(retry_after)

    @staticmethod
    def _is_blocking_media_retry(row: QueuedCall, error: BaseException) -> bool:
        return (
            row.priority == 1
            and row.operation == "edit_message_media"
            and isinstance(error, RetryAfter)
        )

    def _blocking_media_retry_row(self, row: QueuedCall, error: RetryAfter) -> QueuedCall:
        migrated_chat_id = getattr(error, "_etm_telegram_chat_id", row.telegram_chat_id)
        if migrated_chat_id == row.telegram_chat_id:
            return row
        args, kwargs = self.queue.decode_payload_raw(row.payload)
        args, kwargs = self.adapter._rewrite_queued_chat_id(
            row.operation, args, kwargs, migrated_chat_id
        )
        return replace(
            row,
            telegram_chat_id=migrated_chat_id,
            payload=self.queue.encode_payload(args, kwargs),
        )

    def _fail_blocking_retry(self, retry: BlockingMediaRetry, error: BaseException) -> None:
        # A delayed retry retains the manager's database-update context.  Claim
        # it before completing the terminal adapter path so concurrent terminal
        # conditions cannot consume that context or fail the waiter twice.
        if self.blocking_media_retries.pop(retry.row.id, None) is not retry:
            return
        self.adapter.record_queued_failure(retry.row, error, retry.selection)
        if retry.row.log_context is not None:
            try:
                self.queue.delete(retry.row.id)
            except Exception as delete_error:
                self._stop_for_persistence_error(delete_error)
                return
            self._record_terminal_discard(retry.row)
        else:
            self.queue.cleanup_payload_media(retry.row.payload)
        self.queue.fail_waiter(retry.row.id, error)
        self.queue.observe_metrics(
            lambda metrics: metrics.record_failure(retry.row.priority, retry.row.operation, "terminal")
        )
        self._record_terminal_completion(retry.row, retry.selection, "failure")

    def _schedule_blocking_retry_before_deadline(
        self, retry: BlockingMediaRetry, retry_at: Optional[float] = None
    ) -> bool:
        """Schedule a retry only while its original wall-clock deadline remains valid."""
        remaining = retry.deadline - time.time()
        if remaining <= 0:
            self._fail_blocking_retry(retry, retry.error)
            return False
        deadline_at = time.monotonic() + remaining
        self._schedule_retry(deadline_at if retry_at is None else min(retry_at, deadline_at))
        return True

    def _dispatch_blocking_media_retries(self) -> None:
        now = time.monotonic()
        for row_id, retry in tuple(self.blocking_media_retries.items()):
            if retry.retry_at > now:
                self._schedule_blocking_retry_before_deadline(retry, retry.retry_at)
                continue
            if time.time() >= retry.deadline:
                self._fail_blocking_retry(retry, retry.error)
                continue
            if self._destination_key(retry.row) in self.in_flight_destinations:
                self._schedule_blocking_retry_before_deadline(retry)
                continue
            slot = self.slots.reserve()
            if slot is None:
                self._record_dispatch("deferred")
                self.queue.observe_metrics(
                    lambda metrics: metrics.record_retry(retry.row.priority, retry.row.operation, "worker_capacity")
                )
                self._schedule_blocking_retry_before_deadline(retry)
                continue
            with slot:
                decision = self._select_sender(retry.row, now)
                if decision is None:
                    # The sender lookup raised: hold the row for the delay the log names.
                    retry = self.blocking_media_retries[row_id] = replace(
                        retry, retry_at=now + self.STALL_RESCAN_SECONDS)
                    self._schedule_blocking_retry_before_deadline(retry, retry.retry_at)
                    continue
                if decision.terminal_error_class is not None:
                    self._record_dispatch("failed")
                    self._fail_blocking_retry(
                        retry, RequiredSenderUnavailableError(decision.terminal_error_class)
                    )
                    continue
                if decision.selection is None:
                    self._record_dispatch("deferred")
                    if decision.retry_at is not None:
                        self._schedule_blocking_retry_before_deadline(retry, decision.retry_at)
                    continue
                if (
                    decision.selection.sender is not retry.selection.sender
                    or decision.selection.sender_bot_id != retry.selection.sender_bot_id
                ):
                    self._record_dispatch("failed")
                    self._fail_blocking_retry(retry, retry.error)
                    continue
                limits_acquired = self._acquire_sender_limits(retry.row, retry.selection)
                if limits_acquired is None:
                    # Acquiring the sender's limits raised: hold the row for the delay the log names.
                    retry = self.blocking_media_retries[row_id] = replace(
                        retry, retry_at=now + self.STALL_RESCAN_SECONDS)
                    self._schedule_blocking_retry_before_deadline(retry, retry.retry_at)
                    continue
                if not limits_acquired:
                    self._record_dispatch("deferred")
                    self.queue.observe_metrics(
                        lambda metrics: metrics.record_retry(retry.row.priority, retry.row.operation, "local_limit")
                    )
                    self._schedule_blocking_retry_before_deadline(retry, now + 0.25)
                    continue
                closeables: tuple[object, ...] = ()
                try:
                    args, kwargs = self.queue.decode_payload(retry.row.payload)
                    closeables = self.queue.payload_closeables(args, kwargs)
                    dispatched_at = self._record_dispatch_attempt(retry.row)
                    future = self.executor.submit(
                        self.adapter.execute_queued_call, retry.row, args, kwargs, retry.selection
                    )
                except BaseException as error:
                    self.queue.close_payload_resources(closeables)
                    self._record_dispatch("failed")
                    self.queue.observe_metrics(
                        lambda metrics: metrics.record_failure(retry.row.priority, retry.row.operation, "dispatch")
                    )
                    self._fail_blocking_retry(retry, error)
                    continue
                self.blocking_media_retries.pop(row_id, None)
                self._record_dispatch("submitted")
                future.add_done_callback(partial(
                    self._close_payload_on_future_completion, closeables=closeables
                ))
                future.add_done_callback(self._wake_on_future_completion)
                self.in_flight[row_id] = SubmittedCall(
                    retry.row, retry.selection, future, dispatched_at, closeables
                )
                slot.own(row_id)
                self.in_flight_destinations.add(self._destination_key(retry.row))
                self.queue.observe_metrics(
                    lambda metrics: metrics.increment_in_flight(
                        retry.row.priority, retry.row.operation, self._sender_kind(retry.selection)
                    )
                )

    # A sender lookup that raises defers its row instead of stopping the worker.
    SENDER_FAILURE_MESSAGE = (
        "Choosing a sender for queue row %s failed; the row stays queued and is "
        "retried in %.0f seconds."
    )

    def _select_sender(self, row: QueuedCall, now: float) -> SenderSelectionResult | None:
        try:
            return self.adapter.select_sender(row, now)
        except Exception as error:
            self._record_dispatch("deferred")
            logging.getLogger(__name__).error(
                self.SENDER_FAILURE_MESSAGE, row.id, self.STALL_RESCAN_SECONDS, exc_info=error
            )
            return None

    def _acquire_sender_limits(self, row: QueuedCall, selection: SenderSelection) -> bool | None:
        try:
            return self.adapter.acquire_sender_limits(selection, row.telegram_chat_id)
        except Exception as error:
            self._record_dispatch("deferred")
            logging.getLogger(__name__).error(
                self.SENDER_FAILURE_MESSAGE, row.id, self.STALL_RESCAN_SECONDS, exc_info=error
            )
            return None

    def _stop_for_persistence_error(self, error: Exception) -> None:
        if self.failure is None:
            if isinstance(error, QueuePersistenceError):
                persistence_error = QueuePersistenceError(str(error))
            else:
                persistence_error = QueuePersistenceError("Outbound queue deletion failed.")
            self.failure = persistence_error
        else:
            persistence_error = self.failure
        self.stopping = True
        self._row_not_before.clear()
        self.queue.fail_all_waiters(persistence_error)
        self.wake_event.set()

    RECONCILIATION_BATCH_SIZE = 32

    def reconcile_sent_pending(self, row_id: Optional[int] = None) -> set[int]:
        """Apply a bounded batch; failed receipts stay durable with retry deadlines."""
        started = time.monotonic()
        if row_id is None and started < self._reconciliation_not_before:
            self._schedule_retry(self._reconciliation_not_before)
            return set()
        reconciled: set[int] = set()
        failed: list[int] = []
        try:
            for row in self.queue.iter_sent_pending(
                due_before=time.time(), limit=self.RECONCILIATION_BATCH_SIZE, row_id=row_id,
            ):
                try:
                    if not self.adapter.reconcile_queued_delivery(row):
                        failed.append(row.id)
                        continue
                    self.queue.delete(row.id, successful=True)
                except IllegalAggregationTransitionError as error:
                    if row.id not in self._illegal_receipt_rows:
                        self._illegal_receipt_rows.add(row.id)
                        logging.getLogger(__name__).warning(
                            "Receipt of queue row %s (aggregation kind %r) cannot be reconciled; "
                            "it stays queued and is retried: %s", row.id, error.kind, error,
                        )
                    failed.append(row.id)
                    continue
                except Exception:
                    failed.append(row.id)
                    continue
                self._record_submitted_removal(row)
                self._row_not_before.pop(row.id, None)
                self._illegal_receipt_rows.discard(row.id)
                reconciled.add(row.id)
            self.queue.defer_reconciliation(failed, time.time())
            finished = time.monotonic()
            if row_id is None and (reconciled or failed):
                # Old receipt recovery yields at least as long as it worked.
                # Wakes from new traffic must not bypass this recovery pacing;
                # freshly completed live receipts still reconcile immediately.
                self._reconciliation_not_before = finished + max(0.25, finished - started)
            due = self.queue.next_reconciliation_time()
            if due is not None:
                self._schedule_retry(max(
                    self._reconciliation_not_before,
                    finished + max(0.01, due - time.time()),
                ))
        except Exception as error:
            self._stop_for_persistence_error(error)
        return reconciled

    def dispatch_once(self) -> None:
        with self._lock:
            if self.stopping:
                return
            self.next_deadline = None
            for submitted in self.in_flight.values():
                if submitted.completion_retry_at:
                    self._schedule_retry(submitted.completion_retry_at)
            self.reconcile_sent_pending()
            if self.stopping:
                return
            self._dispatch_blocking_media_retries()
            if not self.slots.free:
                if self.in_flight:
                    return  # A completion will wake us; do not scan the backlog.
                # Nothing is in flight, so no completion can release these slots.
                # Waiting for one would stop delivery until the next restart.
                logging.getLogger(__name__).error(
                    "Outbound worker permits were held without a submitted call; "
                    "resetting them. Queued rows are retained."
                )
                self.slots.reset()
            retry_destinations = {
                self._destination_key(retry.row) for retry in self.blocking_media_retries.values()
            }
            for row_id in tuple(self.quarantined_rows):
                stored_size = self.queue.queued_stored_size(row_id)
                if stored_size is None:
                    self.quarantined_rows.pop(row_id, None)
                    continue
                try:
                    self.queue.check_replay_size(row_id, stored_size)
                except QueuePersistenceError:
                    # A legacy v1 row can be compacted to sidecar-backed v2.
                    # Re-admit it so one recovery attempt can run normally.
                    if self.queue.queued_payload_version(row_id) == 1:
                        self.quarantined_rows.pop(row_id, None)
                    continue
                self.quarantined_rows.pop(row_id, None)
            heads = self._dispatch_heads()
            if not heads and self.queue.has_unresolvable_dependency():
                # Deferral links that only point at other waiting rows have no valid
                # order at all, so deliver the oldest row per destination instead of
                # leaving the queue idle until the next restart.
                heads = self.queue.heads(
                    include_payload=False, excluded_ids=self._election_excluded(),
                    ready_only=True, respect_predecessors=False,
                )
                if heads:
                    logging.getLogger(__name__).error(
                        "Queued rows were deferred by predecessor links that never resolve; "
                        "dispatching the oldest row of %d destination(s) to break the cycle.",
                        len(heads),
                    )
                    # This bypassed the incremental election; rescan next pass.
                    self._heal_heads = True
            for row in heads:
                if (
                    row.id in self.in_flight
                    or self._destination_key(row) in self.in_flight_destinations
                    or self._destination_key(row) in retry_destinations
                ):
                    continue
                if row.id in self.quarantined_rows:
                    try:
                        self.queue.check_replay_size(row.id, row.stored_bytes)
                    except QueuePersistenceError:
                        continue
                    self.quarantined_rows.pop(row.id, None)
                legacy_recovery = False
                try:
                    self.queue.check_replay_size(row.id, row.stored_bytes)
                except QueuePersistenceError as error:
                    if self.queue.queued_payload_version(row.id) == 1:
                        legacy_recovery = True
                    else:
                        self.quarantined_rows[row.id] = str(error)
                        self._heal_heads = True
                        continue
                retained_bytes = sum(item.row.stored_bytes for item in self.in_flight.values())
                retained_bytes += sum(item.row.stored_bytes for item in self.blocking_media_retries.values())
                if not legacy_recovery and retained_bytes + row.stored_bytes > self.queue.MAX_REPLAY_BYTES:
                    continue  # Existing completion/retry events release this byte budget.
                now = time.monotonic()
                if row.retry_after > time.time():
                    self._schedule_retry(now + row.retry_after - time.time())
                    continue
                deadline = self.adapter.queued_aggregation_deadline(row)
                if deadline == math.inf:
                    # Re-elect its destination without it in the next pass.
                    self._waiting_for_failing[row.id] = self._destination_key(row)
                    self.queue.mark_destination_dirty(row.telegram_chat_id, row.thread_id)
                    self.wake_event.set()
                    continue
                if deadline is not None and deadline > time.time():
                    self._schedule_retry(now + deadline - time.time())
                    continue
                not_before = self._row_not_before.get(row.id)
                if not_before is not None:
                    if now < not_before:
                        self._schedule_retry(not_before)
                        continue
                    self._row_not_before.pop(row.id, None)
                slot = self.slots.reserve()
                if slot is None:
                    self._record_dispatch("deferred")
                    self.queue.observe_metrics(
                        lambda metrics: metrics.record_retry(row.priority, row.operation, "worker_capacity")
                    )
                    continue
                with slot:
                    decision = self._select_sender(row, now)
                    if decision is None:
                        self._defer_row(row, now + self.STALL_RESCAN_SECONDS)
                        continue
                    if decision.terminal_error_class is not None:
                        unavailable_error = RequiredSenderUnavailableError(decision.terminal_error_class)
                        try:
                            if self.queue.hold_failed_aggregation(row.id, unavailable_error):
                                self.wake_event.set()
                                self.queue.fail_waiter(row.id, unavailable_error)
                                self._record_terminal_completion(row, None, "failure")
                                continue
                            if self._retain_failed_history(row, unavailable_error):
                                self._record_terminal_completion(row, None, "failure")
                                continue
                            self.queue.delete(row.id)
                        except Exception as delete_error:
                            self._stop_for_persistence_error(delete_error)
                            return
                        self._record_terminal_discard(row)
                        self._row_not_before.pop(row.id, None)
                        self._record_dispatch("failed")
                        self.queue.observe_metrics(
                            lambda metrics: metrics.record_failure(row.priority, row.operation, "terminal")
                        )
                        unavailable_error = RequiredSenderUnavailableError(decision.terminal_error_class)
                        self._record_terminal_completion(row, None, "failure")
                        self.queue.fail_waiter(row.id, unavailable_error)
                        self.adapter.finish_discarded_delivery(row)
                        continue
                    if decision.selection is None:
                        self._record_dispatch("deferred")
                        if decision.retry_at is not None:
                            self._schedule_retry(decision.retry_at)
                        continue
                    # Take capacity before reading the payload: a rejection here must
                    # stay cheap, because a full bucket re-asks within 250 ms.
                    limits_acquired = self._acquire_sender_limits(row, decision.selection)
                    if limits_acquired is None:
                        self._defer_row(row, now + self.STALL_RESCAN_SECONDS)
                        continue
                    if not limits_acquired:
                        self._record_dispatch("deferred")
                        self.queue.observe_metrics(
                            lambda metrics: metrics.record_retry(row.priority, row.operation, "local_limit")
                        )
                        self._schedule_retry(now + self.LOCAL_LIMIT_RETRY_SECONDS)
                        continue
                    try:
                        if legacy_recovery:
                            row = self.queue.recover_legacy_media_payload(row.id)
                            self.queue.check_replay_size(row.id, row.stored_bytes)
                        else:
                            row = self.queue.load_queued(row.id)
                        row = self.adapter.materialize_queued_aggregation(row, decision.selection)
                        args, kwargs = self.queue.decode_payload(row.payload)
                    except MissingQueuedExternalMediaError as error:
                        self._reopen_aggregation(row)
                        try:
                            self.queue.delete(row.id)
                        except Exception as delete_error:
                            self._stop_for_persistence_error(delete_error)
                            return
                        self._record_terminal_discard(row)
                        self._row_not_before.pop(row.id, None)
                        self._record_dispatch("failed")
                        self.queue.observe_metrics(
                            lambda metrics: metrics.record_failure(row.priority, row.operation, "terminal")
                        )
                        self.queue.fail_waiter(row.id, error)
                        self.adapter.finish_discarded_delivery(row)
                        continue
                    except InvalidQueuedPayloadError as error:
                        try:
                            self.queue.hold_invalid_payload(row.id)
                        except Exception as persistence_error:
                            self._stop_for_persistence_error(persistence_error)
                            return
                        self.queue.fail_waiter(row.id, error)
                        self.wake_event.set()
                        continue
                    except QueuePersistenceError as error:
                        if legacy_recovery:
                            self.quarantined_rows[row.id] = str(error)
                            self._heal_heads = True
                            continue
                        self._stop_for_persistence_error(error)
                        return
                    except Exception as error:
                        self._stop_for_persistence_error(error)
                        return
                    # Rows carrying a durable log context are retained until the
                    # MsgLog write is committed, regardless of blocking priority.
                    retained = row.priority == 0 or row.log_context is not None or row.operation in RETAINED_OPERATIONS
                    closeables = self.queue.payload_closeables(args, kwargs)
                    if not retained:
                        try:
                            self.queue.delete(row.id, cleanup_media=False)
                        except Exception as delete_error:
                            self.queue.close_payload_resources(closeables)
                            self._stop_for_persistence_error(delete_error)
                            return
                        self._record_submitted_removal(row)
                    attempt_marked = False
                    if row.operation in RETAINED_OPERATIONS:
                        try:
                            self.queue.begin_delivery_attempt(row.id, decision.selection)
                            attempt_marked = True
                        except Exception as persistence_error:
                            self.queue.close_payload_resources(closeables)
                            self._stop_for_persistence_error(persistence_error)
                            return
                    try:
                        dispatched_at = self._record_dispatch_attempt(row)
                        future = self.executor.submit(
                            self.adapter.execute_queued_call, row, args, kwargs, decision.selection
                        )
                    except Exception:
                        self.queue.close_payload_resources(closeables)
                        if attempt_marked:
                            try:
                                self.queue.release_delivery_attempt(row.id)
                            except Exception as persistence_error:
                                self._stop_for_persistence_error(persistence_error)
                                return
                        self._record_dispatch("failed")
                        self.queue.observe_metrics(
                            lambda metrics: metrics.record_dispatch_failure(row.priority, row.operation)
                        )
                        self.queue.observe_metrics(
                            lambda metrics: metrics.record_failure(row.priority, row.operation, "dispatch")
                        )
                        if retained:
                            self._schedule_retry(now + self.LOCAL_LIMIT_RETRY_SECONDS)
                        else:
                            self.queue.cleanup_payload_media(row.payload)
                            submit_error = ExecutorSubmitError("Unable to submit queued Telegram call.")
                            self._record_terminal_completion(row, decision.selection, "failure")
                            self.queue.fail_waiter(row.id, submit_error)
                            self.wake_event.set()
                        continue
                    self._record_dispatch("submitted")
                    future.add_done_callback(partial(
                        self._close_payload_on_future_completion, closeables=closeables
                    ))
                    future.add_done_callback(self._wake_on_future_completion)
                    self.in_flight[row.id] = SubmittedCall(
                        row, decision.selection, future, dispatched_at, closeables
                    )
                    slot.own(row.id)
                    self.in_flight_destinations.add(self._destination_key(row))
                    sender_kind = self._sender_kind(decision.selection)
                    self.queue.observe_metrics(
                        lambda metrics: metrics.increment_in_flight(
                            row.priority, row.operation, sender_kind
                        )
                    )
            self._finish_pass(heads)

    def _finish_pass(self, heads: list[QueuedCall]) -> None:
        """A stalled pass reports itself and schedules a rescan."""
        if self.stopping:
            return
        if (
            self.next_deadline is None and not self.in_flight
            and (heads or self.queue.has_waiting_rows())
        ):
            # Nothing was dispatched and nothing is in flight, so no
            # completion will wake this worker: without a deadline these
            # rows would stay queued until the next message or restart.
            now = time.monotonic()
            if now - self._stall_reported_at >= self.STALL_REPORT_INTERVAL:
                self._stall_reported_at = now
                logging.getLogger(__name__).error(
                    "Outbound scheduler left %d dispatchable head(s) undispatched with no "
                    "retry deadline and nothing in flight while queued rows remain; "
                    "rescanning in %.0f seconds.",
                    len(heads), self.STALL_RESCAN_SECONDS,
                )
            self._schedule_retry(now + self.STALL_RESCAN_SECONDS)

    def _defer_row(self, row: QueuedCall, retry_at: float) -> None:
        self._row_not_before[row.id] = retry_at
        self._schedule_retry(retry_at)

    def harvest_completed(self) -> None:
        with self._lock:
            any_harvested = False
            for row_id, submitted in tuple(self.in_flight.items()):
                if not submitted.future.done():
                    continue
                self.queue.mark_destination_dirty(submitted.row.telegram_chat_id, submitted.row.thread_id)
                delivery = None
                if not submitted.future.cancelled() and submitted.future.exception() is None:
                    completed = submitted.future.result()
                    if isinstance(completed, QueuedDeliveryResult):
                        delivery = completed
                        if time.monotonic() < submitted.completion_retry_at:
                            self._schedule_retry(submitted.completion_retry_at)
                            continue
                        try:
                            self.queue.record_telegram_completion(
                                row_id, delivery.receipt,
                                supplement=delivery.supplement,
                                operation_resolver=self.adapter._queue_operation,
                            )
                        except Exception as persistence_error:
                            logging.getLogger(__name__).warning(
                                "Primary response for queue row %s awaits durable receipt/attachment commit: %s",
                                row_id, type(persistence_error).__name__,
                            )
                            submitted.completion_retry_at = time.monotonic() + min(60, 2 ** submitted.completion_attempts)
                            submitted.completion_attempts = min(6, submitted.completion_attempts + 1)
                            self._schedule_retry(submitted.completion_retry_at)
                            continue
                any_harvested = True
                self.in_flight.pop(row_id)
                self.in_flight_destinations.discard(self._destination_key(submitted.row))
                self.slots.release(row_id)
                self.queue.observe_metrics(
                    lambda metrics: metrics.decrement_in_flight(
                        submitted.row.priority, submitted.row.operation, self._sender_kind(submitted.selection)
                    )
                )
                try:
                    result = submitted.future.result()
                except BaseException as error:
                    self._record_executor_attempt_duration(submitted, "failure")
                    self.queue.observe_metrics(
                        lambda metrics: metrics.record_failure(
                            submitted.row.priority, submitted.row.operation, "execution"
                        )
                    )
                    if isinstance(error, QueuePersistenceError):
                        self._stop_for_persistence_error(error)
                        return
                    if self._is_blocking_media_retry(submitted.row, error):
                        assert isinstance(error, RetryAfter)
                        retry_after = self._retry_after_seconds(error)
                        deadline = submitted.row.created_at + self.queue.blocking_timeout(submitted.row.payload)
                        if not self.stopping and time.time() + retry_after <= deadline:
                            retry_row = self._blocking_media_retry_row(submitted.row, error)
                            self.adapter.record_queued_retry_after(
                                retry_row, error, submitted.selection
                            )
                            retry_at = time.monotonic() + retry_after
                            self.blocking_media_retries[row_id] = BlockingMediaRetry(
                                retry_row, submitted.selection, retry_at, deadline, error
                            )
                            self._schedule_retry(retry_at)
                            self.queue.observe_metrics(
                                lambda metrics: metrics.record_retry(
                                    submitted.row.priority, submitted.row.operation, "rate_limit"
                                )
                            )
                            continue
                    decision = self.adapter.record_queued_failure(submitted.row, error, submitted.selection)
                    if decision.retry_reason == "acquisition":
                        try:
                            delay = self.queue.defer_acquisition(
                                row_id, max(0, (decision.retry_at or 0) - time.monotonic()),
                                counted=not isinstance(error.__cause__, RetryAfter),
                            )
                        except Exception as persistence_error:
                            self._stop_for_persistence_error(persistence_error)
                            return
                        self.queue.observe_metrics(
                            lambda metrics: metrics.record_retry(
                                submitted.row.priority, submitted.row.operation, "acquisition"
                            )
                        )
                        self._schedule_retry(time.monotonic() + delay)
                        continue
                    if decision.kind == "delivery_uncertain":
                        try:
                            self.queue.hold_uncertain_delivery(row_id, error)
                        except Exception as persistence_error:
                            self._stop_for_persistence_error(persistence_error)
                            return
                        self.queue.fail_waiter(row_id, DeliveryUncertainError(
                            f"Queue row {row_id}: Telegram delivery is unconfirmed; automatic resend disabled."
                        ))
                        self.queue.observe_metrics(
                            lambda metrics: metrics.record_failure(submitted.row.priority, submitted.row.operation, "uncertain")
                        )
                        continue
                    unconfirmed_edit = decision.retry_reason == UNCONFIRMED_EDIT_RETRY
                    if unconfirmed_edit:
                        try:
                            dropped = self.queue.unconfirmed_edit_superseded(row_id)
                            if dropped is not None:
                                self.queue.delete(row_id)
                        except Exception as persistence_error:
                            self._stop_for_persistence_error(persistence_error)
                            return
                        if dropped is not None:
                            logging.getLogger(__name__).warning(
                                "Queue row %s (%s): Telegram did not confirm the edit (error=%s/%s); "
                                "dropped because %s.",
                                row_id, submitted.row.operation, type(error).__name__,
                                type(error.__cause__).__name__ if error.__cause__ else "none", dropped,
                            )
                            self._record_terminal_discard(submitted.row)
                            self._row_not_before.pop(row_id, None)
                            self.queue.fail_waiter(row_id, error)
                            self.adapter.finish_discarded_delivery(submitted.row)
                            self.queue.observe_metrics(
                                lambda metrics: metrics.record_failure(
                                    submitted.row.priority, submitted.row.operation, "terminal"
                                )
                            )
                            self._record_terminal_completion(submitted.row, submitted.selection, "failure")
                            continue
                        logging.getLogger(__name__).warning(
                            "Queue row %s (%s): Telegram did not confirm the edit (error=%s/%s); "
                            "trying it once more.",
                            row_id, submitted.row.operation, type(error).__name__,
                            type(error.__cause__).__name__ if error.__cause__ else "none",
                        )
                    if decision.kind == "retry_eventual" and (submitted.row.priority == 0 or unconfirmed_edit):
                        if decision.retry_at is None:
                            raise RuntimeError("Retry decision requires a retry deadline.")
                        try:
                            if submitted.row.operation in RETAINED_OPERATIONS:
                                self.queue.release_delivery_attempt(row_id)
                            # A flood wait holds back the whole chat, not only this row.
                            if not isinstance(error, RetryAfter):
                                self.queue.defer_failed_send(
                                    row_id, time.time() + max(0.0, decision.retry_at - time.monotonic()),
                                    unconfirmed=unconfirmed_edit,
                                )
                        except Exception as persistence_error:
                            self._stop_for_persistence_error(persistence_error)
                            return
                        if self.stopping:
                            self.queue.fail_waiter(row_id, SchedulerStoppedError("Outbound scheduler stopped."))
                            continue
                        self._row_not_before[row_id] = decision.retry_at
                        self._schedule_retry(decision.retry_at)
                        if self.queue.metrics is not None:
                            retry_reason = decision.retry_reason
                            if retry_reason is None:
                                if isinstance(error, RetryAfter):
                                    retry_reason = "rate_limit"
                                elif isinstance(error, NetworkError):
                                    retry_reason = "transport"
                                else:
                                    retry_reason = "membership"
                            if retry_reason == "rate_limit":
                                # Nothing else raises RetryAfter, so name the class, the
                                # requested delay and the sender that produced it.
                                logging.getLogger(__name__).warning(
                                    "Queue row %s (%s) deferred as rate limited: error=%s/%s, "
                                    "retry_after=%s, sender=%s.",
                                    row_id, submitted.row.operation, type(error).__name__,
                                    type(error.__cause__).__name__ if error.__cause__ else "none",
                                    getattr(error, "retry_after", None),
                                    submitted.selection.sender_bot_id or "main",
                                )
                            self.queue.observe_metrics(
                                lambda metrics: metrics.record_retry(
                                    submitted.row.priority, submitted.row.operation,
                                    retry_reason,
                                )
                            )
                        continue
                    if (submitted.row.priority == 0 or submitted.row.log_context is not None
                            or submitted.row.operation in RETAINED_OPERATIONS):
                        try:
                            if self.queue.hold_failed_aggregation(row_id, error):
                                self.queue.fail_waiter(row_id, error)
                                self._record_terminal_completion(submitted.row, submitted.selection, "failure")
                                continue
                            if self._retain_failed_history(submitted.row, error):
                                self._record_terminal_completion(submitted.row, submitted.selection, "failure")
                                continue
                            self.queue.delete(row_id)
                        except Exception as delete_error:
                            self._stop_for_persistence_error(delete_error)
                            return
                        self._reopen_aggregation(submitted.row)
                        self._record_terminal_discard(submitted.row)
                        self._row_not_before.pop(row_id, None)
                    else:
                        self.queue.cleanup_payload_media(submitted.row.payload)
                    self.queue.fail_waiter(row_id, error)
                    self.queue.observe_metrics(
                        lambda metrics: metrics.record_failure(
                            submitted.row.priority, submitted.row.operation, "terminal"
                        )
                    )
                    self._record_terminal_completion(submitted.row, submitted.selection, "failure")
                else:
                    self._row_not_before.pop(row_id, None)
                    self._record_executor_attempt_duration(submitted, "success")
                    if delivery is not None:
                        result = delivery.result
                    if submitted.row.log_context is not None:
                        if delivery is None:
                            try:
                                self.queue.record_telegram_completion(
                                    row_id,
                                    self.adapter.encode_queued_completion_receipt(result, submitted.selection),
                                )
                            except Exception as persistence_error:
                                self._stop_for_persistence_error(persistence_error)
                                return
                        # Do not rescan the entire pending-log backlog per completion.
                        self.reconcile_sent_pending(row_id)
                        if self.failure is not None:
                            return
                    if (submitted.row.log_context is None and submitted.row.priority == 1
                            and submitted.row.operation not in RETAINED_OPERATIONS):
                        self.queue.release_dependents(row_id)
                    self.adapter.record_queued_success(submitted.row, result, submitted.selection)
                    if (submitted.row.priority == 0 or submitted.row.operation in RETAINED_OPERATIONS) and submitted.row.log_context is None:
                        try:
                            self.queue.delete(row_id, successful=True)
                        except Exception as delete_error:
                            self._stop_for_persistence_error(delete_error)
                            return
                        self._record_submitted_removal(submitted.row)
                    elif submitted.row.priority == 1 and submitted.row.log_context is None:
                        self.queue.cleanup_payload_media(submitted.row.payload)
                    self.queue.complete_waiter(row_id, result, submitted.selection)
                    self._record_terminal_completion(submitted.row, submitted.selection, "success")
            if any_harvested:
                self.wake_event.set()

    def stop_and_drain(self, timeout: float = 5.0) -> None:
        with self._lock:
            self.stopping = True
            self._row_not_before.clear()
            stopped_error = self.failure or SchedulerStoppedError("Outbound scheduler stopped.")
            for retry in tuple(self.blocking_media_retries.values()):
                self._fail_blocking_retry(retry, stopped_error)
            for row_id in tuple(self.queue.waiters):
                if row_id not in self.in_flight:
                    self.queue.fail_waiter(row_id, stopped_error)
            self.wake_event.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.harvest_completed()
            with self._lock:
                if not self.in_flight:
                    return
            time.sleep(0.01)
        with self._lock:
            self.harvest_completed()
            for row_id, submitted in tuple(self.in_flight.items()):
                self.in_flight.pop(row_id)
                self.in_flight_destinations.discard(self._destination_key(submitted.row))
                self.slots.release(row_id)
                self.queue.observe_metrics(
                    lambda metrics: metrics.decrement_in_flight(
                        submitted.row.priority, submitted.row.operation, self._sender_kind(submitted.selection)
                    )
                )
                self.queue.fail_waiter(row_id, SchedulerStoppedError("Outbound scheduler stopped."))
