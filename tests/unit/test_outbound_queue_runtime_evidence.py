"""Independent lifecycle evidence for the dequeue-only outbound queue."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from prometheus_client import generate_latest
from telegram.error import NetworkError, RetryAfter, TelegramError

import efb_telegram_master.outbound as outbound
from efb_telegram_master.bot_manager import TelegramBotManager
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.auxiliary_bot import AuxiliaryBot
from efb_telegram_master.outbound import (
    ExecutorSubmitError,
    InvalidQueuedPayloadError,
    OutboundQueue,
    OutboundQueueScheduler,
    QueuePersistenceError,
    QueueRequest,
    SchedulerStoppedError,
    SenderSelection,
    SenderSelectionResult,
)


def send_message(chat_id: int, text: str) -> tuple[int, str]:
    return chat_id, text


def enqueue(queue: OutboundQueue, chat_id: int, text: str = "message") -> tuple[int, Future]:
    return queue.enqueue_many(
        [QueueRequest("send_message", (), {"chat_id": chat_id, "text": text})],
        lambda _operation: send_message,
    )


class ControlledExecutor:
    """An executor whose submitted calls run only when the test resolves them."""

    def __init__(self, submit_error: BaseException | None = None) -> None:
        self.submit_error = submit_error
        self.submissions: list[tuple[object, tuple[object, ...], Future]] = []

    def submit(self, function, *args):
        if self.submit_error is not None:
            raise self.submit_error
        future: Future = Future()
        self.submissions.append((function, args, future))
        return future


class AlwaysAvailableLimiter:
    def try_acquire(self, _chat_id: int) -> bool:
        return True


def auxiliary_probe_publishing_non_membership(bot_id: int, chat_id: int) -> AuxiliaryBot:
    auxiliary = object.__new__(AuxiliaryBot)
    auxiliary.bot_id = bot_id
    auxiliary.username = "evidence-auxiliary"
    auxiliary.bot = object()
    auxiliary.disabled = False
    auxiliary.async_bot = SimpleNamespace(
        get_chat_member=lambda _chat_id, _bot_id: SimpleNamespace(status="left")
    )
    auxiliary._runtime = None
    auxiliary._membership_cache = {}
    auxiliary._membership_lock = threading.Lock()
    auxiliary._pending_probes = {chat_id}
    auxiliary._metrics = None
    auxiliary._membership_changed_callback = None
    return auxiliary


def manager_adapter() -> TelegramBotManager:
    """Build the public queue-adapter surface without starting a Telegram runtime."""
    manager = object.__new__(TelegramBotManager)
    manager._bot = object()
    manager._rate_limiter = AlwaysAvailableLimiter()
    manager._bot_chat_disabled_until = {}
    manager.bot_pool = None
    return manager


def test_membership_publication_not_telegram_failure_removes_only_triggering_affinity() -> None:
    manager = manager_adapter()
    manager._outbound_scheduler = SimpleNamespace(wake_event=threading.Event())
    auxiliary = auxiliary_probe_publishing_non_membership(17, 211)
    manager.bot_pool = BotPool([auxiliary], manager)
    manager.bot_pool.record_successful_auxiliary_send("slave-a", 17)
    manager.bot_pool.record_successful_auxiliary_send("slave-b", 17)
    row = SimpleNamespace(telegram_chat_id=211, slave_id="slave-a")
    selection = SenderSelection(auxiliary.bot, "17")

    manager.record_queued_failure(row, TelegramError("send failed"), selection)
    assert manager.bot_pool.preferred_sender("slave-a") is auxiliary

    auxiliary._probe_membership(211)

    assert manager._outbound_scheduler.wake_event.is_set()
    assert manager.bot_pool.preferred_sender("slave-a") is None
    assert manager.bot_pool.preferred_sender("slave-b") is auxiliary


def test_main_bot_remains_a_candidate_when_no_auxiliary_is_available() -> None:
    manager = manager_adapter()
    row = SimpleNamespace(telegram_chat_id=223, required_sender_bot_id=None, slave_id=None)

    decision = manager.select_sender(row, now=0.0)

    assert decision.selection is not None
    assert decision.selection.sender is manager._bot
    assert decision.selection.sender_bot_id is None


def test_enqueue_commit_wakes_scheduler_and_stopped_scheduler_rejects_without_row(
    retained_queue: OutboundQueue,
) -> None:
    manager = manager_adapter()
    scheduler = OutboundQueueScheduler(retained_queue, manager, ControlledExecutor(), worker_count=1)
    manager._outbound_queue = retained_queue
    manager._outbound_scheduler = scheduler
    manager._queue_operation = lambda _operation: send_message

    row_id, waiter = manager._enqueue_requests([
        QueueRequest("send_message", (), {"chat_id": 31, "text": "wake"})
    ])

    assert scheduler.wake_event.is_set()
    assert [row.id for row in retained_queue.heads()] == [int(row_id)]
    scheduler.stopping = True
    with pytest.raises(SchedulerStoppedError):
        manager._enqueue_requests([QueueRequest("send_message", (), {"chat_id": 32, "text": "stop"})])
    assert [row.id for row in retained_queue.heads()] == [int(row_id)]
    retained_queue.fail_waiter(int(row_id), SchedulerStoppedError("test cleanup"))
    with pytest.raises(SchedulerStoppedError):
        waiter.result()


def test_enqueue_commit_finishing_under_scheduler_lock_precedes_shutdown(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = manager_adapter()
    scheduler = OutboundQueueScheduler(retained_queue, manager, ControlledExecutor(), worker_count=1)
    manager._outbound_queue = retained_queue
    manager._outbound_scheduler = scheduler
    manager._queue_operation = lambda _operation: send_message
    committed = threading.Event()
    release_enqueue = threading.Event()
    shutdown_finished = threading.Event()
    enqueue_result: list[tuple[str, Future]] = []
    original_enqueue_many = retained_queue.enqueue_many

    def enqueue_then_hold(*args, **kwargs):
        result = original_enqueue_many(*args, **kwargs)
        committed.set()
        assert release_enqueue.wait(timeout=1.0)
        return result

    monkeypatch.setattr(retained_queue, "enqueue_many", enqueue_then_hold)

    producer = threading.Thread(
        target=lambda: enqueue_result.append(manager._enqueue_requests([
            QueueRequest("send_message", (), {"chat_id": 37, "text": "locked"})
        ])),
    )
    producer.start()
    assert committed.wait(timeout=1.0)
    assert retained_queue.heads()

    stopper = threading.Thread(
        target=lambda: (scheduler.stop_and_drain(timeout=0.0), shutdown_finished.set()),
    )
    stopper.start()
    assert not shutdown_finished.wait(timeout=0.05)
    assert not scheduler.stopping

    release_enqueue.set()
    producer.join(timeout=1.0)
    stopper.join(timeout=1.0)
    assert not producer.is_alive()
    assert not stopper.is_alive()
    assert shutdown_finished.is_set()
    row_id, waiter = enqueue_result[0]
    assert [row.id for row in retained_queue.heads()] == [int(row_id)]
    with pytest.raises(SchedulerStoppedError):
        waiter.result()


@dataclass
class RecordingAdapter:
    retry_at: float | None = None
    terminal: bool = False

    def __post_init__(self) -> None:
        self.failures: list[tuple[object, BaseException]] = []
        self.successes: list[tuple[object, object]] = []
        self.executed: list[int] = []

    def select_sender(self, row, now: float) -> SenderSelectionResult:
        if self.terminal:
            return SenderSelectionResult(terminal_error_class="required_sender_unavailable")
        if self.retry_at is not None and now < self.retry_at:
            return SenderSelectionResult(retry_at=self.retry_at)
        return SenderSelectionResult(selection=SenderSelection(object(), None))

    def acquire_sender_limits(self, _selection: SenderSelection, _chat_id: int) -> bool:
        return True

    def execute_queued_call(self, row, _args, _kwargs, _selection: SenderSelection) -> int:
        self.executed.append(row.id)
        return row.id

    def record_queued_failure(self, row, error: BaseException, _selection: SenderSelection) -> None:
        self.failures.append((row, error))

    def record_queued_success(self, row, result: object, _selection: SenderSelection) -> None:
        self.successes.append((row, result))


@pytest.fixture
def retained_queue(tmp_path: Path) -> OutboundQueue:
    queue = OutboundQueue(tmp_path)
    queue.close()
    restarted = OutboundQueue(tmp_path)
    yield restarted
    restarted.close()


def test_retry_after_failure_does_not_reenqueue_and_defers_only_later_sender_chat_work(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RetryAfter is a completed failure, while later work observes its cooldown."""
    first_id, first_waiter = enqueue(retained_queue, 41, "first")
    second_id, second_waiter = enqueue(retained_queue, 41, "second")
    executor = ControlledExecutor()
    adapter = manager_adapter()
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)

    clock = {"now": 100.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["now"])
    scheduler.dispatch_once()
    assert [row.id for row in retained_queue.heads()] == [second_id]
    assert len(executor.submissions) == 1

    retry_after = RetryAfter(10)
    executor.submissions[0][2].set_exception(retry_after)
    scheduler.harvest_completed()
    assert scheduler.wake_event.is_set()
    with pytest.raises(RetryAfter):
        first_waiter.result()
    assert not first_waiter.cancelled()
    assert not second_waiter.done()

    # The manager records the deadline during failure harvest.  The next pass
    # must retain the later row until it expires rather than dispatching it.
    scheduler.dispatch_once()
    assert len(executor.submissions) == 1
    assert scheduler.next_deadline == 110.0
    assert [row.id for row in retained_queue.heads()] == [second_id]

    clock["now"] = 110.0
    scheduler.dispatch_once()
    assert len(executor.submissions) == 2
    assert first_id not in {row.id for row in retained_queue.heads()}


def test_submitted_future_completion_wakes_scheduler(retained_queue: OutboundQueue) -> None:
    _row_id, _waiter = enqueue(retained_queue, 43, "completion wake")
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    scheduler.wake_event.clear()
    executor.submissions[0][2].set_result("sent")

    assert scheduler.wake_event.is_set()


def test_scheduler_publishes_metrics_for_actual_dequeue_and_completion(tmp_path: Path) -> None:
    metrics = Metrics()
    queue = OutboundQueue(tmp_path)
    queue.metrics = metrics
    row_id, waiter = enqueue(queue, 47, "metrics")
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_result("sent")
    scheduler.harvest_completed()

    assert waiter.result() == "sent"
    rendered = generate_latest(metrics.registry).decode()
    assert 'etm_outbound_enqueued_total{operation="send_message",priority="normal"} 1.0' in rendered
    assert "etm_outbound_queue_depth 0.0" in rendered
    assert 'etm_outbound_queue_removals_total{operation="send_message",outcome="submitted",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_dequeued_total{operation="send_message",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_in_flight{operation="send_message",priority="normal",sender_kind="main"} 0.0' in rendered
    assert 'etm_outbound_completions_total{operation="send_message",outcome="success",priority="normal",sender_kind="main"} 1.0' in rendered
    assert row_id not in scheduler.in_flight
    queue.close()


def test_queue_metrics_start_from_retained_rows_and_publish_terminal_discard(tmp_path: Path) -> None:
    retained = OutboundQueue(tmp_path)
    enqueue(retained, 53, "retained")
    retained.close()
    metrics = Metrics()
    queue = OutboundQueue(tmp_path, metrics=metrics)
    queue.connection.execute(
        "INSERT INTO outbound_queue "
        "(priority, telegram_chat_id, operation, payload, slave_id, required_sender_bot_id, created_at) "
        "VALUES (1, 53, 'send_message', X'02', NULL, NULL, 0)"
    )
    queue.connection.commit()
    queue.refresh_depth()
    scheduler = OutboundQueueScheduler(queue, RecordingAdapter(), ControlledExecutor(), worker_count=1)

    scheduler.dispatch_once()

    rendered = generate_latest(metrics.registry).decode()
    assert "etm_outbound_queue_depth 1.0" in rendered
    assert 'etm_outbound_queue_removals_total{operation="send_message",outcome="terminal_discard",priority="blocking"} 1.0' in rendered
    assert 'etm_outbound_queue_residence_seconds_count{operation="send_message",outcome="terminal_discard",priority="blocking"} 1.0' in rendered
    queue.close()


@pytest.mark.parametrize("failure", [TelegramError("telegram"), NetworkError("network"), CancelledError()])
def test_dequeued_call_failures_release_destination_without_reenqueue(
    retained_queue: OutboundQueue, failure: BaseException
) -> None:
    first_id, first_waiter = enqueue(retained_queue, 73, "first")
    second_id, second_waiter = enqueue(retained_queue, 73, "second")
    executor = ControlledExecutor()
    adapter = RecordingAdapter()
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(failure)
    scheduler.harvest_completed()

    with pytest.raises(type(failure)):
        first_waiter.result()
    assert first_id not in {row.id for row in retained_queue.heads()}
    assert first_id not in scheduler.in_flight
    assert 73 not in scheduler.in_flight_destinations
    assert len(adapter.failures) == 1
    assert adapter.failures[0][1] is failure

    scheduler.dispatch_once()
    assert len(executor.submissions) == 2
    assert second_id in scheduler.in_flight
    assert not second_waiter.done()


def test_executor_submit_failure_loses_dequeued_row_and_releases_destination(
    retained_queue: OutboundQueue,
) -> None:
    first_id, first_waiter = enqueue(retained_queue, 89, "first")
    second_id, _second_waiter = enqueue(retained_queue, 89, "second")
    adapter = RecordingAdapter()
    scheduler = OutboundQueueScheduler(
        retained_queue, adapter, ControlledExecutor(RuntimeError("executor unavailable")), worker_count=1
    )

    scheduler.dispatch_once()

    with pytest.raises(ExecutorSubmitError):
        first_waiter.result()
    assert first_id not in {row.id for row in retained_queue.heads()}
    assert 89 not in scheduler.in_flight_destinations
    assert scheduler.in_flight == {}

    scheduler.executor = ControlledExecutor()
    scheduler.dispatch_once()
    assert second_id in scheduler.in_flight


class DeleteStatementFailureConnection(sqlite3.Connection):
    fail_delete = False
    rollbacks = 0

    def execute(self, statement: str, parameters=()):
        if self.fail_delete and statement.startswith("DELETE FROM outbound_queue"):
            raise sqlite3.OperationalError("injected delete failure")
        return super().execute(statement, parameters)

    def rollback(self) -> None:
        type(self).rollbacks += 1
        super().rollback()


class DeleteCommitFailureConnection(sqlite3.Connection):
    fail_commit = False
    rollbacks = 0

    def commit(self) -> None:
        if self.fail_commit:
            raise sqlite3.OperationalError("injected delete commit failure")
        super().commit()

    def rollback(self) -> None:
        type(self).rollbacks += 1
        super().rollback()


@pytest.mark.parametrize(
    ("connection_type", "failure_attribute"),
    [(DeleteStatementFailureConnection, "fail_delete"), (DeleteCommitFailureConnection, "fail_commit")],
)
def test_delete_failures_rollback_fail_stop_and_retain_sqlite_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    connection_type: type[sqlite3.Connection],
    failure_attribute: str,
) -> None:
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        kwargs["factory"] = connection_type
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(outbound.sqlite3, "connect", connect)
    queue = OutboundQueue(tmp_path)
    first_id, first_waiter = enqueue(queue, 101, "first")
    _second_id, second_waiter = enqueue(queue, 102, "second")
    connection_type.rollbacks = 0
    setattr(connection_type, failure_attribute, True)
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(queue, RecordingAdapter(), executor, worker_count=2)

    scheduler.dispatch_once()

    assert scheduler.stopping
    assert isinstance(scheduler.failure, QueuePersistenceError)
    assert connection_type.rollbacks >= 1
    assert queue.path.exists()
    assert executor.submissions == []
    for waiter in (first_waiter, second_waiter):
        with pytest.raises(QueuePersistenceError):
            waiter.result()
    assert not queue.waiters
    scheduler.dispatch_once()
    assert executor.submissions == []
    assert first_id in {row.id for row in queue.heads()}
    queue.close()


def test_startup_wakes_recompute_deadline_without_dequeue_when_worker_permit_is_unavailable(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    row_id, waiter = enqueue(retained_queue, 131)
    adapter = RecordingAdapter(retry_at=50.25)
    scheduler = OutboundQueueScheduler(retained_queue, adapter, ControlledExecutor(), worker_count=1)
    monkeypatch.setattr(outbound.time, "monotonic", lambda: 50.0)

    scheduler.dispatch_once()  # Startup inspection.
    assert scheduler.next_deadline == 50.25
    assert [row.id for row in retained_queue.heads()] == [row_id]

    scheduler.wake_event.set()  # Spurious/enqueue/membership wake only triggers recomputation.
    scheduler.dispatch_once()
    assert scheduler.wake_event.is_set()
    assert scheduler.next_deadline == 50.25
    assert not waiter.done()

    assert scheduler._permits.acquire(blocking=False)
    adapter.retry_at = None
    scheduler.dispatch_once()
    assert [row.id for row in retained_queue.heads()] == [row_id]
    scheduler._permits.release()


def test_invalid_payload_is_terminally_discarded_without_worker_or_sender_acquisition(
    retained_queue: OutboundQueue,
) -> None:
    retained_queue.connection.execute(
        "INSERT INTO outbound_queue "
        "(priority, telegram_chat_id, operation, payload, slave_id, required_sender_bot_id, created_at) "
        "VALUES (0, 151, 'send_message', X'02', NULL, NULL, 0)"
    )
    retained_queue.connection.commit()
    row_id = retained_queue.heads()[0].id
    waiter = Future()
    retained_queue.waiters[row_id] = waiter
    executor = ControlledExecutor()
    adapter = RecordingAdapter()
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)

    scheduler.dispatch_once()

    with pytest.raises(InvalidQueuedPayloadError):
        waiter.result()
    assert retained_queue.heads() == []
    assert executor.submissions == []
    assert adapter.failures == []
    assert scheduler.in_flight == {}


def test_shutdown_final_snapshot_abandons_only_dequeued_work_and_keeps_later_row(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_id, first_waiter = enqueue(retained_queue, 173, "first")
    second_id, second_waiter = enqueue(retained_queue, 173, "second")
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)
    scheduler.dispatch_once()

    clock = {"value": 0.0}

    def monotonic() -> float:
        return clock["value"]

    def sleep(seconds: float) -> None:
        clock["value"] += seconds

    monkeypatch.setattr(outbound.time, "monotonic", monotonic)
    monkeypatch.setattr(outbound.time, "sleep", sleep)
    scheduler.stop_and_drain(timeout=5.0)

    assert clock["value"] >= 5.0
    assert scheduler.wake_event.is_set()
    with pytest.raises(SchedulerStoppedError):
        first_waiter.result()
    with pytest.raises(SchedulerStoppedError):
        second_waiter.result()
    assert not retained_queue.waiters
    assert first_id not in scheduler.in_flight
    assert 173 not in scheduler.in_flight_destinations
    assert [row.id for row in retained_queue.heads()] == [second_id]

    executor.submissions[0][2].set_result("late")
    scheduler.harvest_completed()
    assert not retained_queue.waiters


class InitializationFailureConnection(sqlite3.Connection):
    instances: list["InitializationFailureConnection"] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        type(self).instances.append(self)

    def execute(self, statement: str, parameters=()):
        if statement.startswith("CREATE TABLE"):
            raise sqlite3.OperationalError("injected schema failure")
        return super().execute(statement, parameters)


def test_initialization_failure_rolls_back_closes_and_retains_queue_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    InitializationFailureConnection.instances = []

    def connect(*args, **kwargs):
        kwargs["factory"] = InitializationFailureConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(outbound.sqlite3, "connect", connect)

    with pytest.raises(sqlite3.OperationalError, match="injected schema failure"):
        OutboundQueue(tmp_path)

    queue_path = tmp_path / OutboundQueue.filename
    assert queue_path.exists()
    assert len(InitializationFailureConnection.instances) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        InitializationFailureConnection.instances[0].execute("SELECT 1")
