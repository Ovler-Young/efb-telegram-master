from __future__ import annotations

import collections
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional, Protocol

from efb_telegram_master.outbound.outbound_execution import OutboundExecution
from efb_telegram_master.outbound.outbound_types import (
    OutboundLifecycle,
    OutboundShutdownTimeout,
    QueuedCall,
    QueueEnqueueError,
    QueueError,
    QueueFuture,
    QueueRequest,
    SchedulerStoppedError,
    SenderSelection,
    SendReceipt,
    cleanup_upload_paths,
    rewind_uploads,
)
from efb_telegram_master.outbound.sender_policy import SenderPolicy
from efb_telegram_master.transport.telegram_calls import QUEUED_OPERATIONS, TelegramCallAdapter

if TYPE_CHECKING:
    from efb_telegram_master.runtime.bot_pool import BotPool
    from efb_telegram_master.runtime.rate_limiter import SlidingWindowRateLimiter


_CHAT_ID_ARGUMENT_INDICES = {"edit_message_text": 1}
DEFAULT_MAX_PENDING = 1000


class OutboundMetrics(Protocol):
    def record_outbound_outcome(self, operation: str, outcome: str, seconds: float) -> None: ...

    def record_outbound_retry(self, operation: str, reason: str) -> None: ...

    def record_outbound_saturation(self, reason: str) -> None: ...


class _CallPhase(Enum):
    PRIMARY = "primary"
    ATTACHMENT = "attachment"


@dataclass
class _PendingCall:
    call: QueuedCall
    waiter: QueueFuture
    retry_at: float = 0.0
    phase: _CallPhase = field(default_factory=lambda: _CallPhase.PRIMARY)
    primary_result: Optional[SendReceipt] = None
    attachment: Optional[QueuedCall] = None
    attachment_migrated: bool = False
    redirect_source: Optional[int] = None
    transport_retries: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)

    def active_call(self) -> QueuedCall:
        if self.phase is _CallPhase.PRIMARY:
            return self.call
        assert self.attachment is not None
        return self.attachment


@dataclass
class _SubmittedCall:
    pending: _PendingCall
    selection: SenderSelection


def _call_with_chat_id(call: QueuedCall, chat_id: int) -> QueuedCall:
    args = list(call.args)
    kwargs = dict(call.kwargs)
    argument_index = _CHAT_ID_ARGUMENT_INDICES.get(call.operation, 0)
    if "chat_id" in kwargs:
        kwargs["chat_id"] = chat_id
    else:
        args[argument_index] = chat_id
    return replace(call, args=tuple(args), kwargs=kwargs, telegram_chat_id=chat_id)


class OutboundQueue:
    def __init__(
        self,
        main_bot: object,
        bot_pool: Optional[BotPool],
        main_rate_limiter: SlidingWindowRateLimiter,
        *,
        worker_count: int,
        blocking_timeout: float,
        shutdown_drain_timeout: float,
        shutdown_join_grace: float,
        cancel_active_calls: Callable[[], None] | None = None,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self._sender_policy = SenderPolicy(main_bot, bot_pool, main_rate_limiter)
        self._call_adapter = TelegramCallAdapter(bot_pool)
        self._blocking_timeout = blocking_timeout
        self._shutdown_drain_timeout = shutdown_drain_timeout
        self._shutdown_join_grace = shutdown_join_grace
        self._cancel_active_calls = cancel_active_calls
        if max_pending < 0:
            raise ValueError("max_pending must be non-negative")
        self._max_pending = max_pending
        self._executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ETM-send")
        self._pending: collections.deque[_PendingCall] = collections.deque()
        self._in_flight: dict[Future[object], _SubmittedCall] = {}
        self._in_flight_chats: set[int] = set()
        self._chat_redirects: dict[int, int] = {}
        self._capacity = threading.BoundedSemaphore(worker_count)
        self._lock = threading.RLock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._started = False
        self._lifecycle = OutboundLifecycle.RUNNING
        self._finalizing = False
        self._finalized = threading.Event()
        self._metrics: OutboundMetrics | None = None
        self._execution = OutboundExecution(self, self._mark_quiescent, self._finalize_resources)
        self._worker = threading.Thread(target=self._execution.run, name="ETM queued send worker", daemon=True)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if self._lifecycle is not OutboundLifecycle.RUNNING:
                raise SchedulerStoppedError("Outbound queue stopped.")
            self._started = True
        self._worker.start()

    def enqueue(self, request: QueueRequest) -> QueueFuture:
        if request.operation not in QUEUED_OPERATIONS:
            cleanup_upload_paths(request.cleanup)
            raise QueueEnqueueError(f"Unsupported queued operation: {request.operation}")
        waiter: QueueFuture = Future()
        call = QueuedCall(request.operation, request.args, dict(request.kwargs), request.telegram_chat_id, request.slave_id, request.required_sender_bot_id, request.cleanup)
        with self._lock:
            if self._lifecycle is not OutboundLifecycle.RUNNING:
                cleanup_upload_paths(request.cleanup)
                raise SchedulerStoppedError("Outbound queue stopped.")
            if len(self._pending) >= self._max_pending:
                cleanup_upload_paths(request.cleanup)
                self._record_rejected(request.operation)
                self._record_saturation("pending_capacity")
                raise QueueEnqueueError("Outbound queue pending capacity reached.")
            pending = _PendingCall(_call_with_chat_id(call, self._resolve_chat_id_locked(call.telegram_chat_id)), waiter)
            self._pending.append(pending)
            waiter.add_done_callback(self._discard_cancelled_pending)
            self._record_outcome(pending, "enqueued")
            self._wake_event.set()
        return waiter

    def bind_metrics(self, metrics: OutboundMetrics) -> None:
        with self._lock:
            self._metrics = metrics

    def enqueue_and_wait(self, request: QueueRequest) -> SendReceipt:
        try:
            return self.enqueue(request).result(timeout=self._blocking_timeout)
        except FutureTimeoutError as error:
            raise RuntimeError(f"Telegram call to chat {request.telegram_chat_id} timed out after {self._blocking_timeout:g}s") from error

    def stop(self, deadline: float | None = None) -> None:
        if deadline is None:
            deadline = time.monotonic() + self._shutdown_drain_timeout + self._shutdown_join_grace
        with self._lock:
            if self._lifecycle is OutboundLifecycle.FINALIZED:
                return
            if self._lifecycle is OutboundLifecycle.RUNNING:
                self._lifecycle = OutboundLifecycle.STOPPING
                self._fail_pending_locked()
                self._stop_event.set()
                self._wake_event.set()
            submitted, started = tuple(self._in_flight), self._started
        if self._cancel_active_calls is not None:
            self._cancel_active_calls()
        for future in submitted:
            future.cancel()
        if not started:
            self._mark_quiescent()
            self._finalize_resources()
        if not self._finalized.wait(max(0.0, deadline - time.monotonic())):
            raise OutboundShutdownTimeout("Outbound queue did not stop before the shutdown deadline.")

    def destination_snapshot(self) -> list[tuple[str, int, float]]:
        with self._lock:
            destinations: dict[int, list[float]] = {}
            for pending in self._pending:
                destinations.setdefault(pending.call.telegram_chat_id, []).append(pending.enqueued_at)
            now = time.monotonic()
            ranked = sorted(destinations.values(), key=len, reverse=True)
            return [(f"rank_{index}", len(enqueued_at), max(0.0, now - min(enqueued_at))) for index, enqueued_at in enumerate(ranked, start=1)]

    @property
    def lifecycle(self) -> OutboundLifecycle:
        with self._lock:
            return self._lifecycle

    def worker_snapshot(self) -> tuple[bool, int]:
        with self._lock:
            return self._worker.is_alive(), len(self._in_flight)

    def cooldown_snapshot(self) -> dict[str, float]:
        return self._sender_policy.cooldown_snapshot()

    def rate_limit_occupancy_snapshot(self) -> dict[str, float]:
        return self._sender_policy.rate_limit_occupancy_snapshot()

    def _submit_execution_locked(self, pending: _PendingCall, selection: SenderSelection) -> None:
        execute = self._call_adapter.execute_primary if pending.phase is _CallPhase.PRIMARY else self._call_adapter.execute_attachment
        future = self._executor.submit(execute, pending.active_call(), selection)
        self._in_flight[future] = _SubmittedCall(pending, selection)
        self._in_flight_chats.add(pending.call.telegram_chat_id)

    def _resolve_chat_id_locked(self, chat_id: int) -> int:
        seen: set[int] = set()
        while chat_id in self._chat_redirects and chat_id not in seen:
            seen.add(chat_id)
            chat_id = self._chat_redirects[chat_id]
        return chat_id

    def _migrate_attachment_locked(self, pending: _PendingCall, new_chat_id: int) -> None:
        old_chat_id, new_chat_id = pending.call.telegram_chat_id, self._resolve_chat_id_locked(new_chat_id)
        if old_chat_id == new_chat_id:
            raise QueueError("Attachment chat migration did not change the destination.")
        rewind_uploads(pending.active_call().args, pending.active_call().kwargs)
        self._chat_redirects[old_chat_id] = new_chat_id
        pending.redirect_source = old_chat_id
        for queued in self._pending:
            resolved_chat_id = self._resolve_chat_id_locked(queued.call.telegram_chat_id)
            if resolved_chat_id != queued.call.telegram_chat_id:
                queued.call = _call_with_chat_id(queued.call, resolved_chat_id)
                if queued.attachment is not None:
                    queued.attachment = _call_with_chat_id(queued.attachment, resolved_chat_id)
        pending.call = _call_with_chat_id(pending.call, new_chat_id)
        assert pending.attachment is not None
        pending.attachment = _call_with_chat_id(pending.attachment, new_chat_id)
        pending.attachment_migrated, pending.retry_at = True, 0.0

    def _clear_chat_redirect_locked(self, pending: _PendingCall) -> None:
        if pending.redirect_source is not None:
            self._chat_redirects.pop(pending.redirect_source, None)
            pending.redirect_source = None

    def _fail_pending_locked(self) -> None:
        while self._pending:
            pending = self._pending.popleft()
            self._record_outcome(pending, "cancelled")
            self._complete_pending_locked(pending, error=SchedulerStoppedError("Outbound queue stopped."))

    def _complete_pending_locked(self, pending: _PendingCall, *, result: Optional[SendReceipt] = None, error: Optional[BaseException] = None) -> None:
        cleanup_upload_paths(pending.call.cleanup)
        self._clear_chat_redirect_locked(pending)
        if pending.waiter.done():
            return
        if error is not None:
            pending.waiter.set_exception(error)
        else:
            assert result is not None
            pending.waiter.set_result(result)

    def _discard_cancelled_pending(self, waiter: QueueFuture) -> None:
        if not waiter.cancelled():
            return
        with self._lock:
            retained: collections.deque[_PendingCall] = collections.deque()
            while self._pending:
                pending = self._pending.popleft()
                if pending.waiter is waiter:
                    cleanup_upload_paths(pending.call.cleanup)
                    self._clear_chat_redirect_locked(pending)
                    self._record_outcome(pending, "cancelled")
                else:
                    retained.append(pending)
            self._pending = retained
            self._wake_event.set()

    def _record_outcome(self, pending: _PendingCall, outcome: str) -> None:
        if self._metrics is not None:
            self._metrics.record_outbound_outcome(pending.call.operation, outcome, time.monotonic() - pending.enqueued_at)

    def _record_retry(self, call: QueuedCall, reason: str) -> None:
        if self._metrics is not None:
            self._metrics.record_outbound_retry(call.operation, reason)

    def _record_rejected(self, operation: str) -> None:
        if self._metrics is not None:
            self._metrics.record_outbound_outcome(operation, "rejected", 0.0)

    def _record_saturation(self, reason: str) -> None:
        if self._metrics is not None:
            self._metrics.record_outbound_saturation(reason)

    def _finalize_resources(self) -> None:
        with self._lock:
            if self._lifecycle is OutboundLifecycle.FINALIZED or self._lifecycle is not OutboundLifecycle.QUIESCENT or self._finalizing:
                return
            self._finalizing = True
        self._executor.shutdown(wait=True)
        with self._lock:
            self._lifecycle = OutboundLifecycle.FINALIZED
            self._finalized.set()

    def _mark_quiescent(self) -> None:
        with self._lock:
            if self._lifecycle is OutboundLifecycle.STOPPING:
                self._lifecycle = OutboundLifecycle.QUIESCENT
