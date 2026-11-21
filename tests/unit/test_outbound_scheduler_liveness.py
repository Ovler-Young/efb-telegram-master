"""A dispatch pass must never leave the outbound worker asleep with runnable rows.

The worker waits for a wake-up event or a retry deadline. A pass that leaves
queued rows undispatched while nothing is in flight has neither, so delivery
used to stop until the next message arrived or the process was restarted.
"""
import logging

from concurrent.futures import ThreadPoolExecutor

import pytest

from efb_telegram_master.outbound import (
    OutboundQueueScheduler,
    QueueRequest,
    SenderSelectionResult,
)
from tests.unit.test_outbound import Adapter, DurableAdapter, enqueue


def _scheduler(tmp_path, adapter, make_queue, *, workers=1):
    queue = make_queue(tmp_path)
    return queue, OutboundQueueScheduler(queue, adapter, ThreadPoolExecutor(workers), workers)


def test_pass_without_progress_reschedules_instead_of_waiting_for_an_event(tmp_path, caplog, make_queue):
    """No dispatch, no deadline and nothing in flight must still retry."""
    adapter = Adapter()
    adapter.select_sender = lambda row, now: SenderSelectionResult()
    queue, scheduler = _scheduler(tmp_path, adapter, make_queue=make_queue)
    enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "left behind"}))
    try:
        with caplog.at_level(logging.ERROR, logger="efb_telegram_master.outbound"):
            scheduler.dispatch_once()
        assert scheduler.next_deadline is not None
        assert adapter.calls == []
        assert "no retry deadline" in caplog.records[-1].message
    finally:
        queue.close()


def test_idle_queue_keeps_waiting_for_events(tmp_path, make_queue):
    """An idle queue must not turn the worker into a poller."""
    queue, scheduler = _scheduler(tmp_path, DurableAdapter(reconcile=True), make_queue=make_queue)
    try:
        scheduler.dispatch_once()
        assert scheduler.next_deadline is None
    finally:
        queue.close()


def test_permits_held_without_a_submission_are_reset(tmp_path, make_queue):
    """Permits no submitted call owns cannot be released by a completion."""
    adapter = Adapter()
    queue, scheduler = _scheduler(tmp_path, adapter, make_queue=make_queue)
    enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "resumes"}))
    try:
        assert scheduler.slots.reserve() is not None  # Lost with an aborted attempt.
        scheduler.dispatch_once()
        assert [call[1:] for call in adapter.calls] == [(7, "send_message")]
        assert scheduler.next_deadline is None
    finally:
        queue.close()


def test_link_to_a_predecessor_that_no_longer_exists_does_not_block(tmp_path, make_queue):
    """A discarded or failed predecessor must not strand its dependent forever."""
    queue, scheduler = _scheduler(tmp_path, Adapter(), make_queue=make_queue)
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


def test_waiting_predecessor_keeps_deferring_its_dependent(tmp_path, make_queue):
    """Ordering is preserved while the predecessor is itself still waiting."""
    queue, scheduler = _scheduler(tmp_path, Adapter(), make_queue=make_queue)
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


def test_dependency_cycle_is_broken_instead_of_stalling(tmp_path, caplog, make_queue):
    """Rows that only defer to each other have no order, so delivery must resume."""
    queue, scheduler = _scheduler(tmp_path, Adapter(), make_queue=make_queue)
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


def test_a_delivered_predecessor_still_awaits_reconciliation(tmp_path, make_queue):
    """A sent parent keeps its dependent until its MsgLog write commits."""
    queue, scheduler = _scheduler(tmp_path, Adapter(), make_queue=make_queue)
    parent, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "parent"}))
    child, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "child"}))
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE id=?", (parent, child))
        queue.connection.execute(
            "UPDATE outbound_queue SET delivery_state='sent_pending', completion_receipt=? WHERE id=?",
            (b"\x00", parent),
        )
    try:
        assert queue.heads(ready_only=True) == []
        # Not a cycle: the parent can resolve, so the dependency is worth waiting for.
        assert not queue.has_unresolvable_dependency()
    finally:
        queue.close()


def test_a_persistent_stall_is_reported_once_per_interval(tmp_path, monkeypatch, caplog, make_queue):
    """Wakes from new traffic must not turn a stall into a log flood."""
    clock = [1000.0]
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: clock[0])
    adapter = Adapter()
    adapter.select_sender = lambda row, now: SenderSelectionResult()
    queue, scheduler = _scheduler(tmp_path, adapter, make_queue=make_queue)
    enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "left behind"}))
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


def _enqueue_topic(queue, thread_id, text):
    def send_message(chat_id: int, text: str, message_thread_id: int = 0):
        return chat_id, thread_id, text
    return queue.enqueue_many([QueueRequest(
        "send_message", (), {"chat_id": -1001, "text": text, "message_thread_id": thread_id},
    )], lambda _operation: send_message)


def test_topics_of_one_chat_dispatch_in_parallel(tmp_path, make_queue):
    """Two topics of one group are separate ordering units."""
    queue = make_queue(tmp_path)
    _enqueue_topic(queue, 11, "a")
    _enqueue_topic(queue, 12, "b")
    _enqueue_topic(queue, 11, "c")
    adapter = Adapter(block=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=2)
        try:
            scheduler.dispatch_once()
            assert {
                (call.row.telegram_chat_id, call.row.thread_id)
                for call in scheduler.in_flight.values()
            } == {(-1001, 11), (-1001, 12)}
            assert scheduler.in_flight_destinations == {(-1001, 11), (-1001, 12)}
        finally:
            adapter.release.set()
            scheduler.stop_and_drain(timeout=2)
            queue.close()


def test_one_topic_keeps_serial_order(tmp_path, make_queue):
    """A second row of the same topic waits for the first."""
    queue = make_queue(tmp_path)
    first, _ = _enqueue_topic(queue, 11, "a")
    second, _ = _enqueue_topic(queue, 11, "b")
    adapter = Adapter(block=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=2)
        try:
            scheduler.dispatch_once()
            assert list(scheduler.in_flight) == [first]
            assert second not in scheduler.in_flight, "same-topic rows must not overlap"
        finally:
            adapter.release.set()
            scheduler.stop_and_drain(timeout=2)
            queue.close()
    assert second > first


def test_head_slots_go_one_per_depth_tier_shallowest_first(tmp_path, make_queue):
    """Every waiting destination gets a slot before a tier gets a second one."""
    queue = make_queue(tmp_path)
    try:
        def send_message(chat_id: int, text: str):
            return chat_id, text

        def queue_row(chat_id, text):
            return queue.enqueue_many(
                [QueueRequest("send_message", (), {"chat_id": chat_id, "text": text})],
                lambda _operation: send_message,
            )[0]

        for index in range(3):          # deepest queue, enqueued first
            queue_row(11, f"a{index}")
        short = queue_row(12, "b0")     # one waiting row
        queue_row(13, "c0")             # two waiting rows
        queue_row(13, "c1")
        equally_short = queue_row(14, "d0")

        heads = queue.heads(ready_only=True)
        order = [row.telegram_chat_id for row in heads]
        # One slot per tier (1, 2, 3 waiting rows) before depth 1 gets its second.
        assert order == [12, 13, 11, 14]
        assert [row.id for row in heads if row.telegram_chat_id == 12] == [short]
        assert [row.id for row in heads if row.telegram_chat_id == 14] == [equally_short]
        # The deep queue is offered a slot ahead of the second shallow one.
        assert order.index(11) < order.index(14)
    finally:
        queue.close()


def test_equal_depths_keep_the_plain_fifo_order(tmp_path, make_queue):
    """Equal queue depths fall back to the age of the head row."""
    queue = make_queue(tmp_path)
    try:
        chats = (-1001, -1002, -1003)
        rows = [enqueue(queue, QueueRequest("send_message", (), {"chat_id": chat, "text": "x"}))[0]
                for chat in chats]
        assert [row.id for row in queue.heads(ready_only=True)] == rows
    finally:
        queue.close()


def test_a_deep_queue_is_not_squeezed_out_by_shallow_ones(tmp_path, make_queue):
    """A pass spends one slot per depth tier, not all of them on the shallowest."""
    queue = make_queue(tmp_path)
    adapter = Adapter(block=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=2)
        try:
            for index in range(5):  # deepest destination, and the oldest rows
                enqueue(queue, QueueRequest("send_message", (), {"chat_id": -2000, "text": f"d{index}"}))
            enqueue(queue, QueueRequest("send_message", (), {"chat_id": -1001, "text": "a"}))
            enqueue(queue, QueueRequest("send_message", (), {"chat_id": -1002, "text": "b"}))
            scheduler.dispatch_once()
            assert {call.row.telegram_chat_id for call in scheduler.in_flight.values()} == {-2000, -1001}
        finally:
            adapter.release.set()
            scheduler.stop_and_drain(timeout=2)
            queue.close()


def test_a_completion_re_elects_only_its_own_destination(tmp_path, monkeypatch, make_queue):
    """A pass must not re-read every waiting row when one destination moved."""
    adapter = Adapter()
    queue = make_queue(tmp_path)
    scans = []
    original = queue.heads

    def counting_heads(**kwargs):
        scans.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(queue, "heads", counting_heads)
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        try:
            first, _ = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "one"}))
            second, _ = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "two"}))
            scheduler.dispatch_once()
            assert list(scheduler.in_flight) == [first]
            assert len(scans) == 1

            scheduler.in_flight[first].future.result(timeout=2)
            scheduler.harvest_completed()
            scheduler.dispatch_once()
            assert list(scheduler.in_flight) == [second]
            assert len(scans) == 1, "the completion re-elected its own destination only"
        finally:
            scheduler.stop_and_drain(timeout=2)
            queue.close()


def test_one_destination_head_matches_the_full_scan(tmp_path, make_queue):
    """The single-destination election must pick exactly what a full scan picks."""
    queue = make_queue(tmp_path)
    try:
        for index in range(3):
            enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": f"a{index}"}))
        enqueue(queue, QueueRequest("send_message", (), {"chat_id": 8, "text": "b"}))
        heads = queue.heads(ready_only=True)
        # Shallowest tier first; the depth each head reports comes from the scan.
        assert [(row.telegram_chat_id, row.waiting_rows) for row in heads] == [(8, 1), (7, 3)]
        for row in heads:
            single = queue.head_for(row.telegram_chat_id, row.thread_id, ready_only=True)
            assert single is not None and single.id == row.id
        # A newer priority-1 row wins that destination's election on its own.
        enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "urgent", "_send_mode": "blocking"}))
        urgent = queue.head_for(7, None, ready_only=True)
        assert urgent.priority == 1 and urgent.operation == "send_message"
        assert queue.head_for(8, None, ready_only=True).priority == 0
    finally:
        queue.close()


def _linked_in_a_cycle(queue, scheduler, adapter):
    first, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "first"}))
    second, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "second"}))
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE id=?", (second, first))
        queue.connection.execute("UPDATE outbound_queue SET predecessor_id=? WHERE id=?", (first, second))


def _one_row(select_sender=None, *, block=False, leak_slot=False, stop=False):
    def scenario(queue, scheduler, adapter):
        if select_sender is not None:
            adapter.select_sender = select_sender
        adapter.block = block
        enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "row"}))
        if leak_slot:
            assert scheduler.slots.reserve() is not None
        if stop:
            scheduler.stop_and_drain(timeout=0)
    return scenario


# Each way a pass can end: the queue that produces it, the deadline the worker
# then waits for (None: it waits for an enqueue or completion event only), the
# calls left in flight and whether the scheduler stopped.
PASS_SCENARIOS = {
    "idle": (lambda queue, scheduler, adapter: None, None, 0, False),
    "busy": (_one_row(block=True), None, 1, False),
    "deferred": (_one_row(lambda row, now: SenderSelectionResult(retry_at=now + 5)), 1005.0, 0, False),
    "stalled": (_one_row(lambda row, now: SenderSelectionResult()), 1030.0, 0, False),
    "cycle": (_linked_in_a_cycle, None, 1, False),
    "permit_leak": (_one_row(leak_slot=True), None, 1, False),
    "stopped": (_one_row(stop=True), None, 0, True),
}


@pytest.mark.parametrize("scenario_name", list(PASS_SCENARIOS))
def test_each_way_a_pass_ends_sets_the_wait_the_worker_uses(tmp_path, monkeypatch, scenario_name, make_queue):
    """Only a stall adds its own rescan deadline; the worker otherwise waits for an event."""
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: 1000.0)
    scenario, deadline, in_flight, stopped = PASS_SCENARIOS[scenario_name]
    adapter = Adapter()
    queue = make_queue(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        try:
            scenario(queue, scheduler, adapter)
            scheduler.dispatch_once()
            assert scheduler.next_deadline == deadline
            assert len(scheduler.in_flight) == in_flight
            assert scheduler.stopping is stopped
        finally:
            adapter.release.set()
            scheduler.stop_and_drain(timeout=2)
            queue.close()


def test_a_failing_sender_lookup_releases_its_slot_and_delivery_continues(tmp_path, monkeypatch, caplog, make_queue):
    """A sender lookup that raises defers its row; the worker's only slot serves the next one."""
    clock = [1000.0]
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: clock[0])
    adapter = Adapter()
    broken_chats = {7}
    select_sender = adapter.select_sender

    def failing_select_sender(row, now):
        if row.telegram_chat_id in broken_chats:
            raise RuntimeError("sender pool unavailable")
        return select_sender(row, now)

    adapter.select_sender = failing_select_sender
    queue = make_queue(tmp_path)
    failing, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "first"}))
    later, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 8, "text": "next"}))
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        try:
            with caplog.at_level(logging.ERROR, logger="efb_telegram_master.outbound"):
                scheduler.dispatch_once()
            assert list(scheduler.in_flight) == [later]
            assert f"Choosing a sender for queue row {failing} failed" in caplog.text
            scheduler.in_flight[later].future.result(timeout=2)
            scheduler.harvest_completed()
            assert scheduler.slots.free == 1

            broken_chats.clear()
            scheduler.dispatch_once()
            assert not scheduler.in_flight
            assert scheduler.next_deadline == 1000.0 + OutboundQueueScheduler.STALL_RESCAN_SECONDS
            clock[0] = scheduler.next_deadline
            scheduler.dispatch_once()
            assert list(scheduler.in_flight) == [failing]
            scheduler.in_flight[failing].future.result(timeout=2)
            assert [call[1] for call in adapter.calls] == [8, 7]
        finally:
            scheduler.stop_and_drain(timeout=2)
            queue.close()
