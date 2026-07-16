"""SQLite-backed outbound Telegram call queue."""

from __future__ import annotations

import copy
import inspect
import io
import numbers
import pickle
import sqlite3
import threading
import time
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Protocol

from telegram import (
    Animation, Audio, Document, InputFile, InputMedia, InputMediaAnimation, InputMediaAudio,
    InputMediaDocument, InputMediaPhoto, InputMediaVideo, PhotoSize, Sticker, Video, Voice,
)


@dataclass(frozen=True)
class _DirectMediaArgument:
    index: int
    keyword: str
    telegram_type: type


QUEUED_OPERATIONS = frozenset({
    "send_message", "send_document", "send_photo", "send_audio",
    "send_video", "send_animation", "send_voice", "send_sticker",
    "send_media_group", "copy_message", "forward_message",
    "edit_message_text", "edit_message_caption", "edit_message_media",
    "delete_message",
})
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
    InputMediaPhoto: PhotoSize,
    InputMediaVideo: Video,
}


class QueueError(RuntimeError):
    pass


class QueueEnqueueError(QueueError):
    pass


class SchedulerStoppedError(QueueError):
    pass


class QueuePersistenceError(QueueError):
    pass


class InvalidQueuedPayloadError(QueueError):
    pass


class RequiredSenderUnavailableError(QueueError):
    pass


class ExecutorSubmitError(QueueError):
    pass


@dataclass(frozen=True)
class QueueRequest:
    operation: str
    args: tuple
    kwargs: dict


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


@dataclass
class SubmittedCall:
    row: QueuedCall
    selection: SenderSelection
    future: Future


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


class QueueAdapter(Protocol):
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


class OutboundQueue:
    """Own the queue connection, codec, and transactional row mutations."""

    filename = "outbound-queue.sqlite3"

    def __init__(self, channel_data_path: Path | str, metrics: Optional[QueueMetrics] = None):
        self.path = Path(channel_data_path) / self.filename
        self._lock = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = None
        self.metrics = metrics
        self.waiters: dict[int, Future] = {}
        self._open()

    def _open(self) -> None:
        connection: Optional[sqlite3.Connection] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
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
                "required_sender_bot_id TEXT NULL, created_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS outbound_queue_destination_priority_id "
                "ON outbound_queue (telegram_chat_id, priority DESC, id ASC)"
            )
            connection.commit()
            self._connection = connection
            self.refresh_depth()
        except Exception:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()
            raise

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
            self.metrics.record_removal(row.priority, row.operation, outcome, time.time() - row.created_at)
        self.refresh_depth()

    @staticmethod
    def _snapshot_media_value(value: object) -> object:
        if isinstance(value, bytes):
            return value
        local_path = OutboundQueue._local_media_path(value)
        if local_path is not None:
            try:
                with local_path.open("rb") as source:
                    return OutboundQueue._snapshot_media_value(source)
            except (OSError, QueueEnqueueError) as error:
                raise QueueEnqueueError("Unable to serialize queued Telegram call.") from error
        if isinstance(value, str):
            return value
        if isinstance(value, InputFile):
            stored_content = value.input_file_content
            if isinstance(stored_content, bytes):
                input_content = stored_content
            else:
                captured = OutboundQueue._snapshot_media_value(stored_content)
                if not isinstance(captured, _InlineMediaSnapshot):
                    raise QueueEnqueueError("Unable to serialize queued Telegram call.")
                input_content = captured.content
            return _InlineMediaSnapshot(
                input_content, value.filename, True, value.attach_name, value.mimetype
            )
        tell = getattr(value, "tell", None)
        seek = getattr(value, "seek", None)
        read = getattr(value, "read", None)
        if not callable(tell) or not callable(seek) or not callable(read):
            raise QueueEnqueueError("Unable to serialize queued Telegram call.")

        previous_position: Optional[int] = None
        read_error: Optional[Exception] = None
        content: object = None
        try:
            previous_position = tell()
            seek(0)
            content = read()
        except Exception as error:
            read_error = error
        finally:
            if previous_position is not None:
                try:
                    seek(previous_position)
                except Exception as error:
                    raise QueueEnqueueError("Unable to serialize queued Telegram call.") from error
        if read_error is not None:
            raise QueueEnqueueError("Unable to serialize queued Telegram call.") from read_error
        if not isinstance(content, bytes):
            raise QueueEnqueueError("Unable to serialize queued Telegram call.")

        source_name = getattr(value, "name", None)
        filename = Path(source_name).name if isinstance(source_name, (str, Path)) else None
        return _InlineMediaSnapshot(content, filename or None)

    @staticmethod
    def _local_media_path(value: object) -> Optional[Path]:
        if isinstance(value, Path):
            return value
        if not isinstance(value, str) or value.startswith(("http://", "https://")):
            return None
        try:
            path = Path(value)
            return path if path.is_file() else None
        except OSError:
            return None

    @classmethod
    def _needs_media_snapshot(cls, value: object) -> bool:
        return cls._local_media_path(value) is not None or isinstance(value, InputFile) or any(
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

    @classmethod
    def _normalize_input_media(cls, value: object) -> object:
        if not isinstance(value, InputMedia):
            return value
        normalized = copy.copy(value)
        cls._validate_media_value(value.media, _NESTED_MEDIA_TYPES.get(type(value)))
        if cls._needs_media_snapshot(value.media):
            object.__setattr__(normalized, "media", cls._snapshot_media_value(value.media))
        thumbnail = getattr(value, "thumbnail", None)
        if thumbnail is not None:
            cls._validate_media_value(thumbnail)
            if cls._needs_media_snapshot(thumbnail):
                object.__setattr__(normalized, "thumbnail", cls._snapshot_media_value(thumbnail))
        return normalized

    @classmethod
    def _normalize_direct_media(
        cls, operation: str, args: tuple, kwargs: dict
    ) -> tuple[tuple, dict]:
        argument = _DIRECT_MEDIA_ARGUMENTS.get(operation)
        if argument is None:
            return args, kwargs
        index, keyword = argument.index, argument.keyword
        positional = len(args) > index
        if not positional and keyword not in kwargs:
            return args, kwargs
        value = args[index] if positional else kwargs[keyword]
        cls._validate_media_value(value, argument.telegram_type)
        if not cls._needs_media_snapshot(value):
            return args, kwargs
        snapshot = cls._snapshot_media_value(value)
        explicit_filename = kwargs.get("filename")
        if explicit_filename is not None and not isinstance(explicit_filename, str):
            raise QueueEnqueueError("Unable to serialize queued Telegram call.")
        if isinstance(snapshot, _InlineMediaSnapshot) and explicit_filename is not None:
            snapshot = _InlineMediaSnapshot(
                snapshot.content, explicit_filename, snapshot.input_file,
                snapshot.attach_name, None if snapshot.input_file else snapshot.mimetype
            )
        if positional:
            normalized_args = list(args)
            normalized_args[index] = snapshot
            return tuple(normalized_args), kwargs
        normalized_kwargs = dict(kwargs)
        normalized_kwargs[keyword] = snapshot
        return args, normalized_kwargs

    @classmethod
    def _normalize_nested_media(cls, operation: str, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
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
                isinstance(item, InputMedia) for item in value
            ):
                raise QueueEnqueueError("Unable to serialize queued Telegram call.")
            normalized = type(value)(cls._normalize_input_media(item) for item in value)
        else:
            if not isinstance(value, InputMedia):
                raise QueueEnqueueError("Unable to serialize queued Telegram call.")
            normalized = cls._normalize_input_media(value)
        if positional:
            normalized_args = list(args)
            normalized_args[index] = normalized
            return tuple(normalized_args), kwargs
        normalized_kwargs = dict(kwargs)
        normalized_kwargs[keyword] = normalized
        return args, normalized_kwargs

    @classmethod
    def _normalize_thumbnail(cls, operation: str, kwargs: dict) -> dict:
        thumbnail = kwargs.get("thumbnail")
        if operation not in _THUMBNAIL_OPERATIONS or thumbnail is None:
            return kwargs
        cls._validate_media_value(thumbnail)
        if not cls._needs_media_snapshot(thumbnail):
            return kwargs
        normalized_kwargs = dict(kwargs)
        normalized_kwargs["thumbnail"] = cls._snapshot_media_value(thumbnail)
        return normalized_kwargs

    @classmethod
    def _normalize_keyword_media(cls, operation: str, kwargs: dict) -> dict:
        keywords = _KEYWORD_MEDIA_ARGUMENTS.get(operation, ())
        normalized_kwargs = kwargs
        for keyword in keywords:
            value = normalized_kwargs.get(keyword)
            if value is None:
                continue
            cls._validate_media_value(value)
            if not cls._needs_media_snapshot(value):
                continue
            snapshot = cls._snapshot_media_value(value)
            if normalized_kwargs is kwargs:
                normalized_kwargs = dict(kwargs)
            normalized_kwargs[keyword] = snapshot
        return normalized_kwargs

    @staticmethod
    def encode_payload(args: tuple, kwargs: dict) -> bytes:
        try:
            return b"\x01" + pickle.dumps((args, kwargs), protocol=5)
        except Exception as error:
            raise QueueEnqueueError("Unable to serialize queued Telegram call.") from error

    @staticmethod
    def decode_payload(payload: bytes) -> tuple[tuple, dict]:
        if not payload or payload[0] != 1:
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
        elif required_sender is not None and required_sender != "__main__":
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

    def _prepare(self, request: QueueRequest, operation: Callable[..., object]) -> tuple[str, tuple, dict, int, int, Optional[str], Optional[str], bytes]:
        if not isinstance(request.operation, str) or not isinstance(request.args, tuple) or not isinstance(request.kwargs, dict):
            raise QueueEnqueueError("Queue request must be (operation: str, args: tuple, kwargs: dict).")
        telegram_kwargs, priority, slave_id, required_sender = self._validate_metadata(
            request.operation, request.kwargs
        )
        telegram_args, telegram_kwargs = self._normalize_direct_media(
            request.operation, request.args, telegram_kwargs
        )
        telegram_args, telegram_kwargs = self._normalize_nested_media(
            request.operation, telegram_args, telegram_kwargs
        )
        telegram_kwargs = self._normalize_thumbnail(request.operation, telegram_kwargs)
        telegram_kwargs = self._normalize_keyword_media(request.operation, telegram_kwargs)
        chat_id = self._destination(operation, telegram_args, telegram_kwargs)
        payload = self.encode_payload(telegram_args, telegram_kwargs)
        return request.operation, telegram_args, telegram_kwargs, chat_id, priority, slave_id, required_sender, payload

    def enqueue_many(
        self, requests: Iterable[QueueRequest], operation_resolver: Callable[[str], Callable[..., object]]
    ) -> tuple[int, Future]:
        request_list = list(requests)
        if not request_list:
            raise QueueEnqueueError("Queued request sequence cannot be empty.")
        prepared = [self._prepare(request, operation_resolver(request.operation)) for request in request_list]
        destinations = {(item[3], item[4]) for item in prepared}
        if len(destinations) != 1:
            raise QueueEnqueueError("Queued request sequence must share chat_id and priority.")
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                identifiers: list[int] = []
                now = time.time()
                for operation, _args, _kwargs, chat_id, priority, slave_id, required_sender, payload in prepared:
                    cursor = self.connection.execute(
                        "INSERT INTO outbound_queue "
                        "(priority, telegram_chat_id, operation, payload, slave_id, required_sender_bot_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (priority, chat_id, operation, payload, slave_id, required_sender, now),
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
                raise QueueEnqueueError("Unable to commit queued Telegram call.") from error
            if self.metrics is not None:
                for operation, _args, _kwargs, _chat_id, priority, _slave_id, _required_sender, _payload in prepared:
                    self.metrics.record_enqueued(priority, operation)
            self.refresh_depth()
            waiter: Future = Future()
            self.waiters[identifiers[0]] = waiter
            return identifiers[0], waiter

    def heads(self) -> list[QueuedCall]:
        with self._lock:
            # The current bounded queue is read in sort order so Python can
            # preserve priority/FIFO semantics while selecting one row per destination.
            rows = self.connection.execute(
                "SELECT id, priority, telegram_chat_id, operation, payload, slave_id, "
                "required_sender_bot_id, created_at FROM outbound_queue "
                "ORDER BY telegram_chat_id ASC, priority DESC, id ASC"
            ).fetchall()
        heads: list[QueuedCall] = []
        destinations: set[int] = set()
        for row in rows:
            if int(row[2]) in destinations:
                continue
            destinations.add(int(row[2]))
            heads.append(QueuedCall(*row))
        return heads

    def delete(self, row_id: int) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN")
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

    def fail_waiter(self, row_id: int, error: BaseException) -> None:
        waiter = self.waiters.pop(row_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_exception(error)

    def fail_all_waiters(self, error: BaseException) -> None:
        for row_id in tuple(self.waiters):
            self.fail_waiter(row_id, error)


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
        self.next_deadline: Optional[float] = None

    @staticmethod
    def _sender_kind(selection: SenderSelection) -> str:
        return "main" if selection.sender_bot_id is None else "auxiliary"

    def _record_submitted_removal(self, row: QueuedCall) -> None:
        self.queue.record_removal(row, "submitted")
        if self.queue.metrics is not None:
            self.queue.metrics.record_dequeued(row.priority, row.operation)

    def _record_terminal_discard(self, row: QueuedCall) -> None:
        self.queue.record_removal(row, "terminal_discard")

    def _schedule_retry(self, retry_at: float) -> None:
        self.next_deadline = min(self.next_deadline, retry_at) if self.next_deadline else retry_at

    def _wake_on_future_completion(self, _future: Future) -> None:
        self.wake_event.set()

    def _stop_for_persistence_error(self, error: Exception) -> None:
        persistence_error = QueuePersistenceError("Outbound queue deletion failed.")
        persistence_error.__cause__ = error
        self.failure = persistence_error
        self.stopping = True
        self.queue.fail_all_waiters(persistence_error)
        self.wake_event.set()

    def dispatch_once(self) -> None:
        with self._lock:
            if self.stopping:
                return
            self.next_deadline = None
            for row in self.queue.heads():
                if row.telegram_chat_id in self.in_flight_destinations:
                    continue
                try:
                    args, kwargs = self.queue.decode_payload(row.payload)
                except InvalidQueuedPayloadError as error:
                    try:
                        self.queue.delete(row.id)
                    except Exception as delete_error:
                        self._stop_for_persistence_error(delete_error)
                        return
                    self._record_terminal_discard(row)
                    self.queue.fail_waiter(row.id, error)
                    continue
                if not self._permits.acquire(blocking=False):
                    continue
                now = time.monotonic()
                decision = self.adapter.select_sender(row, now)
                if decision.terminal_error_class is not None:
                    self._permits.release()
                    try:
                        self.queue.delete(row.id)
                    except Exception as delete_error:
                        self._stop_for_persistence_error(delete_error)
                        return
                    self._record_terminal_discard(row)
                    self.queue.fail_waiter(row.id, RequiredSenderUnavailableError(decision.terminal_error_class))
                    continue
                if decision.selection is None:
                    self._permits.release()
                    if decision.retry_at is not None:
                        self._schedule_retry(decision.retry_at)
                    continue
                if not self.adapter.acquire_sender_limits(decision.selection, row.telegram_chat_id):
                    self._permits.release()
                    self._schedule_retry(now + 0.25)
                    continue
                retained = row.priority == 0
                if not retained:
                    try:
                        self.queue.delete(row.id)
                    except Exception as delete_error:
                        self._permits.release()
                        self._stop_for_persistence_error(delete_error)
                        return
                    self._record_submitted_removal(row)
                try:
                    future = self.executor.submit(
                        self.adapter.execute_queued_call, row, args, kwargs, decision.selection
                    )
                except Exception as error:
                    self._permits.release()
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_dispatch_failure(row.priority, row.operation)
                    if retained:
                        self._schedule_retry(now + 0.25)
                    else:
                        self.queue.fail_waiter(row.id, ExecutorSubmitError("Unable to submit queued Telegram call."))
                    continue
                future.add_done_callback(self._wake_on_future_completion)
                self.in_flight[row.id] = SubmittedCall(row, decision.selection, future)
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
                    decision = self.adapter.record_queued_failure(submitted.row, error, submitted.selection)
                    if decision.kind == "retry_eventual" and submitted.row.priority == 0:
                        if self.stopping:
                            self.queue.fail_waiter(row_id, SchedulerStoppedError("Outbound scheduler stopped."))
                            continue
                        if decision.retry_at is None:
                            raise RuntimeError("Retry decision requires a retry deadline.")
                        self._schedule_retry(decision.retry_at)
                        continue
                    if submitted.row.priority == 0:
                        try:
                            self.queue.delete(row_id)
                        except Exception as delete_error:
                            self._stop_for_persistence_error(delete_error)
                            return
                        self._record_terminal_discard(submitted.row)
                    self.queue.fail_waiter(row_id, error)
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_completion(
                            submitted.row.priority, submitted.row.operation,
                            self._sender_kind(submitted.selection), "failure"
                        )
                else:
                    self.adapter.record_queued_success(submitted.row, result, submitted.selection)
                    if submitted.row.priority == 0:
                        try:
                            self.queue.delete(row_id)
                        except Exception as delete_error:
                            self._stop_for_persistence_error(delete_error)
                            return
                        self._record_submitted_removal(submitted.row)
                    waiter = self.queue.waiters.pop(row_id, None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(result)
                    if self.queue.metrics is not None:
                        self.queue.metrics.record_completion(
                            submitted.row.priority, submitted.row.operation,
                            self._sender_kind(submitted.selection), "success"
                        )
            if any_harvested:
                self.wake_event.set()

    def stop_and_drain(self, timeout: float = 5.0) -> None:
        with self._lock:
            self.stopping = True
            stopped_error = SchedulerStoppedError("Outbound scheduler stopped.")
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
                if submitted.future.done():
                    continue
                self.in_flight.pop(row_id)
                self.in_flight_destinations.discard(submitted.row.telegram_chat_id)
                self._permits.release()
                if self.queue.metrics is not None:
                    self.queue.metrics.decrement_in_flight(
                        submitted.row.priority, submitted.row.operation, self._sender_kind(submitted.selection)
                    )
                self.queue.fail_waiter(row_id, SchedulerStoppedError("Outbound scheduler stopped."))
