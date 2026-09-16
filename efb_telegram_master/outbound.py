"""SQLite-backed outbound Telegram call queue."""

from __future__ import annotations

import copy
import inspect
import io
import logging
import numbers
import os
import pickle
import shutil
import sqlite3
import tempfile
import threading
from functools import partial
import time
from concurrent.futures import Executor, Future
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Protocol
from urllib.parse import unquote, urlsplit

from telegram import (
    Animation, Audio, Document, InputFile, InputMedia, InputMediaAnimation, InputMediaAudio,
    InputMediaDocument, InputMediaLivePhoto, InputMediaPhoto, InputMediaVideo, PhotoSize,
    Sticker, Video, Voice,
)
from telegram.error import NetworkError, RetryAfter
import httpx


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
RETAINED_OPERATIONS = MESSAGE_CREATING_OPERATIONS | {"edit_message_text", "edit_message_caption"}


def transport_definitely_not_sent(error: BaseException) -> bool:
    """Only connection establishment/pool failures prove no request was sent."""
    return (
        isinstance(error, NetworkError)
        and isinstance(error.__cause__, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
    )


HISTORY_REPLAY_KEY = "_history_replay"
SUPPLEMENTAL_KEY = "_full_content_attachment"
HISTORY_SOURCE_PREFIX = "__history__:"


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


class DeliveryUncertainError(QueueError):
    """Remote send may have succeeded; preserve the row and require confirmation."""
    pass


class InvalidTelegramResponseError(QueueError):
    """A send returned no usable primary receipt; the delivery must be held."""


class InvalidQueuedPayloadError(QueueError):
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


class QueueMetrics(Protocol):
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


class OutboundQueue:
    """Own the queue connection, codec, and transactional row mutations."""

    filename = "outbound-queue.sqlite3"
    # Protect restart from already-inflated historical rows before reading their
    # BLOBs. Oversized rows are retained and reported, never silently discarded.
    MAX_REPLAY_BYTES = 128 * 1024 * 1024
    # Legacy v1 rows may contain one complete media file inline. Recover one
    # such row at a time, rewrite it to v2 sidecar storage, then let normal
    # replay budgeting apply to the compact row.

    def __init__(self, channel_data_path: Path | str, metrics: Optional[QueueMetrics] = None):
        self.path = Path(channel_data_path) / self.filename
        self.media_dir = Path(channel_data_path) / "outbound-media"
        self._lock = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = None
        self.metrics = metrics
        self.waiters: dict[int, Future] = {}
        self._open()

    def _open(self) -> None:
        connection: Optional[sqlite3.Connection] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.media_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS outbound_queue_destination_priority_id "
                "ON outbound_queue (telegram_chat_id, priority DESC, id ASC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS outbound_queue_state_destination_priority_id "
                "ON outbound_queue (delivery_state, telegram_chat_id, priority DESC, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS outbound_queue_reconciliation_due "
                "ON outbound_queue (delivery_state, reconcile_after, id)"
            )
            connection.commit()
            self._connection = connection
            self._cleanup_orphan_media()
            self.refresh_depth()
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()
            raise

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
                           ("attempt_started_at", "REAL")):
            if name not in columns:
                connection.execute(f"ALTER TABLE outbound_queue ADD COLUMN {name} {kind} NULL")
        # Undo only pre-submission holds caused by the removed history sender
        # constraint. Attempted/uncertain deliveries must never be auto-replayed.
        connection.execute(
            "UPDATE outbound_queue SET delivery_hold=NULL "
            "WHERE substr(slave_id, 1, ?) = ? AND delivery_state='queued' "
            "AND delivery_hold='history_failed:RequiredSenderUnavailableError' "
            "AND attempt_started_at IS NULL",
            (len(HISTORY_SOURCE_PREFIX), HISTORY_SOURCE_PREFIX),
        )

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise QueuePersistenceError("Outbound queue is closed.")
        return self._connection

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def refresh_depth(self) -> None:
        if self.metrics is not None:
            depth = self.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0]
            self.metrics.set_queue_depth(int(depth))

    def record_removal(self, row: QueuedCall, outcome: str) -> None:
        if self.metrics is not None:
            residence_seconds = max(0.0, time.time() - row.created_at)
            self.metrics.record_removal(row.priority, row.operation, outcome, residence_seconds)
        self.refresh_depth()

    def _store_media_stream(
        self,
        source,
        filename: Optional[str],
        *,
        input_file: bool = False,
        attach_name: Optional[str] = None,
        mimetype: Optional[str] = None,
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
        return _StoredMediaSnapshot(
            Path(path).name,
            filename,
            input_file=input_file,
            attach_name=attach_name,
            mimetype=mimetype,
        )

    def _snapshot_media_value(
        self,
        value: object,
        cleanup_files: frozenset[str] = frozenset(),
    ) -> object:
        if isinstance(value, bytes):
            return self._store_media_stream(io.BytesIO(value), None)
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
                    return self._store_media_stream(source, local_path.name)
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
                )
            captured = self._snapshot_media_value(stored_content, cleanup_files)
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
            snapshot = self._store_media_stream(value, filename or None)
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
        return isinstance(value, bytes) or cls._local_media_path(value) is not None or isinstance(value, InputFile) or any(
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
        self, value: object, cleanup_files: frozenset[str]
    ) -> object:
        if not isinstance(value, InputMedia):
            return value
        normalized = copy.copy(value)
        self._validate_media_value(value.media, _NESTED_MEDIA_TYPES.get(type(value)))
        if self._needs_media_snapshot(value.media):
            object.__setattr__(
                normalized, "media", self._snapshot_media_value(value.media, cleanup_files)
            )
        for field in _INPUT_MEDIA_ATTACHMENT_FIELDS.get(type(value), ()):
            attachment = getattr(value, field, None)
            if attachment is None:
                continue
            self._validate_media_value(attachment)
            if self._needs_media_snapshot(attachment):
                object.__setattr__(
                    normalized, field, self._snapshot_media_value(attachment, cleanup_files)
                )
        return normalized

    def _normalize_direct_media(
        self, operation: str, args: tuple, kwargs: dict, cleanup_files: frozenset[str]
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
        snapshot = self._snapshot_media_value(value, cleanup_files)
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
        self, operation: str, args: tuple, kwargs: dict, cleanup_files: frozenset[str]
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
            normalized = type(value)(self._normalize_input_media(item, cleanup_files) for item in value)
        else:
            if not isinstance(value, InputMedia):
                raise QueueEnqueueError("Unable to serialize queued Telegram call.")
            normalized = self._normalize_input_media(value, cleanup_files)
        if positional:
            normalized_args = list(args)
            normalized_args[index] = normalized
            return tuple(normalized_args), kwargs
        normalized_kwargs = dict(kwargs)
        normalized_kwargs[keyword] = normalized
        return args, normalized_kwargs

    def _normalize_thumbnail(
        self, operation: str, kwargs: dict, cleanup_files: frozenset[str]
    ) -> dict:
        thumbnail = kwargs.get("thumbnail")
        if operation not in _THUMBNAIL_OPERATIONS or thumbnail is None:
            return kwargs
        self._validate_media_value(thumbnail)
        if not self._needs_media_snapshot(thumbnail):
            return kwargs
        normalized_kwargs = dict(kwargs)
        normalized_kwargs["thumbnail"] = self._snapshot_media_value(thumbnail, cleanup_files)
        return normalized_kwargs

    def _normalize_keyword_media(
        self, operation: str, kwargs: dict, cleanup_files: frozenset[str]
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
            snapshot = self._snapshot_media_value(value, cleanup_files)
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

    def _restore_media_references(self, value: object) -> object:
        if isinstance(value, _StoredMediaSnapshot):
            if value.external_uri is not None:
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
            stream = _NamedMediaFile(raw, value.filename)
            if not value.input_file:
                return stream
            input_file = InputFile(
                stream,
                filename=value.filename,
                attach=value.attach_name is not None,
                read_file_handle=False,
            )
            input_file.attach_name = value.attach_name
            if value.mimetype is not None:
                input_file.mimetype = value.mimetype
            return input_file
        if isinstance(value, tuple):
            return tuple(self._restore_media_references(item) for item in value)
        if isinstance(value, list):
            return [self._restore_media_references(item) for item in value]
        if isinstance(value, dict):
            return {key: self._restore_media_references(item) for key, item in value.items()}
        if isinstance(value, InputMedia):
            restored_media = copy.copy(value)
            object.__setattr__(restored_media, "media", self._restore_media_references(value.media))
            for field in _INPUT_MEDIA_ATTACHMENT_FIELDS.get(type(value), ()):
                attachment = getattr(value, field, None)
                if attachment is not None:
                    object.__setattr__(
                        restored_media, field, self._restore_media_references(attachment)
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
            restored_args = self._restore_media_references(args)
            restored_kwargs = self._restore_media_references(kwargs)
            assert isinstance(restored_args, tuple) and isinstance(restored_kwargs, dict)
            return restored_args, restored_kwargs
        return args, kwargs

    def _collect_media_references(self, value: object, found: list[_StoredMediaSnapshot]) -> None:
        if isinstance(value, _StoredMediaSnapshot):
            found.append(value)
            return
        if isinstance(value, (tuple, list)):
            for item in value:
                self._collect_media_references(item, found)
            return
        if isinstance(value, dict):
            for item in value.values():
                self._collect_media_references(item, found)
            return
        if isinstance(value, InputMedia):
            self._collect_media_references(value.media, found)
            for field in _INPUT_MEDIA_ATTACHMENT_FIELDS.get(type(value), ()):
                attachment = getattr(value, field, None)
                if attachment is not None:
                    self._collect_media_references(attachment, found)

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
            cursor = self.connection.execute(
                "SELECT payload FROM outbound_queue WHERE substr(payload, 1, 1) = X'02'"
            )
            for (payload,) in cursor:
                try:
                    value = pickle.loads(payload[1:])
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
                    elif item.cleanup_external and item.external_uri is not None:
                        external = self._local_media_path(item.external_uri)
                        if external is None or not external.is_file():
                            raise QueuePersistenceError(
                                "Queued external media file is missing; row retained."
                            )
        except sqlite3.Error:
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

    def _prepare(self, request: QueueRequest, operation: Callable[..., object]) -> tuple[str, tuple, dict, int, int, Optional[str], Optional[str], bytes, Optional[bytes]]:
        if not isinstance(request.operation, str) or not isinstance(request.args, tuple) or not isinstance(request.kwargs, dict):
            raise QueueEnqueueError("Queue request must be (operation: str, args: tuple, kwargs: dict).")
        cleanup_files = frozenset(
            str(Path(path).resolve()) for path in request.cleanup_files
        )
        telegram_kwargs, priority, slave_id, required_sender = self._validate_metadata(
            request.operation, request.kwargs
        )
        supplement = telegram_kwargs.pop(SUPPLEMENTAL_KEY, False)
        history_replay = telegram_kwargs.pop(HISTORY_REPLAY_KEY, None)
        if history_replay is not None and not isinstance(history_replay, dict):
            raise QueueEnqueueError("History replay metadata must be a mapping.")
        telegram_args, telegram_kwargs = self._normalize_direct_media(
            request.operation, request.args, telegram_kwargs, cleanup_files
        )
        telegram_args, telegram_kwargs = self._normalize_nested_media(
            request.operation, telegram_args, telegram_kwargs, cleanup_files
        )
        telegram_kwargs = self._normalize_thumbnail(
            request.operation, telegram_kwargs, cleanup_files
        )
        telegram_kwargs = self._normalize_keyword_media(
            request.operation, telegram_kwargs, cleanup_files
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
            slave_id, required_sender, payload, request.log_context,
        )

    def enqueue_many(
        self, requests: Iterable[QueueRequest], operation_resolver: Callable[[str], Callable[..., object]]
    ) -> tuple[int, Future]:
        request_list = list(requests)
        if not request_list:
            raise QueueEnqueueError("Queued request sequence cannot be empty.")
        prepared = []
        try:
            for request in request_list:
                prepared.append(self._prepare(request, operation_resolver(request.operation)))
        except Exception:
            for item in prepared:
                self.cleanup_payload_media(item[7])
            raise
        destinations = {(item[3], item[4]) for item in prepared}
        if len(destinations) != 1:
            for item in prepared:
                self.cleanup_payload_media(item[7])
            raise QueueEnqueueError("Queued request sequence must share chat_id and priority.")
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                identifiers: list[int] = []
                now = time.time()
                for operation, _args, _kwargs, chat_id, priority, slave_id, required_sender, payload, log_context in prepared:
                    cursor = self.connection.execute(
                        "INSERT INTO outbound_queue "
                        "(priority, telegram_chat_id, operation, payload, slave_id, required_sender_bot_id, "
                        "created_at, log_context) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (priority, chat_id, operation, payload, slave_id, required_sender, now, log_context),
                    )
                    identifier = cursor.lastrowid
                    if identifier is None:
                        raise QueueEnqueueError("SQLite did not return an inserted queue row ID.")
                    identifiers.append(identifier)
                self.connection.commit()
            except Exception as error:
                try:
                    self.connection.rollback()
                except sqlite3.Error:
                    pass
                for item in prepared:
                    self.cleanup_payload_media(item[7])
                raise QueueEnqueueError("Unable to commit queued Telegram call.") from error
            if self.metrics is not None:
                for operation, _args, _kwargs, _chat_id, priority, _slave_id, _required_sender, _payload, _log_context in prepared:
                    self.metrics.record_enqueued(priority, operation)
            self.refresh_depth()
            waiter: Future = Future()
            self.waiters[identifiers[0]] = waiter
            return identifiers[0], waiter

    def heads(
        self, *, include_payload: bool = True, excluded_ids: Iterable[int] = (),
        ready_only: bool = False,
    ) -> list[QueuedCall]:
        payload = "q.payload" if include_payload else "X''"
        context = "q.log_context" if include_payload else "CASE WHEN q.log_context IS NULL THEN NULL ELSE X'' END"
        receipt = "q.completion_receipt" if include_payload else "NULL"
        excluded = tuple(excluded_ids)
        exclusion_sql = " AND delivery_hold IS NULL" if ready_only else ""
        parameters: tuple[object, ...] = ()
        if excluded:
            exclusion_sql += " AND id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            parameters = tuple(excluded)
        with self._lock:
            # Select one runnable head per destination before reading media BLOBs.
            # Quarantined historical rows may be skipped explicitly so one
            # unreplayable attachment does not strand all newer traffic for the
            # same Telegram destination.
            rows = self.connection.execute(
                f"SELECT q.id, q.priority, q.telegram_chat_id, q.operation, {payload}, q.slave_id, "
                f"q.required_sender_bot_id, q.created_at, {context}, q.delivery_state, {receipt}, "
                "length(q.payload) + COALESCE(length(q.log_context), 0) + COALESCE(length(q.completion_receipt), 0), q.reconcile_after "
                "FROM outbound_queue AS q JOIN ("
                "SELECT COALESCE(MIN(CASE WHEN priority = 1 THEN id END), MIN(id)) AS head_id "
                "FROM outbound_queue WHERE delivery_state = 'queued'" + exclusion_sql +
                " GROUP BY telegram_chat_id"
                ") AS heads ON q.id = heads.head_id ORDER BY q.telegram_chat_id",
                parameters,
            ).fetchall()
        return [QueuedCall(*row) for row in rows]

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
                "required_sender_bot_id, created_at, log_context, delivery_state, completion_receipt "
                "FROM outbound_queue WHERE id = ? AND delivery_state = 'queued'", (row_id,),
            ).fetchone()
        if row is None:
            raise QueuePersistenceError(f"Queued row {row_id} disappeared before dispatch.")
        return replace(QueuedCall(*row), stored_bytes=int(size[0]))

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
                "SELECT hex(substr(payload, 1, 1)) FROM outbound_queue "
                "WHERE id = ? AND delivery_state = 'queued'",
                (row_id,),
            ).fetchone()
        if row is None or not row[0]:
            return None
        return int(str(row[0]), 16)

    def load_legacy_queued_for_recovery(self, row_id: int) -> QueuedCall:
        """Load one bounded v1 row so it can be rewritten to sidecar storage."""
        with self._lock:
            metadata = self.connection.execute(
                "SELECT length(payload) + COALESCE(length(log_context), 0) + "
                "COALESCE(length(completion_receipt), 0), hex(substr(payload, 1, 1)) "
                "FROM outbound_queue WHERE id = ? AND delivery_state = 'queued'",
                (row_id,),
            ).fetchone()
            if metadata is None:
                raise QueuePersistenceError(f"Queued row {row_id} disappeared before recovery.")
            size, version_hex = int(metadata[0]), str(metadata[1])
            if version_hex != "01":
                raise QueuePersistenceError(f"Queued row {row_id} is not a legacy v1 payload.")
            row = self.connection.execute(
                "SELECT id, priority, telegram_chat_id, operation, payload, slave_id, "
                "required_sender_bot_id, created_at, log_context, delivery_state, completion_receipt "
                "FROM outbound_queue WHERE id = ? AND delivery_state = 'queued'",
                (row_id,),
            ).fetchone()
        if row is None:
            raise QueuePersistenceError(f"Queued row {row_id} disappeared before recovery.")
        return replace(QueuedCall(*row), stored_bytes=size)

    def rewrite_legacy_media_payload(self, row: QueuedCall) -> QueuedCall:
        """Rewrite one loaded v1 media call to compact v2 sidecar storage."""
        if not row.payload or row.payload[0] != 1:
            return row
        args, kwargs = self.decode_payload_raw(row.payload)
        normalized_args, normalized_kwargs = self._normalize_direct_media(
            row.operation, args, kwargs, frozenset()
        )
        normalized_args, normalized_kwargs = self._normalize_nested_media(
            row.operation, normalized_args, normalized_kwargs, frozenset()
        )
        normalized_kwargs = self._normalize_thumbnail(
            row.operation, normalized_kwargs, frozenset()
        )
        normalized_kwargs = self._normalize_keyword_media(
            row.operation, normalized_kwargs, frozenset()
        )
        payload = self.encode_payload(normalized_args, normalized_kwargs)
        try:
            with self._lock:
                self.connection.execute("BEGIN")
                cursor = self.connection.execute(
                    "UPDATE outbound_queue SET payload = ? "
                    "WHERE id = ? AND delivery_state = 'queued' AND substr(payload, 1, 1) = X'01'",
                    (payload, row.id),
                )
                if cursor.rowcount != 1:
                    raise QueuePersistenceError(
                        f"Queued row {row.id} changed before legacy recovery could commit."
                    )
                self.connection.commit()
        except Exception:
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass
            self.cleanup_payload_media(payload)
            raise
        compact_size = len(payload) + len(row.log_context or b"") + len(row.completion_receipt or b"")
        return replace(row, payload=payload, stored_bytes=compact_size)

    def check_replay_size(self, row_id: int, size: int) -> None:
        if size > self.MAX_REPLAY_BYTES:
            raise QueuePersistenceError(
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
        filters = ["delivery_state = 'sent_pending'"]
        parameters: list[object] = []
        if due_before is not None:
            filters.append("reconcile_after <= ?")
            parameters.append(due_before)
        if row_id is not None:
            filters.append("id = ?")
            parameters.append(row_id)
        parameters.append(limit)
        with self._lock:
            # Read IDs first: 32 historical contexts can themselves occupy GBs.
            identifiers = self.connection.execute(
                "SELECT id, COALESCE(length(log_context), 0) + COALESCE(length(completion_receipt), 0) "
                "FROM outbound_queue WHERE " + " AND ".join(filters) +
                " ORDER BY reconcile_after, id LIMIT ?", parameters,
            ).fetchall()
        for identifier, size in identifiers:
            self.check_replay_size(identifier, size)
            with self._lock:
                row = self.connection.execute(
                    "SELECT id, priority, telegram_chat_id, operation, X'', slave_id, "
                    "required_sender_bot_id, created_at, log_context, delivery_state, completion_receipt "
                    "FROM outbound_queue WHERE id = ? AND delivery_state = 'sent_pending'",
                    (identifier,),
                ).fetchone()
            if row is not None:
                yield QueuedCall(*row)
            del row

    def next_reconciliation_time(self) -> Optional[float]:
        with self._lock:
            return self.connection.execute(
                "SELECT MIN(reconcile_after) FROM outbound_queue WHERE delivery_state = 'sent_pending'"
            ).fetchone()[0]

    def defer_reconciliation(self, row_ids: list[int], now: float) -> None:
        """Persist exponential retry delays (1s to 60s), including across restarts."""
        if not row_ids:
            return
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self.connection.executemany(
                    "UPDATE outbound_queue SET "
                    "reconcile_after = ? + MIN(60, 1 << MIN(reconcile_attempts, 6)), "
                    "reconcile_attempts = MIN(reconcile_attempts + 1, 7) "
                    "WHERE id = ? AND delivery_state = 'sent_pending'",
                    [(now, row_id) for row_id in row_ids],
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def begin_delivery_attempt(self, row_id: int, selection: SenderSelection) -> None:
        """Persist before submission: an interrupted send is not safe to replay."""
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold='in_flight', attempt_sender_bot_id=?, "
                "attempt_started_at=? WHERE id=? AND delivery_state='queued' AND delivery_hold IS NULL",
                (selection.sender_bot_id, time.time(), row_id),
            )
            if cursor.rowcount != 1:
                raise QueuePersistenceError(f"Queue row {row_id} is not available for a send attempt.")

    def release_delivery_attempt(self, row_id: int) -> None:
        """Use only when submission failed or Telegram definitely rejected the call."""
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=NULL WHERE id=? AND delivery_state='queued'",
                (row_id,),
            )

    def hold_uncertain_delivery(self, row_id: int, error: BaseException) -> None:
        # Store class names only, never credentials, request objects or tracebacks.
        reason = type(error).__name__
        if error.__cause__ is not None:
            reason += "/" + type(error.__cause__).__name__
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=? WHERE id=? AND delivery_state='queued'",
                ("uncertain:" + reason, row_id),
            )
            if cursor.rowcount != 1:
                raise QueuePersistenceError(f"Queue row {row_id} disappeared while preserving an uncertain send.")

    def hold_failed_history(self, row_id: int, error: BaseException, *, supplement: bool = False) -> None:
        """Keep rejected history or supplemental work available for repair."""
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=? WHERE id=? AND delivery_state='queued'",
                (("supplement_failed:" if supplement else "history_failed:") + type(error).__name__, row_id),
            )
            if cursor.rowcount != 1:
                raise QueuePersistenceError(f"History queue row {row_id} disappeared before failure retention.")

    def defer_acquisition(self, row_id: int, minimum_delay: float = 0) -> float:
        """Persist capped backoff for an acquisition that never attempted a send."""
        with self._lock, self.connection:
            attempts = self.connection.execute(
                "SELECT reconcile_attempts FROM outbound_queue WHERE id=?", (row_id,)
            ).fetchone()[0]
            delay = max(min(60, 2 ** min(attempts, 6)), minimum_delay)
            self.connection.execute(
                "UPDATE outbound_queue SET delivery_hold=NULL, reconcile_attempts=?, reconcile_after=? WHERE id=?",
                (min(attempts + 1, 6), time.time() + delay, row_id),
            )
        return float(delay)

    def record_telegram_completion(
        self, row_id: int, receipt: bytes, *, supplement: Optional[QueueRequest] = None,
        operation_resolver: Optional[Callable[[str], Callable[..., object]]] = None,
    ) -> None:
        if not isinstance(receipt, bytes):
            raise QueuePersistenceError("Queued Telegram completion receipt must be bytes.")
        prepared = None
        if supplement is not None:
            assert operation_resolver is not None
            prepared = self._prepare(supplement, operation_resolver(supplement.operation))
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                if prepared is not None:
                    operation, _args, _kwargs, chat_id, priority, slave_id, required_sender, payload, log_context = prepared
                    self.connection.execute(
                        "INSERT INTO outbound_queue "
                        "(priority, telegram_chat_id, operation, payload, slave_id, required_sender_bot_id, "
                        "created_at, log_context) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (priority, chat_id, operation, payload, slave_id, required_sender, time.time(), log_context),
                    )
                cursor = self.connection.execute(
                    "UPDATE outbound_queue SET delivery_state = 'sent_pending', completion_receipt = ?, delivery_hold = NULL, "
                    "reconcile_after=0, reconcile_attempts=0 "
                    "WHERE id = ? AND delivery_state = 'queued'",
                    (receipt, row_id),
                )
                if cursor.rowcount != 1:
                    raise QueuePersistenceError(f"Queued row {row_id} cannot record Telegram completion.")
                depth = 0
                if prepared is not None and self.metrics is not None:
                    depth = self.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0]
                self.connection.commit()
            except Exception:
                try:
                    self.connection.rollback()
                except sqlite3.Error:
                    pass
                if prepared is not None:
                    self.cleanup_payload_media(prepared[7])
                raise
        if prepared is not None and self.metrics is not None:
            self.metrics.record_enqueued(prepared[4], prepared[0])
            self.metrics.set_queue_depth(depth)

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
                    "SELECT payload FROM outbound_queue WHERE id = ? AND delivery_state = 'queued'",
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
                cursor = self.connection.execute(
                    "UPDATE outbound_queue SET telegram_chat_id = ?, payload = ? "
                    "WHERE id = ? AND delivery_state = 'queued'",
                    (new_chat_id, new_payload, row_id),
                )
                if cursor.rowcount != 1:
                    raise QueuePersistenceError(f"Queued row {row_id} cannot be retargeted.")
                self.connection.commit()
            except Exception as error:
                try:
                    self.connection.rollback()
                except sqlite3.Error:
                    pass
                if isinstance(error, QueuePersistenceError):
                    raise
                raise QueuePersistenceError(
                    f"Queued row {row_id} retarget persistence failed."
                ) from error

    def delete(self, row_id: int, *, cleanup_media: bool = True) -> None:
        payload: Optional[bytes] = None
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                if cleanup_media:
                    marker = self.connection.execute(
                        "SELECT substr(payload, 1, 1) FROM outbound_queue WHERE id = ?",
                        (row_id,),
                    ).fetchone()
                    if marker is not None and marker[0] == b"\x02":
                        payload = self.connection.execute(
                            "SELECT payload FROM outbound_queue WHERE id = ?", (row_id,)
                        ).fetchone()[0]
                cursor = self.connection.execute("DELETE FROM outbound_queue WHERE id = ?", (row_id,))
                if cursor.rowcount != 1:
                    raise QueuePersistenceError(f"Queued row {row_id} disappeared before deletion.")
                self.connection.commit()
            except Exception:
                try:
                    self.connection.rollback()
                except sqlite3.Error:
                    pass
                raise
        if payload is not None:
            self.cleanup_payload_media(payload)

    def fail_waiter(self, row_id: int, error: BaseException) -> None:
        waiter = self.waiters.pop(row_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_exception(error)

    def fail_all_waiters(self, error: BaseException) -> None:
        for row_id in tuple(self.waiters):
            self.fail_waiter(row_id, _fresh_exception(error))


class OutboundQueueScheduler:
    """Schedule one in-flight call per chat and retain eventual rows through retries."""

    def __init__(self, queue: OutboundQueue, adapter: QueueAdapter, executor: Executor, worker_count: int):
        self.queue = queue
        self.adapter = adapter
        self.executor = executor
        self._permits = threading.BoundedSemaphore(worker_count)
        self._lock = threading.RLock()
        self.wake_event = threading.Event()
        self.stopping = False
        self.failure: Optional[QueuePersistenceError] = None
        self.in_flight: dict[int, SubmittedCall] = {}
        self.in_flight_destinations: set[int] = set()
        self.blocking_media_retries: dict[int, BlockingMediaRetry] = {}
        self.quarantined_rows: dict[int, str] = {}
        self._row_not_before: dict[int, float] = {}
        self.next_deadline: Optional[float] = None
        self._reconciliation_not_before = 0.0

    @staticmethod
    def _sender_kind(selection: SenderSelection) -> str:
        return "main" if selection.sender_bot_id is None else "auxiliary"

    @staticmethod
    def _expected_sender_kind(row: QueuedCall) -> str:
        return "auxiliary" if row.required_sender_bot_id is not None else "main"

    def _record_submitted_removal(self, row: QueuedCall) -> None:
        self.queue.record_removal(row, "submitted")
        if self.queue.metrics is not None:
            self.queue.metrics.record_dequeued(row.priority, row.operation)

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
        if self.queue.metrics is not None:
            self.queue.metrics.record_queue_dispatch(outcome)

    def _record_dispatch_attempt(self, row: QueuedCall) -> float:
        dispatched_at = time.monotonic()
        if self.queue.metrics is not None:
            self.queue.metrics.record_queue_wait(
                row.priority, row.operation, max(0.0, time.time() - row.created_at)
            )
        return dispatched_at

    def _record_executor_attempt_duration(self, submitted: SubmittedCall, outcome: str) -> None:
        if self.queue.metrics is None:
            return
        self.queue.metrics.record_executor_attempt_duration(
            submitted.row.priority,
            submitted.row.operation,
            outcome,
            max(0.0, time.monotonic() - submitted.dispatched_at),
        )

    def _record_terminal_completion(
        self, row: QueuedCall, selection: SenderSelection | None, outcome: str
    ) -> None:
        if self.queue.metrics is None:
            return
        sender_kind = self._sender_kind(selection) if selection is not None else self._expected_sender_kind(row)
        self.queue.metrics.record_completion(
            row.priority,
            row.operation,
            sender_kind,
            outcome,
        )
        self.queue.metrics.record_queue_lifetime(
            row.priority, row.operation, outcome, max(0.0, time.time() - row.created_at)
        )

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self.in_flight)

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
        if self.queue.metrics is not None:
            self.queue.metrics.record_failure(retry.row.priority, retry.row.operation, "terminal")
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
            if retry.row.telegram_chat_id in self.in_flight_destinations:
                self._schedule_blocking_retry_before_deadline(retry)
                continue
            if not self._permits.acquire(blocking=False):
                self._record_dispatch("deferred")
                if self.queue.metrics is not None:
                    self.queue.metrics.record_retry(retry.row.priority, retry.row.operation, "worker_capacity")
                self._schedule_blocking_retry_before_deadline(retry)
                continue
            decision = self.adapter.select_sender(retry.row, now)
            if decision.terminal_error_class is not None:
                self._permits.release()
                self._record_dispatch("failed")
                self._fail_blocking_retry(
                    retry, RequiredSenderUnavailableError(decision.terminal_error_class)
                )
                continue
            if decision.selection is None:
                self._permits.release()
                self._record_dispatch("deferred")
                if decision.retry_at is not None:
                    self._schedule_blocking_retry_before_deadline(retry, decision.retry_at)
                continue
            if (
                decision.selection.sender is not retry.selection.sender
                or decision.selection.sender_bot_id != retry.selection.sender_bot_id
            ):
                self._permits.release()
                self._record_dispatch("failed")
                self._fail_blocking_retry(retry, retry.error)
                continue
            if not self.adapter.acquire_sender_limits(retry.selection, retry.row.telegram_chat_id):
                self._permits.release()
                self._record_dispatch("deferred")
                if self.queue.metrics is not None:
                    self.queue.metrics.record_retry(retry.row.priority, retry.row.operation, "rate_limit")
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
                self._permits.release()
                self._record_dispatch("failed")
                if self.queue.metrics is not None:
                    self.queue.metrics.record_failure(retry.row.priority, retry.row.operation, "dispatch")
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
            self.in_flight_destinations.add(retry.row.telegram_chat_id)
            if self.queue.metrics is not None:
                self.queue.metrics.increment_in_flight(
                    retry.row.priority, retry.row.operation, self._sender_kind(retry.selection)
                )

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
        reconciler = getattr(self.adapter, "reconcile_queued_delivery", None)
        if not callable(reconciler):
            return set()
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
                    if not reconciler(row):
                        failed.append(row.id)
                        continue
                    self.queue.delete(row.id)
                except Exception:
                    failed.append(row.id)
                    continue
                self._record_submitted_removal(row)
                self._row_not_before.pop(row.id, None)
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
            if not self._permits.acquire(blocking=False):
                return  # A future completion will wake us; do not scan the backlog.
            self._permits.release()
            retry_destinations = {
                retry.row.telegram_chat_id for retry in self.blocking_media_retries.values()
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
            for row in self.queue.heads(
                include_payload=False, excluded_ids=self.quarantined_rows, ready_only=True
            ):
                if (
                    row.id in self.in_flight
                    or row.telegram_chat_id in self.in_flight_destinations
                    or row.telegram_chat_id in retry_destinations
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
                        continue
                retained_bytes = sum(item.row.stored_bytes for item in self.in_flight.values())
                retained_bytes += sum(item.row.stored_bytes for item in self.blocking_media_retries.values())
                if not legacy_recovery and retained_bytes + row.stored_bytes > self.queue.MAX_REPLAY_BYTES:
                    continue  # Existing completion/retry events release this byte budget.
                now = time.monotonic()
                if row.retry_after > time.time():
                    self._schedule_retry(now + row.retry_after - time.time())
                    continue
                not_before = self._row_not_before.get(row.id)
                if not_before is not None:
                    if now < not_before:
                        self._schedule_retry(not_before)
                        continue
                    self._row_not_before.pop(row.id, None)
                if not self._permits.acquire(blocking=False):
                    self._record_dispatch("deferred")
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_retry(row.priority, row.operation, "worker_capacity")
                    continue
                decision = self.adapter.select_sender(row, now)
                if decision.terminal_error_class is not None:
                    self._permits.release()
                    unavailable_error = RequiredSenderUnavailableError(decision.terminal_error_class)
                    try:
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
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_failure(row.priority, row.operation, "terminal")
                    unavailable_error = RequiredSenderUnavailableError(decision.terminal_error_class)
                    self._record_terminal_completion(row, None, "failure")
                    self.queue.fail_waiter(row.id, unavailable_error)
                    continue
                if decision.selection is None:
                    self._permits.release()
                    self._record_dispatch("deferred")
                    if decision.retry_at is not None:
                        self._schedule_retry(decision.retry_at)
                    continue
                if not self.adapter.acquire_sender_limits(decision.selection, row.telegram_chat_id):
                    self._permits.release()
                    self._record_dispatch("deferred")
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_retry(row.priority, row.operation, "rate_limit")
                    self._schedule_retry(now + 0.25)
                    continue
                try:
                    if legacy_recovery:
                        row = self.queue.load_legacy_queued_for_recovery(row.id)
                        row = self.queue.rewrite_legacy_media_payload(row)
                        self.queue.check_replay_size(row.id, row.stored_bytes)
                    else:
                        row = self.queue.load_queued(row.id)
                    args, kwargs = self.queue.decode_payload(row.payload)
                except InvalidQueuedPayloadError as error:
                    self._permits.release()
                    try:
                        if self._retain_failed_history(row, error):
                            self._record_terminal_completion(row, None, "failure")
                            continue
                        self.queue.delete(row.id)
                    except Exception as delete_error:
                        self._stop_for_persistence_error(delete_error)
                        return
                    self._record_terminal_discard(row)
                    self._row_not_before.pop(row.id, None)
                    self._record_dispatch("failed")
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_failure(row.priority, row.operation, "terminal")
                    self._record_terminal_completion(row, None, "failure")
                    self.queue.fail_waiter(row.id, error)
                    continue
                except QueuePersistenceError as error:
                    self._permits.release()
                    if legacy_recovery:
                        self.quarantined_rows[row.id] = str(error)
                        continue
                    self._stop_for_persistence_error(error)
                    return
                except Exception as error:
                    self._permits.release()
                    self._stop_for_persistence_error(error)
                    return
                # Rows carrying a durable log context are retained until the
                # MsgLog write is committed, regardless of blocking priority.
                retained = row.priority == 0 or row.log_context is not None or row.operation in RETAINED_OPERATIONS
                if not retained:
                    try:
                        self.queue.delete(row.id, cleanup_media=False)
                    except Exception as delete_error:
                        self._permits.release()
                        self._stop_for_persistence_error(delete_error)
                        return
                    self._record_submitted_removal(row)
                closeables = self.queue.payload_closeables(args, kwargs)
                attempt_marked = False
                if row.operation in RETAINED_OPERATIONS:
                    try:
                        self.queue.begin_delivery_attempt(row.id, decision.selection)
                        attempt_marked = True
                    except Exception as persistence_error:
                        self.queue.close_payload_resources(closeables)
                        self._permits.release()
                        self._stop_for_persistence_error(persistence_error)
                        return
                try:
                    dispatched_at = self._record_dispatch_attempt(row)
                    future = self.executor.submit(
                        self.adapter.execute_queued_call, row, args, kwargs, decision.selection
                    )
                except Exception:
                    self.queue.close_payload_resources(closeables)
                    self._permits.release()
                    if attempt_marked:
                        try:
                            self.queue.release_delivery_attempt(row.id)
                        except Exception as persistence_error:
                            self._stop_for_persistence_error(persistence_error)
                            return
                    self._record_dispatch("failed")
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_dispatch_failure(row.priority, row.operation)
                        self.queue.metrics.record_failure(row.priority, row.operation, "dispatch")
                    if retained:
                        self._schedule_retry(now + 0.25)
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
                self.in_flight_destinations.add(row.telegram_chat_id)
                if self.queue.metrics is not None:
                    self.queue.metrics.increment_in_flight(
                        row.priority, row.operation, self._sender_kind(decision.selection)
                    )

    def harvest_completed(self) -> None:
        with self._lock:
            any_harvested = False
            for row_id, submitted in tuple(self.in_flight.items()):
                if not submitted.future.done():
                    continue
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
                self.in_flight_destinations.remove(submitted.row.telegram_chat_id)
                self._permits.release()
                if self.queue.metrics is not None:
                    self.queue.metrics.decrement_in_flight(
                        submitted.row.priority, submitted.row.operation, self._sender_kind(submitted.selection)
                    )
                try:
                    result = submitted.future.result()
                except BaseException as error:
                    self._record_executor_attempt_duration(submitted, "failure")
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_failure(
                            submitted.row.priority, submitted.row.operation, "execution"
                        )
                    if isinstance(error, QueuePersistenceError):
                        self._stop_for_persistence_error(error)
                        return
                    if self._is_blocking_media_retry(submitted.row, error):
                        assert isinstance(error, RetryAfter)
                        retry_after = self._retry_after_seconds(error)
                        deadline = submitted.row.created_at + 300.0
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
                            if self.queue.metrics is not None:
                                self.queue.metrics.record_retry(
                                    submitted.row.priority, submitted.row.operation, "rate_limit"
                                )
                            continue
                    decision = self.adapter.record_queued_failure(submitted.row, error, submitted.selection)
                    if getattr(decision, "retry_reason", None) == "acquisition":
                        try:
                            delay = self.queue.defer_acquisition(
                                row_id, max(0, (decision.retry_at or 0) - time.monotonic())
                            )
                        except Exception as persistence_error:
                            self._stop_for_persistence_error(persistence_error)
                            return
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
                        if self.queue.metrics is not None:
                            self.queue.metrics.record_failure(submitted.row.priority, submitted.row.operation, "uncertain")
                        continue
                    if decision.kind == "retry_eventual" and submitted.row.priority == 0:
                        if submitted.row.operation in RETAINED_OPERATIONS:
                            try:
                                self.queue.release_delivery_attempt(row_id)
                            except Exception as persistence_error:
                                self._stop_for_persistence_error(persistence_error)
                                return
                        if self.stopping:
                            self.queue.fail_waiter(row_id, SchedulerStoppedError("Outbound scheduler stopped."))
                            continue
                        if decision.retry_at is None:
                            raise RuntimeError("Retry decision requires a retry deadline.")
                        self._row_not_before[row_id] = decision.retry_at
                        self._schedule_retry(decision.retry_at)
                        if self.queue.metrics is not None:
                            retry_reason = getattr(decision, "retry_reason", None)
                            if retry_reason is None:
                                if isinstance(error, RetryAfter):
                                    retry_reason = "rate_limit"
                                elif isinstance(error, NetworkError):
                                    retry_reason = "transport"
                                else:
                                    retry_reason = "membership"
                            self.queue.metrics.record_retry(
                                submitted.row.priority, submitted.row.operation,
                                retry_reason,
                            )
                        continue
                    if (submitted.row.priority == 0 or submitted.row.log_context is not None
                            or submitted.row.operation in RETAINED_OPERATIONS):
                        try:
                            if self._retain_failed_history(submitted.row, error):
                                self._record_terminal_completion(submitted.row, submitted.selection, "failure")
                                continue
                            self.queue.delete(row_id)
                        except Exception as delete_error:
                            self._stop_for_persistence_error(delete_error)
                            return
                        self._record_terminal_discard(submitted.row)
                        self._row_not_before.pop(row_id, None)
                    else:
                        self.queue.cleanup_payload_media(submitted.row.payload)
                    self.queue.fail_waiter(row_id, error)
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_failure(
                            submitted.row.priority, submitted.row.operation, "terminal"
                        )
                    self._record_terminal_completion(submitted.row, submitted.selection, "failure")
                else:
                    self._row_not_before.pop(row_id, None)
                    self._record_executor_attempt_duration(submitted, "success")
                    if delivery is not None:
                        result = delivery.result
                    if submitted.row.log_context is not None:
                        if delivery is None:
                            receipt_encoder = getattr(self.adapter, "encode_queued_completion_receipt", None)
                            if not callable(receipt_encoder):
                                self._stop_for_persistence_error(
                                    QueuePersistenceError(
                                        "Queue adapter cannot persist a Telegram completion receipt."
                                    )
                                )
                                return
                            try:
                                self.queue.record_telegram_completion(
                                    row_id, receipt_encoder(result, submitted.selection)
                                )
                            except Exception as persistence_error:
                                self._stop_for_persistence_error(persistence_error)
                                return
                        # Do not rescan the entire pending-log backlog per completion.
                        self.reconcile_sent_pending(row_id)
                        if self.failure is not None:
                            return
                    self.adapter.record_queued_success(submitted.row, result, submitted.selection)
                    if (submitted.row.priority == 0 or submitted.row.operation in RETAINED_OPERATIONS) and submitted.row.log_context is None:
                        try:
                            self.queue.delete(row_id)
                        except Exception as delete_error:
                            self._stop_for_persistence_error(delete_error)
                            return
                        self._record_submitted_removal(submitted.row)
                    elif submitted.row.priority == 1 and submitted.row.log_context is None:
                        self.queue.cleanup_payload_media(submitted.row.payload)
                    waiter = self.queue.waiters.pop(row_id, None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(result)
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
                self.in_flight_destinations.discard(submitted.row.telegram_chat_id)
                self._permits.release()
                if self.queue.metrics is not None:
                    self.queue.metrics.decrement_in_flight(
                        submitted.row.priority, submitted.row.operation, self._sender_kind(submitted.selection)
                    )
                self.queue.fail_waiter(row_id, SchedulerStoppedError("Outbound scheduler stopped."))
