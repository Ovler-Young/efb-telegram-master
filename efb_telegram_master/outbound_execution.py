from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

import telegram.error
from telegram.error import RetryAfter

from .outbound_types import ExecutorSubmitError, OutboundLifecycle, QueueError, SchedulerStoppedError, SenderSelection, cleanup_upload_paths, rewind_uploads
from .sender_policy import retry_after_seconds
from .transport.telegram_calls import PrimaryExecution

if TYPE_CHECKING:
    from .outbound import OutboundQueue, _PendingCall


_TRANSPORT_RETRY_LIMIT = 2
_TRANSPORT_RETRY_DELAY = 0.05


class OutboundExecution:
    """Run queued calls while leaving lifecycle and admission with OutboundQueue."""

    def __init__(self, queue: OutboundQueue, on_quiescent: Callable[[], None], finalize_resources: Callable[[], None]) -> None:
        self._queue = queue
        self._on_quiescent = on_quiescent
        self._finalize_resources = finalize_resources

    def run(self) -> None:
        try:
            while not self._queue._stop_event.is_set():
                self.harvest_completed()
                self.dispatch_once()
                self._queue._wake_event.wait(timeout=self.sleep_timeout())
                self._queue._wake_event.clear()
        finally:
            self.drain_in_flight()
            self._on_quiescent()
            self._finalize_resources()

    def sleep_timeout(self) -> float:
        with self._queue._lock:
            deadlines = [pending.retry_at for pending in self._queue._pending if pending.retry_at > 0]
        return max(0.0, min(0.25, min(deadlines) - time.monotonic())) if deadlines else 0.25

    def dispatch_once(self) -> None:
        now = time.monotonic()
        with self._queue._lock:
            if self._queue._lifecycle is not OutboundLifecycle.RUNNING:
                return
            blocked_chats: set[int] = set()
            for _ in range(len(self._queue._pending)):
                if not self._queue._capacity.acquire(blocking=False):
                    return
                pending = self._queue._pending.popleft()
                if pending.waiter.cancelled():
                    cleanup_upload_paths(pending.call.cleanup)
                    self._queue._clear_chat_redirect_locked(pending)
                    self._queue._record_outcome(pending, "cancelled")
                    self._queue._capacity.release()
                    continue
                chat_id = pending.call.telegram_chat_id
                if chat_id in blocked_chats:
                    self._queue._pending.append(pending)
                    self._queue._capacity.release()
                    continue
                blocked_chats.add(chat_id)
                if pending.retry_at > now or chat_id in self._queue._in_flight_chats:
                    self._queue._pending.append(pending)
                    self._queue._capacity.release()
                    continue
                decision = self._queue._sender_policy.select(pending.active_call(), now)
                if decision.error is not None:
                    self._queue._record_outcome(pending, "failure")
                    self._queue._complete_pending_locked(pending, error=QueueError(decision.error.replace("_", " ")))
                    self._queue._capacity.release()
                    continue
                if decision.selection is None:
                    pending.retry_at = decision.retry_at or now + 0.1
                    self._queue._pending.append(pending)
                    self._queue._capacity.release()
                    continue
                if not self._queue._sender_policy.acquire(decision.selection, chat_id):
                    pending.retry_at = now + 0.1
                    self._queue._pending.append(pending)
                    self._queue._capacity.release()
                    continue
                try:
                    self._queue._submit_execution_locked(pending, decision.selection)
                except BaseException as error:
                    self._queue._record_outcome(pending, "failure")
                    self._queue._complete_pending_locked(pending, error=ExecutorSubmitError(str(error)))
                    self._queue._capacity.release()
                    continue

    def harvest_completed(self) -> None:
        with self._queue._lock:
            completed = [future for future in self._queue._in_flight if future.done()]
            for future in completed:
                submitted = self._queue._in_flight.pop(future)
                pending = submitted.pending
                self._queue._in_flight_chats.discard(pending.call.telegram_chat_id)
                self._queue._capacity.release()
                if pending.waiter.cancelled():
                    cleanup_upload_paths(pending.call.cleanup)
                    self._queue._clear_chat_redirect_locked(pending)
                    self._queue._record_outcome(pending, "cancelled")
                    continue
                try:
                    result = future.result()
                except RetryAfter as error:
                    self._queue._sender_policy.record_retry_after(pending.active_call(), error, submitted.selection)
                    self._queue._record_retry(pending.active_call(), "rate_limit")
                    try:
                        rewind_uploads(pending.active_call().args, pending.active_call().kwargs)
                    except BaseException as rewind_error:
                        self.finish_terminal_error(pending, submitted.selection, rewind_error)
                    else:
                        pending.retry_at = time.monotonic() + retry_after_seconds(error)
                        self.requeue_or_stop(pending)
                except telegram.error.NetworkError as error:
                    if pending.transport_retries >= _TRANSPORT_RETRY_LIMIT:
                        self.finish_terminal_error(pending, submitted.selection, error)
                        continue
                    try:
                        rewind_uploads(pending.active_call().args, pending.active_call().kwargs)
                    except BaseException as rewind_error:
                        self.finish_terminal_error(pending, submitted.selection, rewind_error)
                    else:
                        pending.transport_retries += 1
                        pending.retry_at = time.monotonic() + _TRANSPORT_RETRY_DELAY * pending.transport_retries
                        self._queue._record_retry(pending.active_call(), "transport")
                        self.requeue_or_stop(pending)
                except telegram.error.ChatMigrated as error:
                    if pending.phase.value == "primary":
                        self._queue._record_retry(pending.active_call(), "migration")
                        self._queue._record_outcome(pending, "failure")
                        pending.waiter.set_exception(error)
                    elif pending.attachment_migrated:
                        self.finish_terminal_error(pending, submitted.selection, QueueError("Attachment chat migrated repeatedly."))
                    else:
                        try:
                            self._queue._migrate_attachment_locked(pending, error.new_chat_id)
                        except BaseException as migration_error:
                            self.finish_terminal_error(pending, submitted.selection, migration_error)
                        else:
                            self.requeue_or_stop(pending)
                except BaseException as error:
                    self.finish_terminal_error(pending, submitted.selection, error)
                else:
                    self.complete_success(pending, result, submitted.selection)
            if completed:
                self._queue._wake_event.set()

    def requeue_or_stop(self, pending: _PendingCall) -> None:
        if self._queue._lifecycle is OutboundLifecycle.RUNNING:
            self._queue._pending.appendleft(pending)
        else:
            self._queue._record_outcome(pending, "cancelled")
            self._queue._complete_pending_locked(pending, error=SchedulerStoppedError("Outbound queue stopped."))

    def complete_success(self, pending: _PendingCall, result: object, selection: SenderSelection) -> None:
        if pending.phase.value == "primary":
            assert isinstance(result, PrimaryExecution)
            if result.attachment is not None:
                pending.primary_result, pending.attachment, pending.phase, pending.retry_at = result.receipt, result.attachment, type(pending.phase).ATTACHMENT, 0.0
                if self._queue._lifecycle is OutboundLifecycle.RUNNING:
                    self._queue._call_adapter.record_successful_send(pending.call, selection)
                    self._queue._pending.appendleft(pending)
                else:
                    self._queue._record_outcome(pending, "cancelled")
                    self._queue._complete_pending_locked(pending, error=SchedulerStoppedError("Outbound queue stopped."))
                return
            receipt = result.receipt
            self._queue._call_adapter.record_successful_send(pending.call, selection)
            self._queue._record_outcome(pending, "success")
            self._queue._complete_pending_locked(pending, result=receipt)
            return
        assert pending.primary_result is not None
        self._queue._record_outcome(pending, "success")
        self._queue._complete_pending_locked(pending, result=pending.primary_result)

    def finish_terminal_error(self, pending: _PendingCall, selection: SenderSelection, error: BaseException) -> None:
        if pending.phase.value == "attachment" and pending.primary_result is not None:
            self._queue._record_outcome(pending, "attachment_failure")
            self._queue._complete_pending_locked(pending, error=error)
            return
        self._queue._sender_policy.record_send_failure(pending.call, selection)
        self._queue._record_outcome(pending, "failure")
        self._queue._complete_pending_locked(pending, error=error)

    def drain_in_flight(self) -> None:
        while True:
            self.harvest_completed()
            with self._queue._lock:
                if not self._queue._in_flight:
                    return
            time.sleep(0.01)
