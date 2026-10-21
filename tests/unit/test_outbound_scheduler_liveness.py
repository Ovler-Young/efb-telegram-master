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
