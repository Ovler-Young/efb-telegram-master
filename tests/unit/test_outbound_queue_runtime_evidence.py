"""Independent lifecycle evidence for the dequeue-only outbound queue."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future
from dataclasses import dataclass
import io
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prometheus_client import generate_latest
from telegram import InputMediaDocument
from telegram.error import ChatMigrated, NetworkError, RetryAfter, TelegramError

import efb_telegram_master.outbound as outbound
from efb_telegram_master.bot_manager import (
    QueuedChatMigrationRetry,
    QueuedDbLogContext,
    TelegramBotManager,
)
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.auxiliary_bot import AuxiliaryBot
from efb_telegram_master.outbound import (
    InvalidQueuedPayloadError,
    OutboundQueue,
    OutboundQueueScheduler,
    QueuePersistenceError,
    QueueRequest,
    RequiredSenderUnavailableError,
    SchedulerStoppedError,
    SenderSelection,
    SenderSelectionResult,
)
from efb_telegram_master.rate_limiter import SlidingWindowRateLimiter


def send_message(chat_id: int, text: str) -> tuple[int, str]:
    return chat_id, text


def send_document(chat_id: int, document: object) -> tuple[int, object]:
    return chat_id, document


def send_video(chat_id: int, video: object, **kwargs) -> tuple[int, object, dict]:
    return chat_id, video, kwargs


def send_media_group(chat_id: int, media: object) -> tuple[int, object]:
    return chat_id, media


def edit_message_media(media: object, chat_id: int, message_id: int) -> tuple[object, int, int]:
    return media, chat_id, message_id


def edit_message_text(chat_id: int, message_id: int, text: str) -> tuple[int, int, str]:
    return chat_id, message_id, text


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


@dataclass(frozen=True)
class CompletionDecision:
    kind: str
    retry_at: float | None = None
    retry_reason: str | None = None


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
    auxiliary._membership_generation = {}
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
    row = SimpleNamespace(telegram_chat_id=211, slave_id="slave-a", priority=0)
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
    failure_decision: CompletionDecision = CompletionDecision("terminal_failure")

    def __post_init__(self) -> None:
        self.failures: list[tuple[object, BaseException]] = []
        self.successes: list[tuple[object, object]] = []
        self.executed: list[int] = []
        self.selection = SenderSelection(object(), None)

    def select_sender(self, row, now: float) -> SenderSelectionResult:
        if self.terminal:
            return SenderSelectionResult(terminal_error_class="required_sender_unavailable")
        if self.retry_at is not None and now < self.retry_at:
            return SenderSelectionResult(retry_at=self.retry_at)
        return SenderSelectionResult(selection=self.selection)

    def acquire_sender_limits(self, _selection: SenderSelection, _chat_id: int) -> bool:
        return True

    def execute_queued_call(self, row, _args, _kwargs, _selection: SenderSelection) -> int:
        self.executed.append(row.id)
        return row.id

    def record_queued_failure(
        self, row, error: BaseException, _selection: SenderSelection
    ) -> CompletionDecision:
        self.failures.append((row, error))
        return self.failure_decision

    def record_queued_retry_after(
        self, row, error: RetryAfter, _selection: SenderSelection
    ) -> None:
        self.failures.append((row, error))

    def record_queued_success(
        self, row, result: object, _selection: SenderSelection
    ) -> CompletionDecision:
        self.successes.append((row, result))
        return CompletionDecision("success")


@pytest.fixture
def retained_queue(tmp_path: Path) -> OutboundQueue:
    queue = OutboundQueue(tmp_path)
    queue.close()
    restarted = OutboundQueue(tmp_path)
    yield restarted
    restarted.close()


def test_eventual_retry_after_retains_original_row_waiter_and_same_priority_fifo(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RetryAfter keeps its original durable row until a later success."""
    source = io.BufferedReader(io.BytesIO(b"retry media"))
    first_id, first_waiter = retained_queue.enqueue_many(
        [QueueRequest("send_document", (41, source), {})],
        lambda _operation: send_document,
    )
    source.close()
    second_id, second_waiter = enqueue(retained_queue, 41, "second")
    executor = ControlledExecutor()
    adapter = manager_adapter()
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)

    clock = {"now": 100.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["now"])
    first_row = next(row for row in retained_queue.heads() if row.id == first_id)
    first_snapshot = (first_row.id, first_row.priority, first_row.payload, first_row.created_at)
    second_snapshot = retained_queue.connection.execute(
        "SELECT id, priority, payload, created_at FROM outbound_queue WHERE id = ?", (second_id,)
    ).fetchone()
    scheduler.dispatch_once()
    assert [row.id for row in retained_queue.heads()] == [first_id]
    assert len(executor.submissions) == 1
    first_media = executor.submissions[0][1][1][1]
    assert first_media.tell() == 0
    assert first_media.read() == b"retry media"

    retry_after = RetryAfter(10)
    executor.submissions[0][2].set_exception(retry_after)
    scheduler.harvest_completed()
    assert scheduler.wake_event.is_set()
    assert not first_waiter.done()
    assert not first_waiter.cancelled()
    assert not second_waiter.done()
    rows_after_retry = retained_queue.connection.execute(
        "SELECT id, priority, payload, created_at FROM outbound_queue ORDER BY id"
    ).fetchall()
    assert rows_after_retry == [first_snapshot, second_snapshot]

    # The original row remains the same-priority destination head during cooldown.
    scheduler.dispatch_once()
    assert len(executor.submissions) == 1
    assert scheduler.next_deadline == 110.0
    assert [row.id for row in retained_queue.heads()] == [first_id]

    clock["now"] = 110.0
    scheduler.dispatch_once()
    assert len(executor.submissions) == 2
    assert executor.submissions[1][1][0].id == first_id
    retry_media = executor.submissions[1][1][1][1]
    assert retry_media is not first_media
    assert retry_media.tell() == 0
    assert retry_media.read() == b"retry media"
    assert [row.id for row in retained_queue.heads()] == [first_id]

    executor.submissions[1][2].set_result("sent")
    scheduler.harvest_completed()
    assert first_waiter.result() == "sent"
    assert [row.id for row in retained_queue.heads()] == [second_id]


def test_video_cover_retry_reconstructs_a_fresh_offset_zero_value(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = io.BufferedReader(io.BytesIO(b"retry cover"))
    row_id, waiter = retained_queue.enqueue_many(
        [QueueRequest("send_video", (41, b"video"), {"cover": source})],
        lambda _operation: send_video,
    )
    source.close()
    executor = ControlledExecutor()
    adapter = manager_adapter()
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)
    clock = {"now": 100.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["now"])

    scheduler.dispatch_once()
    first_cover = executor.submissions[0][1][2]["cover"]
    assert first_cover.tell() == 0
    assert first_cover.read() == b"retry cover"
    executor.submissions[0][2].set_exception(RetryAfter(10))
    scheduler.harvest_completed()

    clock["now"] = 115.0
    scheduler.dispatch_once()
    retry_cover = executor.submissions[1][1][2]["cover"]
    assert executor.submissions[1][1][0].id == row_id
    assert retry_cover is not first_cover
    assert retry_cover.tell() == 0
    assert retry_cover.read() == b"retry cover"

    executor.submissions[1][2].set_result("sent")
    scheduler.harvest_completed()
    assert waiter.result() == "sent"


def test_nested_local_media_reopen_and_retry_reconstruct_distinct_offset_zero_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_path = tmp_path / "nested-retry.bin"
    local_path.write_bytes(b"nested retry media")
    source = local_path.open("rb")
    source.seek(4)
    media = InputMediaDocument(local_path)
    queue = OutboundQueue(tmp_path)
    row_id, _waiter = queue.enqueue_many(
        [QueueRequest("send_media_group", (41, [media]), {})],
        lambda _operation: send_media_group,
    )
    assert source.tell() == 4
    source.close()
    moved_path = tmp_path / "nested-moved.bin"
    local_path.rename(moved_path)
    moved_path.unlink()
    queue.close()

    reopened = OutboundQueue(tmp_path)
    row = reopened.heads()[0]
    reopen_media = reopened.decode_payload(row.payload)[0][1][0].media
    assert row.id == row_id
    assert reopen_media.tell() == 0
    assert reopen_media.read() == b"nested retry media"
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(
        reopened, manager_adapter(), executor, worker_count=1
    )
    clock = {"now": 100.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["now"])

    scheduler.dispatch_once()
    first_media = executor.submissions[0][1][1][1][0].media
    assert first_media is not reopen_media
    assert first_media.tell() == 0
    assert first_media.read() == b"nested retry media"
    executor.submissions[0][2].set_exception(RetryAfter(10))
    scheduler.harvest_completed()

    clock["now"] = 115.0
    scheduler.dispatch_once()
    retry_media = executor.submissions[1][1][1][1][0].media
    assert retry_media is not first_media
    assert retry_media is not reopen_media
    assert retry_media.tell() == 0
    assert retry_media.read() == b"nested retry media"
    executor.submissions[1][2].set_result("sent")
    scheduler.harvest_completed()
    assert reopened.heads() == []
    reopened.close()


def test_blocking_retry_after_is_terminal(retained_queue: OutboundQueue) -> None:
    row_id, waiter = retained_queue.enqueue_many(
        [QueueRequest("send_message", (), {"chat_id": 67, "text": "blocking", "_send_mode": "blocking"})],
        lambda _operation: send_message,
    )
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, manager_adapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(10))
    scheduler.harvest_completed()

    with pytest.raises(RetryAfter):
        waiter.result()
    assert row_id not in {row.id for row in retained_queue.heads()}


def _enqueue_blocking_media_edit(
    queue: OutboundQueue, source: io.BufferedReader, required_sender_bot_id: str = "auxiliary"
) -> tuple[int, Future]:
    return queue.enqueue_many(
        [QueueRequest(
            "edit_message_media", (InputMediaDocument(source), 67, 4),
            {"_send_mode": "blocking", "_required_sender_bot_id": required_sender_bot_id},
        )],
        lambda _operation: edit_message_media,
    )


def _required_auxiliary(bot: object) -> SimpleNamespace:
    return SimpleNamespace(
        bot_id=10,
        bot=bot,
        disabled=False,
        check_membership_tri=lambda _chat_id: True,
        peek_delay=lambda _chat_id: 0.0,
        try_acquire_limits=lambda _chat_id: True,
    )


def _manager_with_required_auxiliary(auxiliary: SimpleNamespace) -> TelegramBotManager:
    manager = manager_adapter()
    manager._queued_db_log_contexts = {}
    manager._queued_db_log_context_lock = threading.Lock()
    manager.bot_pool = BotPool([auxiliary], manager)
    return manager


def test_blocking_media_edit_retries_at_telegram_deadline_with_rewound_payload(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    source = io.BufferedReader(io.BytesIO(b"edited media"))
    row_id, waiter = _enqueue_blocking_media_edit(retained_queue, source)
    source.close()
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    first_media = executor.submissions[0][1][1][0].media
    assert first_media.input_file_content == b"edited media"
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()

    assert not waiter.done()
    assert row_id in scheduler.blocking_media_retries
    assert scheduler.next_deadline == 221.0
    clock["monotonic"] = 221.0
    clock["wall"] = 1121.0
    scheduler.dispatch_once()
    retry_media = executor.submissions[1][1][1][0].media
    assert retry_media is not first_media
    assert retry_media.input_file_content == b"edited media"
    executor.submissions[1][2].set_result("edited")
    scheduler.harvest_completed()
    assert waiter.result() == "edited"


def test_blocking_media_retry_preserves_database_context_until_real_manager_success(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    manager = manager_adapter()
    manager._queued_db_log_contexts = {}
    manager._queued_db_log_context_lock = threading.Lock()
    manager._write_database_update = Mock()
    completed = Mock()
    row_id, waiter = retained_queue.enqueue_many(
        [QueueRequest(
            "edit_message_media", (InputMediaDocument(b"media"), 67, 4),
            {"_send_mode": "blocking", "_required_sender_bot_id": "__main__"},
        )],
        lambda _operation: edit_message_media,
    )
    manager._queued_db_log_contexts[row_id] = QueuedDbLogContext(Mock(), None, completed)
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, manager, executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()

    assert manager._queued_db_log_contexts.keys() == {row_id}
    completed.assert_not_called()
    assert manager._bot_chat_disabled_until == {(None, 67): 221.0}
    clock.update(monotonic=221.0, wall=1121.0)
    scheduler.dispatch_once()
    result = Mock()
    executor.submissions[1][2].set_result(result)
    scheduler.harvest_completed()

    assert waiter.result() is result
    assert manager._queued_db_log_contexts == {}
    manager._write_database_update.assert_called_once()
    completed.assert_not_called()


@pytest.mark.parametrize(
    ("terminal_path", "expected_error"),
    [
        ("deadline", RetryAfter),
        ("submit", RuntimeError),
        ("stop", SchedulerStoppedError),
    ],
)
def test_blocking_media_retry_terminal_paths_finish_real_manager_database_context(
    retained_queue: OutboundQueue,
    monkeypatch: pytest.MonkeyPatch,
    terminal_path: str,
    expected_error: type[BaseException],
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    manager = manager_adapter()
    manager._queued_db_log_contexts = {}
    manager._queued_db_log_context_lock = threading.Lock()
    manager._write_database_update = Mock()
    completed = Mock()
    row_id, waiter = _enqueue_blocking_media_edit(
        retained_queue, io.BufferedReader(io.BytesIO(b"media")), "__main__"
    )
    manager._queued_db_log_contexts[row_id] = QueuedDbLogContext(Mock(), None, completed)
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, manager, executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()

    if terminal_path == "deadline":
        clock.update(monotonic=400.0, wall=1300.0)
        scheduler.dispatch_once()
    elif terminal_path == "submit":
        executor.submit_error = RuntimeError("executor unavailable")
        clock.update(monotonic=221.0, wall=1121.0)
        scheduler.dispatch_once()
    else:
        scheduler.stop_and_drain(timeout=0.0)

    with pytest.raises(expected_error):
        waiter.result()
    scheduler.stop_and_drain(timeout=0.0)
    assert manager._queued_db_log_contexts == {}
    completed.assert_called_once_with()
    manager._write_database_update.assert_not_called()


def test_blocking_media_retry_caps_required_sender_wait_at_original_deadline(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    adapter = RecordingAdapter()
    _row_id, waiter = _enqueue_blocking_media_edit(retained_queue, io.BufferedReader(io.BytesIO(b"media")))
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()
    adapter.retry_at = 500.0
    clock.update(monotonic=221.0, wall=1121.0)
    scheduler.dispatch_once()

    assert scheduler.next_deadline == 400.0
    clock.update(monotonic=400.0, wall=1300.0)
    scheduler.dispatch_once()
    with pytest.raises(RetryAfter):
        waiter.result()
    assert len(executor.submissions) == 1


def test_blocking_media_retry_caps_limiter_wait_at_original_deadline(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    adapter = RecordingAdapter()
    _row_id, waiter = _enqueue_blocking_media_edit(retained_queue, io.BufferedReader(io.BytesIO(b"media")))
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()
    monkeypatch.setattr(adapter, "acquire_sender_limits", lambda _selection, _chat_id: False)
    clock.update(monotonic=221.0, wall=1299.9)
    scheduler.dispatch_once()

    assert scheduler.next_deadline == pytest.approx(221.1)
    clock.update(monotonic=221.1, wall=1300.0)
    scheduler.dispatch_once()
    with pytest.raises(RetryAfter):
        waiter.result()
    assert len(executor.submissions) == 1


def test_blocking_media_retry_keeps_required_auxiliary_sender(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    auxiliary = _required_auxiliary(object())
    manager = _manager_with_required_auxiliary(auxiliary)
    executor = ControlledExecutor()
    _row_id, waiter = _enqueue_blocking_media_edit(
        retained_queue, io.BufferedReader(io.BytesIO(b"media")), "10"
    )
    scheduler = OutboundQueueScheduler(retained_queue, manager, executor, worker_count=1)

    scheduler.dispatch_once()
    assert executor.submissions[0][1][3].sender is auxiliary.bot
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()
    clock.update(monotonic=221.0, wall=1121.0)
    scheduler.dispatch_once()

    assert executor.submissions[1][1][3].sender is auxiliary.bot
    executor.submissions[1][2].set_result(Mock())
    scheduler.harvest_completed()
    assert waiter.done()


@pytest.mark.parametrize(
    ("change", "expected_error"),
    [("disabled", RequiredSenderUnavailableError), ("replaced", RetryAfter)],
)
def test_blocking_media_retry_fails_when_required_auxiliary_changes(
    retained_queue: OutboundQueue,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    expected_error: type[BaseException],
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    auxiliary = _required_auxiliary(object())
    manager = _manager_with_required_auxiliary(auxiliary)
    completed = Mock()
    executor = ControlledExecutor()
    row_id, waiter = _enqueue_blocking_media_edit(
        retained_queue, io.BufferedReader(io.BytesIO(b"media")), "10"
    )
    manager._queued_db_log_contexts[row_id] = QueuedDbLogContext(Mock(), None, completed)
    scheduler = OutboundQueueScheduler(retained_queue, manager, executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()
    if change == "disabled":
        auxiliary.disabled = True
    else:
        auxiliary.bot = object()
    clock.update(monotonic=221.0, wall=1121.0)
    scheduler.dispatch_once()

    with pytest.raises(expected_error):
        waiter.result()
    assert len(executor.submissions) == 1
    assert manager._bot is not auxiliary.bot
    assert manager._queued_db_log_contexts == {}
    completed.assert_called_once_with()


def test_blocking_media_edit_retry_exceeding_original_deadline_is_terminal(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    metrics = Metrics()
    retained_queue.metrics = metrics
    row_id, waiter = _enqueue_blocking_media_edit(retained_queue, io.BufferedReader(io.BytesIO(b"media")))
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(301))
    scheduler.harvest_completed()

    with pytest.raises(RetryAfter):
        waiter.result()
    assert row_id not in scheduler.blocking_media_retries
    rendered = generate_latest(metrics.registry).decode()
    assert 'etm_outbound_completions_total{operation="edit_message_media",outcome="failure",priority="blocking",sender_kind="main"} 1.0' in rendered
    assert 'etm_outbound_queue_lifetime_seconds_count{operation="edit_message_media",outcome="failure",priority="blocking"} 1.0' in rendered
    assert 'etm_outbound_executor_attempt_duration_seconds_count{operation="edit_message_media",outcome="failure",priority="blocking"} 1.0' in rendered


def test_repeated_blocking_media_edit_retry_cannot_extend_original_deadline(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    _row_id, waiter = _enqueue_blocking_media_edit(retained_queue, io.BufferedReader(io.BytesIO(b"media")))
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()
    clock.update(monotonic=221.0, wall=1121.0)
    scheduler.dispatch_once()
    executor.submissions[1][2].set_exception(RetryAfter(180))
    scheduler.harvest_completed()

    with pytest.raises(RetryAfter):
        waiter.result()
    assert not scheduler.blocking_media_retries


def test_blocking_media_edit_retry_keeps_same_chat_fifo_barrier(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    row_id, waiter = _enqueue_blocking_media_edit(retained_queue, io.BufferedReader(io.BytesIO(b"media")))
    later_id, later_waiter = retained_queue.enqueue_many(
        [QueueRequest("send_message", (), {"chat_id": 67, "text": "later", "_send_mode": "blocking"})],
        lambda _operation: send_message,
    )
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()
    scheduler.dispatch_once()
    assert len(executor.submissions) == 1
    clock.update(monotonic=221.0, wall=1121.0)
    scheduler.dispatch_once()
    assert executor.submissions[1][1][0].id == row_id
    executor.submissions[1][2].set_result("edited")
    scheduler.harvest_completed()
    assert waiter.result() == "edited"
    scheduler.dispatch_once()
    assert executor.submissions[2][1][0].id == later_id
    executor.submissions[2][2].set_result("later")
    scheduler.harvest_completed()
    assert later_waiter.result() == "later"


def test_stop_and_drain_fails_delayed_blocking_media_edit_without_retrying(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"monotonic": 100.0, "wall": 1000.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["monotonic"])
    monkeypatch.setattr(outbound.time, "time", lambda: clock["wall"])
    _row_id, waiter = _enqueue_blocking_media_edit(retained_queue, io.BufferedReader(io.BytesIO(b"media")))
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()
    scheduler.stop_and_drain(timeout=0.0)
    clock.update(monotonic=221.0, wall=1121.0)
    scheduler.dispatch_once()

    with pytest.raises(SchedulerStoppedError):
        waiter.result()
    assert len(executor.submissions) == 1


def test_blocking_media_edit_non_retry_after_failure_is_terminal(retained_queue: OutboundQueue) -> None:
    _row_id, waiter = _enqueue_blocking_media_edit(retained_queue, io.BufferedReader(io.BytesIO(b"media")))
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(TelegramError("terminal"))
    scheduler.harvest_completed()

    with pytest.raises(TelegramError, match="terminal"):
        waiter.result()
    assert not scheduler.blocking_media_retries


def test_blocking_text_edit_retry_after_remains_terminal(retained_queue: OutboundQueue) -> None:
    row_id, waiter = retained_queue.enqueue_many(
        [QueueRequest(
            "edit_message_text", (), {
                "chat_id": 67, "message_id": 4, "text": "updated", "_send_mode": "blocking",
                "_required_sender_bot_id": "auxiliary",
            },
        )],
        lambda _operation: edit_message_text,
    )
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(121))
    scheduler.harvest_completed()

    with pytest.raises(RetryAfter):
        waiter.result()
    assert row_id not in scheduler.blocking_media_retries


def test_later_blocking_row_overtakes_retained_normal_row_after_cooldown(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    eventual_id, _eventual_waiter = enqueue(retained_queue, 68, "eventual")
    executor = ControlledExecutor()
    adapter = manager_adapter()
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)
    clock = {"now": 100.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["now"])

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(10))
    scheduler.harvest_completed()
    blocking_id, blocking_waiter = retained_queue.enqueue_many(
        [QueueRequest("send_message", (), {
            "chat_id": 68, "text": "blocking", "_send_mode": "blocking"
        })],
        lambda _operation: send_message,
    )

    scheduler.dispatch_once()
    assert len(executor.submissions) == 1
    clock["now"] = 115.0
    scheduler.dispatch_once()
    assert executor.submissions[1][1][0].id == blocking_id

    executor.submissions[1][2].set_result("blocking sent")
    scheduler.harvest_completed()
    assert blocking_waiter.result() == "blocking sent"
    scheduler.dispatch_once()
    assert executor.submissions[2][1][0].id == eventual_id


def test_terminal_eventual_failure_removes_original_row(retained_queue: OutboundQueue) -> None:
    row_id, waiter = enqueue(retained_queue, 69, "terminal")
    executor = ControlledExecutor()
    adapter = RecordingAdapter()
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)

    scheduler.dispatch_once()
    failure = TelegramError("terminal")
    executor.submissions[0][2].set_exception(failure)
    scheduler.harvest_completed()

    with pytest.raises(TelegramError, match="terminal"):
        waiter.result()
    assert row_id not in {row.id for row in retained_queue.heads()}


def test_submitted_future_completion_wakes_scheduler(retained_queue: OutboundQueue) -> None:
    _row_id, _waiter = enqueue(retained_queue, 43, "completion wake")
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.dispatch_once()
    scheduler.wake_event.clear()
    executor.submissions[0][2].set_result("sent")

    assert scheduler.wake_event.is_set()


def test_harvest_completed_signals_only_after_removing_a_completed_future(
    retained_queue: OutboundQueue,
) -> None:
    _row_id, _waiter = enqueue(retained_queue, 45, "harvest wake")
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(retained_queue, RecordingAdapter(), executor, worker_count=1)

    scheduler.wake_event.clear()
    scheduler.harvest_completed()
    assert not scheduler.wake_event.is_set()

    scheduler.dispatch_once()
    executor.submissions[0][2].set_result("sent")
    scheduler.wake_event.clear()
    scheduler.harvest_completed()

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
    assert 'etm_outbound_queue_dispatches_total{outcome="submitted"} 1.0' in rendered
    assert 'etm_outbound_queue_wait_seconds_count{operation="send_message",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_executor_attempt_duration_seconds_count{operation="send_message",outcome="success",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_queue_lifetime_seconds_count{operation="send_message",outcome="success",priority="normal"} 1.0' in rendered
    assert row_id not in scheduler.in_flight
    queue.close()


def test_scheduler_records_attempt_failure_and_only_terminal_success_after_retry(tmp_path: Path) -> None:
    metrics = Metrics()
    queue = OutboundQueue(tmp_path, metrics=metrics)
    _row_id, waiter = enqueue(queue, 49, "retry metrics")
    adapter = RecordingAdapter(failure_decision=CompletionDecision("retry_eventual", retry_at=10.0))
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(RetryAfter(1))
    scheduler.harvest_completed()

    assert not waiter.done()
    rendered = generate_latest(metrics.registry).decode()
    assert 'etm_outbound_completions_total{operation="send_message",outcome="failure",priority="normal",sender_kind="main"}' not in rendered
    assert 'etm_outbound_retries_total{operation="send_message",priority="normal",reason="rate_limit"} 1.0' in rendered
    assert 'etm_outbound_failures_total{operation="send_message",priority="normal",stage="execution"} 1.0' in rendered
    assert 'etm_outbound_executor_attempt_duration_seconds_count{operation="send_message",outcome="failure",priority="normal"} 1.0' in rendered

    scheduler.dispatch_once()
    executor.submissions[1][2].set_result("sent")
    scheduler.harvest_completed()

    assert waiter.result() == "sent"
    rendered = generate_latest(metrics.registry).decode()
    assert 'etm_outbound_completions_total{operation="send_message",outcome="success",priority="normal",sender_kind="main"} 1.0' in rendered
    assert 'etm_outbound_completions_total{operation="send_message",outcome="failure",priority="normal",sender_kind="main"}' not in rendered
    assert 'etm_outbound_queue_lifetime_seconds_count{operation="send_message",outcome="success",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_queue_lifetime_seconds_count{operation="send_message",outcome="failure",priority="normal"}' not in rendered
    assert 'etm_outbound_executor_attempt_duration_seconds_count{operation="send_message",outcome="success",priority="normal"} 1.0' in rendered
    queue.close()


def test_scheduler_records_transport_retry_reason(tmp_path: Path) -> None:
    metrics = Metrics()
    queue = OutboundQueue(tmp_path, metrics=metrics)
    enqueue(queue, 50, "transport retry")
    adapter = RecordingAdapter(
        failure_decision=CompletionDecision("retry_eventual", retry_at=10.0)
    )
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(NetworkError("connection lost"))
    scheduler.harvest_completed()

    rendered = generate_latest(metrics.registry).decode()
    assert (
        'etm_outbound_retries_total{operation="send_message",priority="normal",'
        'reason="transport"} 1.0' in rendered
    )
    assert (
        'etm_outbound_retries_total{operation="send_message",priority="normal",'
        'reason="membership"}' not in rendered
    )
    queue.close()


def test_transport_retry_deadline_blocks_only_its_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = OutboundQueue(tmp_path)
    first_id, _first_waiter = enqueue(queue, 50, "first")
    manager = manager_adapter()
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(queue, manager, executor, worker_count=2)
    clock = {"now": 100.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["now"])

    scheduler.dispatch_once()
    executor.submissions[0][2].set_exception(NetworkError("connection lost"))
    scheduler.harvest_completed()
    second_id, _second_waiter = enqueue(queue, 50, "second")
    other_id, _other_waiter = enqueue(queue, 51, "other")

    scheduler.dispatch_once()

    assert scheduler.next_deadline == 101.0
    assert [submission[1][0].id for submission in executor.submissions] == [first_id, other_id]

    clock["now"] = 101.0
    scheduler.dispatch_once()

    assert [submission[1][0].id for submission in executor.submissions] == [
        first_id,
        other_id,
        first_id,
    ]
    assert second_id not in [submission[1][0].id for submission in executor.submissions]
    queue.close()


def test_chat_migration_redispatches_retained_row_only_after_harvest(tmp_path: Path) -> None:
    metrics = Metrics()
    queue = OutboundQueue(tmp_path, metrics=metrics)
    first_id, _waiter = enqueue(queue, 61, "first")
    sender = Mock()
    sender.send_message.side_effect = [ChatMigrated(62), "sent"]
    manager = manager_adapter()
    manager._bot = sender
    manager._outbound_queue = queue
    manager.channel = SimpleNamespace(
        chat_binding=SimpleNamespace(chat_migration_by_id=Mock())
    )
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(queue, manager, executor, worker_count=2)

    scheduler.dispatch_once()
    assert len(executor.submissions) == 1
    function, arguments, first_future = executor.submissions[0]
    with pytest.raises(QueuedChatMigrationRetry) as caught:
        function(*arguments)

    scheduler.dispatch_once()
    assert len(executor.submissions) == 1
    first_future.set_exception(caught.value)
    scheduler.harvest_completed()
    assert (
        'etm_outbound_retries_total{operation="send_message",priority="normal",'
        'reason="migration"} 1.0'
        in generate_latest(metrics.registry).decode()
    )
    scheduler.dispatch_once()

    assert len(executor.submissions) == 2
    retried_row = executor.submissions[1][1][0]
    assert retried_row.id == first_id
    assert retried_row.telegram_chat_id == 62
    assert queue.decode_payload(retried_row.payload)[1]["chat_id"] == 62
    manager.channel.chat_binding.chat_migration_by_id.assert_called_once_with(61, 62)
    queue.close()


def test_chat_migration_binding_failure_retains_original_row(tmp_path: Path) -> None:
    queue = OutboundQueue(tmp_path)
    row_id, _waiter = enqueue(queue, 61, "first")
    row = queue.heads()[0]
    sender = Mock()
    sender.send_message.side_effect = ChatMigrated(62)
    manager = manager_adapter()
    manager._outbound_queue = queue
    manager.channel = SimpleNamespace(
        chat_binding=SimpleNamespace(
            chat_migration_by_id=Mock(side_effect=RuntimeError("database unavailable"))
        )
    )
    selection = SenderSelection(sender=sender, sender_bot_id=None)

    with pytest.raises(QueuedChatMigrationRetry) as caught:
        manager.execute_queued_call(row, *queue.decode_payload(row.payload), selection)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("efb_telegram_master.bot_manager.time.monotonic", lambda: 50.0)
        decision = manager.record_queued_failure(row, caught.value, selection)
    assert decision.kind.name == "RETRY_EVENTUAL"
    assert decision.retry_reason == "migration"
    assert decision.retry_at == 51.0
    retained = queue.heads()[0]
    assert retained.id == row_id
    assert retained.telegram_chat_id == 61
    assert queue.decode_payload(retained.payload)[1]["chat_id"] == 61
    queue.close()


def test_chat_migration_binding_failure_observes_retry_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = OutboundQueue(tmp_path)
    row_id, _waiter = enqueue(queue, 61, "first")
    sender = Mock()
    sender.send_message.side_effect = ChatMigrated(62)
    manager = manager_adapter()
    manager._bot = sender
    manager._outbound_queue = queue
    manager.channel = SimpleNamespace(
        chat_binding=SimpleNamespace(
            chat_migration_by_id=Mock(side_effect=RuntimeError("database unavailable"))
        )
    )
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(queue, manager, executor, worker_count=1)
    clock = {"now": 50.0}
    monkeypatch.setattr(outbound.time, "monotonic", lambda: clock["now"])

    scheduler.dispatch_once()
    function, arguments, future = executor.submissions[0]
    with pytest.raises(QueuedChatMigrationRetry) as caught:
        function(*arguments)
    future.set_exception(caught.value)
    scheduler.harvest_completed()

    scheduler.dispatch_once()
    assert len(executor.submissions) == 1
    assert scheduler.next_deadline == 51.0

    clock["now"] = 51.0
    scheduler.dispatch_once()
    assert len(executor.submissions) == 2
    assert executor.submissions[1][1][0].id == row_id
    queue.close()


def test_chat_migration_retarget_failure_stops_scheduler_and_retains_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = OutboundQueue(tmp_path)
    row_id, waiter = enqueue(queue, 61, "first")
    sender = Mock()
    sender.send_message.side_effect = ChatMigrated(62)
    manager = manager_adapter()
    manager._bot = sender
    manager._outbound_queue = queue
    manager.channel = SimpleNamespace(
        chat_binding=SimpleNamespace(chat_migration_by_id=Mock())
    )
    persistence_error = QueuePersistenceError("injected retarget failure")
    monkeypatch.setattr(queue, "retarget", Mock(side_effect=persistence_error))
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(queue, manager, executor, worker_count=1)

    scheduler.dispatch_once()
    function, arguments, future = executor.submissions[0]
    with pytest.raises(QueuePersistenceError) as caught:
        function(*arguments)
    future.set_exception(caught.value)
    scheduler.harvest_completed()

    assert scheduler.stopping
    assert scheduler.failure is persistence_error
    with pytest.raises(QueuePersistenceError):
        waiter.result()
    retained = queue.heads()[0]
    assert retained.id == row_id
    assert retained.telegram_chat_id == 61
    assert queue.decode_payload(retained.payload)[1]["chat_id"] == 61
    manager.channel.chat_binding.chat_migration_by_id.assert_called_once_with(61, 62)
    queue.close()


def test_retarget_updates_only_current_row_and_ignores_corrupt_sibling(tmp_path: Path) -> None:
    queue = OutboundQueue(tmp_path)
    first_id, _waiter = enqueue(queue, 61, "first")
    second_id, _second_waiter = enqueue(queue, 61, "second")
    queue.connection.execute(
        "UPDATE outbound_queue SET payload = X'02' WHERE id = ?", (second_id,)
    )
    queue.connection.commit()

    queue.retarget(first_id, 62, (), {"chat_id": 62, "text": "first"})

    stored_rows = queue.connection.execute(
        "SELECT id, telegram_chat_id, payload FROM outbound_queue ORDER BY id"
    ).fetchall()
    assert [(stored_rows[0][0], stored_rows[0][1])] == [(first_id, 62)]
    assert queue.decode_payload(stored_rows[0][2])[1]["chat_id"] == 62
    assert (stored_rows[1][0], stored_rows[1][1], stored_rows[1][2]) == (
        second_id,
        61,
        b"\x02",
    )
    queue.close()


def test_manager_registers_runtime_snapshot_collectors_with_configured_destination_cap(tmp_path: Path) -> None:
    queue = OutboundQueue(tmp_path)
    enqueue(queue, 61, "first")
    enqueue(queue, 61, "second")
    enqueue(queue, 62, "third")
    metrics = Metrics()
    manager = object.__new__(TelegramBotManager)
    manager._metrics = metrics
    manager._outbound_queue = queue
    manager._outbound_scheduler = SimpleNamespace(in_flight_count=lambda: 3)
    manager._send_worker_thread = SimpleNamespace(is_alive=lambda: True)
    manager._bot_chat_disabled_until = {(None, 61): outbound.time.monotonic() + 1.0}
    manager._rate_limiter = SlidingWindowRateLimiter()
    manager.bot_pool = None

    manager._register_runtime_metric_collectors(top_n=1)

    rendered = generate_latest(metrics.registry).decode()
    assert 'etm_outbound_destination_queue_depth{destination="rank_1"} 2.0' in rendered
    assert 'etm_outbound_destination_queue_depth{destination="rank_2"}' not in rendered
    assert "etm_outbound_worker_healthy 1.0" in rendered
    assert "etm_outbound_worker_in_flight 3.0" in rendered
    assert 'etm_outbound_cooldown_seconds{sender_kind="main"}' in rendered
    assert 'etm_auxiliary_bots{state="enabled"} 0.0' in rendered
    assert 'etm_auxiliary_membership_cache_entries{state="member"} 0.0' in rendered
    assert 'etm_rate_limit_occupancy{scope="global"} 0.0' in rendered
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


def test_executor_submit_failure_retains_eventual_row_and_waiter(
    retained_queue: OutboundQueue,
) -> None:
    first_id, first_waiter = enqueue(retained_queue, 89, "first")
    adapter = RecordingAdapter()
    scheduler = OutboundQueueScheduler(
        retained_queue, adapter, ControlledExecutor(RuntimeError("executor unavailable")), worker_count=1
    )

    scheduler.dispatch_once()

    assert not first_waiter.done()
    assert first_id in {row.id for row in retained_queue.heads()}
    assert 89 not in scheduler.in_flight_destinations
    assert scheduler.in_flight == {}

    executor = ControlledExecutor()
    scheduler.executor = executor
    scheduler.dispatch_once()
    assert first_id in scheduler.in_flight
    executor.submissions[0][2].set_result("sent")
    scheduler.harvest_completed()
    assert first_waiter.result() == "sent"


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
    executor.submissions[0][2].set_result("sent")
    scheduler.harvest_completed()

    assert scheduler.stopping
    assert isinstance(scheduler.failure, QueuePersistenceError)
    assert connection_type.rollbacks >= 1
    assert queue.path.exists()
    assert len(executor.submissions) == 2
    for waiter in (first_waiter, second_waiter):
        with pytest.raises(QueuePersistenceError):
            waiter.result()
    assert not queue.waiters
    scheduler.dispatch_once()
    assert len(executor.submissions) == 2
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


def test_shutdown_final_snapshot_keeps_retained_eventual_rows(
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
    assert [row[0] for row in retained_queue.connection.execute(
        "SELECT id FROM outbound_queue ORDER BY id"
    ).fetchall()] == [first_id, second_id]

    executor.submissions[0][2].set_result("late")
    scheduler.harvest_completed()
    assert not retained_queue.waiters


def test_shutdown_resolves_retained_eventual_retry_after_waiter(
    retained_queue: OutboundQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    row_id, waiter = enqueue(retained_queue, 174, "retry during shutdown")
    executor = ControlledExecutor()
    adapter = RecordingAdapter(failure_decision=CompletionDecision("retry_eventual", retry_at=20.0))
    scheduler = OutboundQueueScheduler(retained_queue, adapter, executor, worker_count=1)
    scheduler.dispatch_once()

    clock = {"value": 0.0}

    def monotonic() -> float:
        return clock["value"]

    def sleep(_seconds: float) -> None:
        executor.submissions[0][2].set_exception(RetryAfter(10))
        clock["value"] = 1.0

    monkeypatch.setattr(outbound.time, "monotonic", monotonic)
    monkeypatch.setattr(outbound.time, "sleep", sleep)

    scheduler.stop_and_drain(timeout=5.0)

    assert waiter.done()
    with pytest.raises(SchedulerStoppedError):
        waiter.result()
    assert [row.id for row in retained_queue.heads()] == [row_id]
    assert row_id not in retained_queue.waiters
    assert scheduler.in_flight == {}
    assert 174 not in scheduler.in_flight_destinations
    assert len(executor.submissions) == 1


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
