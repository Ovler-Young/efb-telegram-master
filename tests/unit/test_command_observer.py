import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master.bot_manager import TelegramBotManager, _inbound_command_context
from efb_telegram_master.command_observer import (
    CommandOutboundObserver,
    CommandOutboundState,
    InboundCommandKey,
)
from efb_telegram_master.outbound import (
    OutboundQueue,
    OutboundQueueScheduler,
    QueuePersistenceError,
    QueueRequest,
    SchedulerStoppedError,
    SenderSelection,
    SenderSelectionResult,
)


class _ImmediateExecutor:
    def submit(self, callback, *args):
        future = Future()
        try:
            future.set_result(callback(*args))
        except BaseException as error:
            future.set_exception(error)
        return future


class _PendingExecutor:
    def __init__(self) -> None:
        self.future = Future()

    def submit(self, _callback, *_args):
        return self.future


class _SchedulerObserverAdapter:
    def __init__(self, observer: CommandOutboundObserver) -> None:
        self.observer = observer
        self.stop_errors: list[BaseException] = []

    def select_sender(self, _row, _now):
        return SenderSelectionResult(selection=SenderSelection(object(), None))

    def acquire_sender_limits(self, _selection, _chat_id):
        return True

    def execute_queued_call(self, row, _args, _kwargs, _selection):
        return row.id

    def record_queued_failure(self, _row, _error, _selection):
        return SimpleNamespace(kind="terminal_failure", retry_at=None)

    def record_queued_retry_after(self, _row, _error, _selection):
        return None

    def record_queued_success(self, _row, _result, _selection):
        return SimpleNamespace(kind="success", retry_at=None)

    def _record_queued_scheduler_stop(self, error: BaseException) -> None:
        self.stop_errors.append(error)
        self.observer.shutdown(error)


def _enqueue_observed_command(queue, observer):
    row_id, _waiter = queue.enqueue_many(
        [QueueRequest("send_message", (), {"chat_id": 100, "text": "reply"})],
        lambda _operation: lambda chat_id, text: (chat_id, text),
    )
    inbound = InboundCommandKey(10, 20)
    observer.register(inbound, row_id, "send_message", 100)
    return inbound, row_id


def test_persistence_failure_terminalizes_observed_command_rows(tmp_path):
    queue = OutboundQueue(tmp_path)
    observer = CommandOutboundObserver()
    inbound, _row_id = _enqueue_observed_command(queue, observer)
    adapter = _SchedulerObserverAdapter(observer)
    scheduler = OutboundQueueScheduler(queue, adapter, _ImmediateExecutor(), worker_count=1)

    def fail_delete(_row_id):
        raise RuntimeError("database is unavailable")

    queue.delete = fail_delete
    scheduler.dispatch_once()
    scheduler.harvest_completed()

    outcome = observer.wait_for_completion(inbound, "send_message", 100, 0.01)
    assert outcome.state is CommandOutboundState.SHUTDOWN
    assert isinstance(outcome.error, QueuePersistenceError)
    assert len(adapter.stop_errors) == 1
    scheduler._stop_for_persistence_error(RuntimeError("second database error"))
    assert len(adapter.stop_errors) == 1
    queue.close()


def test_drain_timeout_terminalizes_observed_command_rows_once(tmp_path):
    queue = OutboundQueue(tmp_path)
    observer = CommandOutboundObserver()
    inbound, _row_id = _enqueue_observed_command(queue, observer)
    adapter = _SchedulerObserverAdapter(observer)
    scheduler = OutboundQueueScheduler(queue, adapter, _PendingExecutor(), worker_count=1)

    scheduler.dispatch_once()
    scheduler.stop_and_drain(timeout=0.0)

    outcome = observer.wait_for_completion(inbound, "send_message", 100, 0.01)
    assert outcome.state is CommandOutboundState.SHUTDOWN
    assert isinstance(outcome.error, SchedulerStoppedError)
    assert len(adapter.stop_errors) == 1
    scheduler.stop_and_drain(timeout=0.0)
    assert len(adapter.stop_errors) == 1
    queue.close()


def _register(observer, *, row_id=1, operation="send_message", target_chat_id=100):
    inbound = InboundCommandKey(10, 20)
    observer.register(inbound, row_id, operation, target_chat_id)
    return inbound


def test_observer_retains_quick_completion_for_exact_command_row():
    observer = CommandOutboundObserver()
    inbound = _register(observer)
    observer.succeed(1)

    outcome = observer.wait_for_completion(inbound, "send_message", 100, 0.01)

    assert outcome.state is CommandOutboundState.SUCCESS


def test_observer_does_not_match_same_chat_different_operation_or_command():
    observer = CommandOutboundObserver()
    inbound = _register(observer)
    observer.register(InboundCommandKey(10, 21), 2, "send_message", 100)
    observer.succeed(2)

    assert observer.snapshot(inbound, "send_message", 100) is not None
    assert observer.snapshot(inbound, "edit_message_text", 100) is None
    assert observer.snapshot(inbound, "send_message", 101) is None
    with pytest.raises(TimeoutError):
        observer.wait_for_completion(inbound, "edit_message_text", 100, 0.01)


def test_observer_keeps_retry_on_its_own_row_until_terminal_completion():
    observer = CommandOutboundObserver()
    inbound = _register(observer)
    observer.retry(1, 123.0)

    pending = observer.snapshot(inbound, "send_message", 100)
    assert pending is not None
    assert pending.state is CommandOutboundState.PENDING
    assert pending.retry_at == 123.0
    observer.succeed(1)
    assert observer.wait_for_completion(inbound, "send_message", 100, 0.01).state is CommandOutboundState.SUCCESS


def test_observer_reports_terminal_failure_and_shutdown():
    observer = CommandOutboundObserver()
    inbound = _register(observer)
    failure = RuntimeError("failed")
    observer.fail(1, failure)
    assert observer.wait_for_completion(inbound, "send_message", 100, 0.01).error is failure

    _register(observer, row_id=2)
    stopped = RuntimeError("stopped")
    observer.shutdown(stopped)
    outcome = observer.wait_for_completion(inbound, "send_message", 100, 0.01)
    assert outcome.row_id == 2
    assert outcome.state is CommandOutboundState.SHUTDOWN
    assert outcome.error is stopped
    observer.succeed(2)
    assert observer.snapshot(inbound, "send_message", 100).state is CommandOutboundState.SHUTDOWN


def test_observer_prunes_expired_rows_and_caps_memory():
    observer = CommandOutboundObserver(ttl=0.0)
    inbound = _register(observer)
    assert observer.snapshot(inbound, "send_message", 100) is None

    observer = CommandOutboundObserver(capacity=1)
    _register(observer, row_id=1)
    _register(observer, row_id=2)
    assert observer.snapshot(inbound, "send_message", 100).row_id == 2


@pytest.mark.asyncio
async def test_async_callback_sets_and_resets_inbound_context_only_for_updates(monkeypatch):
    manager = object.__new__(TelegramBotManager)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=10),
        effective_message=SimpleNamespace(message_id=20),
    )
    monkeypatch.setattr("efb_telegram_master.bot_manager.Update", type(update))
    observed = []

    def callback(_update, _context):
        observed.append(_inbound_command_context.get())

    await manager.as_async_callback(callback)(update, object())
    assert observed == [InboundCommandKey(10, 20)]
    assert _inbound_command_context.get() is None

    await manager.as_async_callback(callback)(object(), object())
    assert observed[-1] is None


@pytest.mark.asyncio
async def test_async_callback_context_does_not_reach_independent_queue_thread(monkeypatch):
    manager = object.__new__(TelegramBotManager)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=10),
        effective_message=SimpleNamespace(message_id=20),
    )
    monkeypatch.setattr("efb_telegram_master.bot_manager.Update", type(update))
    independent_context = []

    def callback(_update, _context):
        thread = threading.Thread(target=lambda: independent_context.append(_inbound_command_context.get()))
        thread.start()
        thread.join()

    await manager.as_async_callback(callback)(update, object())
    assert independent_context == [None]


def test_enqueue_registers_only_the_current_inbound_command_row():
    manager = object.__new__(TelegramBotManager)
    manager._command_outbound_observer = CommandOutboundObserver()
    manager._outbound_scheduler = SimpleNamespace(
        _lock=threading.RLock(), stopping=False, wake_event=Mock(),
    )
    manager._outbound_queue = SimpleNamespace(enqueue_many=Mock(return_value=(7, Mock())))

    def send_message(chat_id, text):
        return chat_id, text

    manager._queue_operation = Mock(return_value=send_message)
    token = _inbound_command_context.set(InboundCommandKey(10, 20))
    try:
        TelegramBotManager._enqueue_requests(
            manager, [
                SimpleNamespace(operation="send_message", args=(), kwargs={"chat_id": 100, "text": "reply"})
            ],
        )
    finally:
        _inbound_command_context.reset(token)

    outcome = manager._command_outbound_observer.snapshot(
        InboundCommandKey(10, 20), "send_message", 100,
    )
    assert outcome is not None
    assert outcome.row_id == 7
