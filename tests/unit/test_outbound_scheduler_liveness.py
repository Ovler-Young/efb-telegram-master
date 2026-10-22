"""A dispatch pass must never leave the outbound worker asleep with runnable rows.

The worker waits for a wake-up event or a retry deadline. A pass that leaves
queued rows undispatched while nothing is in flight has neither, so delivery
used to stop until the next message arrived or the process was restarted.
"""
import logging

from concurrent.futures import ThreadPoolExecutor

import pytest

from efb_telegram_master.outbound import (
    OutboundQueue,
    OutboundQueueScheduler,
    QueueRequest,
    SenderSelectionResult,
)
from tests.unit.test_outbound import Adapter, DurableAdapter, enqueue


def _scheduler(tmp_path, adapter, *, workers=1):
    queue = OutboundQueue(tmp_path)
    return queue, OutboundQueueScheduler(queue, adapter, ThreadPoolExecutor(workers), workers)


def test_pass_without_progress_reschedules_instead_of_waiting_for_an_event(tmp_path, caplog):
    """No dispatch, no deadline and nothing in flight must still retry."""
    adapter = Adapter()
    adapter.select_sender = lambda row, now: SenderSelectionResult()
    queue, scheduler = _scheduler(tmp_path, adapter)
    enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "left behind"}))
    try:
        with caplog.at_level(logging.ERROR, logger="efb_telegram_master.outbound"):
            scheduler.dispatch_once()
        assert scheduler.next_deadline is not None
        assert adapter.calls == []
        assert "no retry deadline" in caplog.records[-1].message
    finally:
        queue.close()


def test_idle_queue_keeps_waiting_for_events(tmp_path):
    """An idle queue must not turn the worker into a poller."""
    queue, scheduler = _scheduler(tmp_path, DurableAdapter(reconcile=True))
    try:
        scheduler.dispatch_once()
        assert scheduler.next_deadline is None
    finally:
        queue.close()


def test_permits_held_without_a_submission_are_reset(tmp_path):
    """Permits no submitted call owns cannot be released by a completion."""
    adapter = Adapter()
    queue, scheduler = _scheduler(tmp_path, adapter)
    enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "resumes"}))
    try:
        assert scheduler._permits.acquire()  # Lost with an aborted attempt.
        scheduler.dispatch_once()
        assert [call[1:] for call in adapter.calls] == [(7, "send_message")]
        assert scheduler.next_deadline is None
    finally:
        queue.close()


@pytest.mark.parametrize("pending_chats", [1, 3])
def test_stall_recovery_keeps_every_retained_row(tmp_path, pending_chats):
    """Recovery reschedules delivery without dropping or reordering rows."""
    adapter = Adapter()
    adapter.select_sender = lambda row, now: SenderSelectionResult()
    queue, scheduler = _scheduler(tmp_path, adapter, workers=2)
    for chat_id in range(pending_chats):
        enqueue(queue, QueueRequest("send_message", (), {"chat_id": chat_id, "text": "kept"}))
    try:
        scheduler.dispatch_once()
        assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == pending_chats
        assert scheduler.next_deadline is not None
    finally:
        queue.close()


def test_link_to_a_predecessor_that_no_longer_exists_does_not_block(tmp_path):
    """A discarded or failed predecessor must not strand its dependent forever."""
    queue, scheduler = _scheduler(tmp_path, Adapter())
    row_id, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "kept"}))
    with queue.connection:
        queue.connection.execute(
            "UPDATE outbound_queue SET predecessor_id=? WHERE id=?", (row_id + 900, row_id)
        )
    try:
        assert [row.id for row in queue.heads(ready_only=True)] == [row_id]
        scheduler.dispatch_once()
        assert len(scheduler.in_flight) == 1
    finally:
        queue.close()


def test_waiting_predecessor_keeps_deferring_its_dependent(tmp_path):
    """Ordering is preserved while the predecessor is itself still waiting."""
    queue, scheduler = _scheduler(tmp_path, Adapter())
    parent, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "parent"}))
    child, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "child"}))
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE id=?", (parent, child))
    try:
        assert [row.id for row in queue.heads(ready_only=True)] == [parent]
        # A permanent hold keeps the row but must not keep its dependents waiting.
        queue.hold_uncertain_delivery(parent, RuntimeError("unconfirmed"))
        assert [row.id for row in queue.heads(ready_only=True)] == [child]
        assert not queue.has_unresolvable_dependency()
    finally:
        queue.close()


def test_dependency_cycle_is_broken_instead_of_stalling(tmp_path, caplog):
    """Rows that only defer to each other have no order, so delivery must resume."""
    queue, scheduler = _scheduler(tmp_path, Adapter())
    first, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "first"}))
    second, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "second"}))
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE id=?", (second, first))
        queue.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE id=?", (first, second))
    try:
        assert queue.heads(ready_only=True) == []
        assert queue.has_unresolvable_dependency()
        with caplog.at_level(logging.ERROR, logger="efb_telegram_master.outbound"):
            scheduler.dispatch_once()
        assert len(scheduler.in_flight) == 1
        assert "never resolve" in caplog.records[-1].message
        assert scheduler.next_deadline is None
    finally:
        queue.close()


def test_a_delivered_predecessor_still_awaits_reconciliation(tmp_path):
    """A sent parent keeps its dependent until its MsgLog write commits."""
    queue, scheduler = _scheduler(tmp_path, Adapter())
    parent, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "parent"}))
    child, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "child"}))
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE id=?", (parent, child))
        queue.connection.execute(
            "UPDATE outbound_queue SET delivery_state='sent_pending', completion_receipt=X'00' WHERE id=?",
            (parent,),
        )
    try:
        assert queue.heads(ready_only=True) == []
        # Not a cycle: the parent can resolve, so the dependency is worth waiting for.
        assert not queue.has_unresolvable_dependency()
    finally:
        queue.close()


def test_a_persistent_stall_is_reported_once_per_interval(tmp_path, monkeypatch, caplog):
    """Wakes from new traffic must not turn a stall into a log flood."""
    clock = [1000.0]
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: clock[0])
    queue, scheduler = _scheduler(tmp_path, Adapter())
    parent, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "parent"}))
    child, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "child"}))
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE id=?", (parent, child))
        queue.connection.execute(
            "UPDATE outbound_queue SET delivery_state='sent_pending', completion_receipt=X'00' WHERE id=?",
            (parent,),
        )
    try:
        with caplog.at_level(logging.ERROR, logger="efb_telegram_master.outbound"):
            scheduler.dispatch_once()
            clock[0] += 5
            scheduler.dispatch_once()
            clock[0] += OutboundQueueScheduler.STALL_REPORT_INTERVAL + 1
            scheduler.dispatch_once()
        assert len([record for record in caplog.records if "dispatchable head(s)" in record.message]) == 2
        assert scheduler.next_deadline is not None
    finally:
        queue.close()
