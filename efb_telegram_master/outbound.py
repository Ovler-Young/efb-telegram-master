"""In-memory, per-chat serialized Telegram Bot API dispatch."""

from __future__ import annotations

import collections
import time
import threading
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from typing import Callable, Optional

from telegram.error import RetryAfter


QUEUED_OPERATIONS = frozenset({
    "send_message", "send_document", "send_photo", "send_audio",
    "send_video", "send_animation", "send_voice", "send_sticker",
    "send_media_group", "copy_message", "forward_message",
    "edit_message_text", "edit_message_caption", "edit_message_media",
    "delete_message", "edit_message_reply_markup", "send_location",
    "send_venue", "create_forum_topic", "edit_forum_topic",
    "reopen_forum_topic", "set_chat_title", "set_chat_photo",
    "pin_chat_message", "set_chat_description",
})


class QueueError(RuntimeError):
    pass


class QueueEnqueueError(QueueError):
    pass


class SchedulerStoppedError(QueueError):
    pass


class ExecutorSubmitError(QueueError):
    pass


@dataclass(frozen=True)
class QueueRequest:
    operation: str
    args: tuple
    kwargs: dict
    telegram_chat_id: int
    slave_id: Optional[str] = None
    required_sender_bot_id: Optional[str] = None


@dataclass(frozen=True)
class QueuedCall:
    operation: str
    args: tuple
    kwargs: dict
    telegram_chat_id: int
    slave_id: Optional[str]
    required_sender_bot_id: Optional[str]


@dataclass(frozen=True)
class SenderSelection:
    sender: object
    sender_bot_id: Optional[str]


@dataclass(frozen=True)
class SenderSelectionResult:
    selection: Optional[SenderSelection] = None
    retry_at: Optional[float] = None
    terminal_error_class: Optional[str] = None


def retry_after_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


@dataclass
class _PendingCall:
    call: QueuedCall
    waiter: Future
    retry_at: float = 0.0


@dataclass
class _SubmittedCall:
    pending: _PendingCall
    selection: SenderSelection
    future: Future


class OutboundQueueScheduler:
    """Dispatch ordinary Bot API calls with bounded concurrency and chat ordering."""

    def __init__(
        self,
        executor: Executor,
        worker_count: int,
        select_sender: Callable[[QueuedCall, float], SenderSelectionResult],
        acquire_sender_limits: Callable[[SenderSelection, int], bool],
        execute_call: Callable[[QueuedCall, SenderSelection], object],
        record_retry_after: Callable[[QueuedCall, RetryAfter, SenderSelection], None],
    ) -> None:
        self._executor = executor
        self._select_sender = select_sender
        self._acquire_sender_limits = acquire_sender_limits
        self._execute_call = execute_call
        self._record_retry_after = record_retry_after
        self._pending: collections.deque[_PendingCall] = collections.deque()
        self._in_flight: dict[Future, _SubmittedCall] = {}
        self._in_flight_chats: set[int] = set()
        self._capacity = threading.BoundedSemaphore(worker_count)
        self._lock = threading.RLock()
        self.wake_event = threading.Event()
        self.stopping = False

    def enqueue(self, request: QueueRequest) -> Future:
        if request.operation not in QUEUED_OPERATIONS:
            raise QueueEnqueueError(f"Unsupported queued operation: {request.operation}")
        waiter: Future = Future()
        pending = _PendingCall(
            QueuedCall(
                request.operation, request.args, dict(request.kwargs), request.telegram_chat_id,
                request.slave_id, request.required_sender_bot_id,
            ),
            waiter,
        )
        with self._lock:
            if self.stopping:
                raise SchedulerStoppedError("Outbound scheduler stopped.")
            self._pending.append(pending)
            self.wake_event.set()
        return waiter

    @property
    def next_deadline(self) -> Optional[float]:
        with self._lock:
            deadlines = [item.retry_at for item in self._pending if item.retry_at > 0]
        return min(deadlines) if deadlines else None

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def destination_snapshot(self) -> list[tuple[str, int, Optional[float]]]:
        with self._lock:
            depths: dict[int, int] = {}
            for pending in self._pending:
                depths[pending.call.telegram_chat_id] = depths.get(pending.call.telegram_chat_id, 0) + 1
        return [(str(chat_id), depth, None) for chat_id, depth in depths.items()]

    def dispatch_once(self) -> None:
        now = time.monotonic()
        with self._lock:
            for _ in range(len(self._pending)):
                if not self._capacity.acquire(blocking=False):
                    return
                pending = self._pending.popleft()
                call = pending.call
                if pending.retry_at > now or call.telegram_chat_id in self._in_flight_chats:
                    self._pending.append(pending)
                    self._capacity.release()
                    continue
                selection = self._select_sender(call, now)
                if selection.terminal_error_class is not None:
                    pending.waiter.set_exception(
                        QueueError(selection.terminal_error_class.replace("_", " "))
                    )
                    self._capacity.release()
                    continue
                if selection.retry_at is not None or selection.selection is None:
                    pending.retry_at = selection.retry_at or now + 0.1
                    self._pending.append(pending)
                    self._capacity.release()
                    continue
                if not self._acquire_sender_limits(selection.selection, call.telegram_chat_id):
                    pending.retry_at = now + 0.1
                    self._pending.append(pending)
                    self._capacity.release()
                    continue
                try:
                    future = self._executor.submit(self._execute_call, call, selection.selection)
                except BaseException as error:
                    pending.waiter.set_exception(ExecutorSubmitError(str(error)))
                    self._capacity.release()
                    continue
                self._in_flight[future] = _SubmittedCall(pending, selection.selection, future)
                self._in_flight_chats.add(call.telegram_chat_id)

    def harvest_completed(self) -> None:
        with self._lock:
            completed = [future for future in self._in_flight if future.done()]
            for future in completed:
                submitted = self._in_flight.pop(future)
                pending = submitted.pending
                self._in_flight_chats.discard(pending.call.telegram_chat_id)
                self._capacity.release()
                try:
                    pending.waiter.set_result(future.result())
                except RetryAfter as error:
                    self._record_retry_after(pending.call, error, submitted.selection)
                    pending.retry_at = time.monotonic() + retry_after_seconds(error)
                    self._pending.append(pending)
                except BaseException as error:
                    pending.waiter.set_exception(error)
            if completed:
                self.wake_event.set()

    def stop_and_drain(self, timeout: float = 5.0) -> None:
        with self._lock:
            self.stopping = True
            error = SchedulerStoppedError("Outbound scheduler stopped.")
            while self._pending:
                self._pending.popleft().waiter.set_exception(error)
            self.wake_event.set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.harvest_completed()
            with self._lock:
                if not self._in_flight:
                    return
            time.sleep(0.01)
        with self._lock:
            for submitted in self._in_flight.values():
                if not submitted.pending.waiter.done():
                    submitted.pending.waiter.set_exception(error)
