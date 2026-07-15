from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import threading

import pytest

from efb_telegram_master.outbound import (
    ExecutorSubmitError,
    InvalidQueuedPayloadError,
    OutboundQueue,
    OutboundQueueScheduler,
    QueueEnqueueError,
    QueueRequest,
    SchedulerStoppedError,
    SenderSelection,
    SenderSelectionResult,
)


def send_message(chat_id, text):
    return (chat_id, text)


def edit_message_text(chat_id, message_id, text):
    return (chat_id, message_id, text)


def operation(name):
    return {"send_message": send_message, "edit_message_text": edit_message_text}[name]


def enqueue(queue, *requests):
    return queue.enqueue_many(requests, operation)


def test_queue_schema_wal_and_restart_retention(tmp_path):
    queue = OutboundQueue(tmp_path)
    row_id, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 12, "text": "first"}))
    assert queue.path == tmp_path / "outbound-queue.sqlite3"
    assert queue.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert queue.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert queue.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'outbound_queue_destination_priority_id'"
    ).fetchone()[0]
    queue.close()

    restarted = OutboundQueue(tmp_path)
    assert [row.id for row in restarted.heads()] == [row_id]
    restarted.delete(row_id)
    restarted.close()
    assert OutboundQueue(tmp_path).heads() == []


def test_payload_version_round_trip_and_invalid_shapes(tmp_path):
    queue = OutboundQueue(tmp_path)
    payload = queue.encode_payload((1,), {"chat_id": 2, "value": {"x": 1}})
    assert queue.decode_payload(payload) == ((1,), {"chat_id": 2, "value": {"x": 1}})
    for invalid in (b"\x02value", b"\x01not-a-pickle", b"\x01\x80\x05K\x01."):
        with pytest.raises(InvalidQueuedPayloadError):
            queue.decode_payload(invalid)
    with pytest.raises(QueueEnqueueError):
        queue.encode_payload((), {"value": threading.Lock()})


@pytest.mark.parametrize(
    ("args", "kwargs", "valid"),
    [
        ((7, "text"), {}, True),
        ((), {"chat_id": 7, "text": "text"}, True),
        ((), {"text": "text"}, False),
        ((7,), {"chat_id": 7, "text": "text"}, False),
        ((), {"chat_id": True, "text": "text"}, False),
        ((), {"chat_id": 7.0, "text": "text"}, False),
        ((), {"chat_id": "7", "text": "text"}, False),
    ],
)
def test_chat_id_binds_after_scheduler_metadata_is_removed(tmp_path, args, kwargs, valid):
    queue = OutboundQueue(tmp_path)
    request = QueueRequest("send_message", args, {**kwargs, "_send_mode": "eventual"})
    if valid:
        enqueue(queue, request)
        assert queue.heads()[0].telegram_chat_id == 7
    else:
        with pytest.raises(QueueEnqueueError):
            enqueue(queue, request)


def test_internal_keys_are_validated_and_absent_from_payload(tmp_path):
    queue = OutboundQueue(tmp_path)
    row_id, _waiter = enqueue(queue, QueueRequest("send_message", (), {
        "chat_id": 7, "text": "text", "_send_mode": "blocking", "_slave_id": "slave",
    }))
    row = queue.heads()[0]
    assert row.id == row_id
    assert row.priority == 1
    assert row.slave_id == "slave"
    assert queue.decode_payload(row.payload)[1] == {"chat_id": 7, "text": "text"}
    for kwargs in (
        {"chat_id": 7, "text": "x", "_send_mode": "later"},
        {"chat_id": 7, "text": "x", "_slave_id": ""},
        {"chat_id": 7, "text": "x", "_required_sender_bot_id": "bot"},
    ):
        with pytest.raises(QueueEnqueueError):
            enqueue(queue, QueueRequest("send_message", (), kwargs))
    with pytest.raises(QueueEnqueueError):
        enqueue(queue, QueueRequest("edit_message_text", (), {
            "chat_id": 7, "message_id": 1, "text": "x"
        }))


def test_send_message_main_bot_sentinel_persists_but_other_required_sender_is_rejected(tmp_path):
    queue = OutboundQueue(tmp_path)

    row_id, _waiter = enqueue(queue, QueueRequest("send_message", (), {
        "chat_id": 7,
        "text": "text",
        "_required_sender_bot_id": "__main__",
    }))

    row = queue.heads()[0]
    assert row.id == row_id
    assert row.required_sender_bot_id == "__main__"
    assert queue.decode_payload(row.payload)[1] == {"chat_id": 7, "text": "text"}
    with pytest.raises(QueueEnqueueError):
        enqueue(queue, QueueRequest("send_message", (), {
            "chat_id": 7,
            "text": "text",
            "_required_sender_bot_id": "auxiliary-1",
        }))


def test_multi_call_is_atomic_ordered_and_returns_only_first_waiter(tmp_path):
    queue = OutboundQueue(tmp_path)
    first, waiter = enqueue(queue,
        QueueRequest("send_message", (), {"chat_id": 7, "text": "one"}),
        QueueRequest("send_message", (), {"chat_id": 7, "text": "two"}),
    )
    assert [row.id for row in queue.heads()] == [first]
    assert list(queue.waiters) == [first]
    with pytest.raises(QueueEnqueueError):
        enqueue(queue,
            QueueRequest("send_message", (), {"chat_id": 7, "text": "one"}),
            QueueRequest("send_message", (), {"chat_id": 8, "text": "two"}),
        )
    assert len(queue.connection.execute("SELECT * FROM outbound_queue").fetchall()) == 2
    assert not waiter.done()


class Adapter:
    def __init__(self, block=False):
        self.block = block
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def select_sender(self, row, now):
        return SenderSelectionResult(selection=SenderSelection(object(), None))

    def acquire_sender_limits(self, selection, telegram_chat_id):
        return True

    def execute_queued_call(self, row, args, kwargs, selection):
        self.calls.append((row.id, row.telegram_chat_id, row.operation))
        self.started.set()
        if self.block:
            self.release.wait(2)
        return row.id

    def record_queued_failure(self, row, error, selection):
        raise AssertionError(f"unexpected failure: {error}")

    def record_queued_success(self, row, result, selection):
        pass


def test_scheduler_prioritizes_blocking_and_never_submits_two_destination_rows(tmp_path):
    queue = OutboundQueue(tmp_path)
    normal_id, _normal = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "normal"}))
    blocking_id, _blocking = enqueue(queue, QueueRequest("send_message", (), {
        "chat_id": 7, "text": "blocking", "_send_mode": "blocking"
    }))
    adapter = Adapter(block=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=2)
        scheduler.dispatch_once()
        assert adapter.started.wait(1)
        assert adapter.calls == [(blocking_id, 7, "send_message")]
        scheduler.dispatch_once()
        assert adapter.calls == [(blocking_id, 7, "send_message")]
        adapter.release.set()
        scheduler.in_flight[blocking_id].future.result(timeout=1)
        scheduler.harvest_completed()
        scheduler.dispatch_once()
        scheduler.in_flight[normal_id].future.result(timeout=1)
        assert adapter.calls[-1][0] == normal_id


def test_delete_failure_stops_scheduler_and_fails_waiters(tmp_path, monkeypatch):
    queue = OutboundQueue(tmp_path)
    _row_id, waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "text"}))
    adapter = Adapter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        monkeypatch.setattr(queue, "delete", lambda _row_id: (_ for _ in ()).throw(sqlite3.OperationalError()))
        scheduler.dispatch_once()
        assert scheduler.stopping
        with pytest.raises(Exception, match="deletion failed"):
            waiter.result()
        assert adapter.calls == []


def test_shutdown_keeps_queued_row_and_fails_abandoned_waiter(tmp_path):
    queue = OutboundQueue(tmp_path)
    row_id, waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "text"}))
    adapter = Adapter(block=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        scheduler.dispatch_once()
        assert adapter.started.wait(1)
        scheduler.stop_and_drain(timeout=0)
        with pytest.raises(SchedulerStoppedError):
            waiter.result()
        assert row_id not in queue.waiters
        adapter.release.set()
