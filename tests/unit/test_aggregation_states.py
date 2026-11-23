"""Aggregation queue rows follow one transition table, across restarts too."""

import copy
import datetime
import logging
from types import SimpleNamespace

import pytest
from telegram import Chat, Message, User

from efb_telegram_master.aggregate import make_source_member
from efb_telegram_master.aggregation_states import (
    ContextState,
    context_state,
    needs_delivered_predecessor,
)
from efb_telegram_master.db import MsgLog
from efb_telegram_master.outbound import (
    IllegalAggregationTransitionError,
    QueueRequest,
    SenderSelectionResult,
)
from efb_telegram_master.queued_log import decode_aggregation, encode_aggregation
from tests.unit import test_live_aggregate_runtime
from tests.unit.test_live_aggregate import source
from tests.unit.test_live_aggregate_runtime import append, complete
from tests.unit.test_live_aggregate_source import restart

runtime = test_live_aggregate_runtime.runtime


def states(manager):
    return {context_state(decode_aggregation(row.log_context)) for row in manager._outbound_queue.aggregation_rows()}


def stored(manager, row_id):
    return manager._outbound_queue.connection.execute(
        "SELECT * FROM outbound_queue WHERE id=?", (row_id,)).fetchone()


def state_and_hold(manager, row_id):
    return manager._outbound_queue.connection.execute(
        "SELECT delivery_state, delivery_hold FROM outbound_queue WHERE id=?", (row_id,)).fetchone()


def member_of(uid, text, at):
    return make_source_member(source(uid, text), received_time=datetime.datetime.fromtimestamp(at))


def test_rows_of_every_kind_recover_and_reconcile(runtime, make_queue):
    manager = runtime
    queue = manager._outbound_queue
    scheduler = manager._outbound_scheduler
    key, first = append(manager, "one", 1)
    complete(manager)
    # A container whose receipt is saved but not yet written to MsgLog, in the
    # shape stored before containers carried their id and routing flag.
    append(manager, "topic", 2, topic=7)
    scheduler.dispatch_once()
    (sent_id, submitted), = scheduler.in_flight.items()
    queue.record_telegram_completion(
        sent_id, manager.encode_queued_completion_receipt(submitted.future.result(timeout=2), submitted.selection))
    older = decode_aggregation(stored(manager, sent_id)[8])
    del older["container_id"], older["routing_hint"]
    queue.connection.execute("UPDATE outbound_queue SET log_context=? WHERE id=?", (encode_aggregation(older), sent_id))
    queue.connection.commit()
    # A confirmed standalone message whose source is then removed: a frozen removal edit.
    solo = source("solo", "solo body")
    solo_receipt = manager.transport.send_message(-100, "Alice:\nsolo body", parse_mode=None)
    manager.channel.db.finalize_source_message(solo, solo_receipt, make_source_member(solo, display_prefix="Alice"), None)
    assert manager.live_aggregation.remove_source_member(key, (key[0], "solo"))
    revised = copy.deepcopy(first)
    revised["source_revision"] = 2
    manager.live_aggregation.queue_container_update("-100.1", [revised], key=key)
    append(manager, "two", 3)
    with manager.live_aggregation.independent(key):
        manager._enqueue_requests([QueueRequest("send_message", (), {"chat_id": -100, "text": "media"})])
    with manager.live_aggregation.source_delivery(key, member_of("three", "three body", 4)):
        manager._enqueue_requests([QueueRequest("send_message", (), {"chat_id": -100, "text": "Alice:\nthree body"})])
    assert states(manager) == {ContextState.AGGREGATE, ContextState.FROZEN_SOURCE, ContextState.MEMBER_UPDATE,
                               ContextState.LOGICAL, ContextState.BOUNDARY, ContextState.SOURCE}
    # A container update in the shape stored before rows carried their append flag.
    update, = [row for row in queue.aggregation_rows()
               if context_state(decode_aggregation(row.log_context)) is ContextState.MEMBER_UPDATE]
    older_update = decode_aggregation(update.log_context)
    del older_update["appendable"]
    queue.connection.execute("UPDATE outbound_queue SET log_context=? WHERE id=?",
                             (encode_aggregation(older_update), update.id))
    queue.connection.commit()

    restart(manager, make_queue=make_queue)
    assert manager._outbound_scheduler.reconcile_sent_pending() == {sent_id}
    for _ in range(6):
        if not manager._outbound_queue.aggregation_rows():
            break
        complete(manager)
    assert not manager._outbound_queue.aggregation_rows()

    db = manager.channel.db
    container = MsgLog.get(MsgLog.master_msg_id == "-100.1").aggregate
    assert [(child["source_id"], child["source_revision"]) for child in container["children"]] == [("one", 2)]
    log, two = db.resolve_source_member(key[0], "two", "-100", None)
    assert log.aggregate and two["status"] == "active"
    assert db.resolve_source_member(key[0], "solo", "-100", None)[1]["status"] == "removed"
    assert db.resolve_source_member(key[0], "three", "-100", None)[1]["status"] == "active"
    assert db.resolve_source_member(key[0], "topic", "-100", "7")[0].aggregate
    assert "media" in [call[2] for call in manager.transport.calls]


def test_illegal_kind_transitions_name_the_row_and_leave_it_unchanged(runtime):
    manager = runtime
    queue = manager._outbound_queue
    append(manager, "one", 1)
    row = queue.aggregation_rows()[0]
    before = stored(manager, row.id)
    batch = decode_aggregation(row.log_context)

    receipt = manager.transport.send_message(-100, "body")
    with pytest.raises(IllegalAggregationTransitionError, match=rf"row {row.id} of kind 'logical' does not accept receipt"):
        manager.live_aggregation.reconcile(row, receipt, None)
    with pytest.raises(IllegalAggregationTransitionError, match=rf"row {row.id} of kind 'logical' does not accept receipt"):
        queue.handoff_aggregation(row.id, encode_aggregation(dict(batch, handoff=True)), lambda payload: payload)
    assert stored(manager, row.id) == before


def test_illegal_materialisation_stops_delivery_with_the_row_named(runtime):
    manager = runtime
    key = (member_of("unsent", "body", 1)["origin_uid"], -100, None)
    # An edit of a source that never reached a container cannot become a container edit.
    row_id = manager.live_aggregation.queue_deferred_update(key, member_of("unsent", "body", 1), None)
    before = stored(manager, row_id)
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    assert scheduler.stopping
    assert str(scheduler.failure) == (
        f"Aggregation queue row {row_id} of kind 'member_update' does not accept materialize; the row was left "
        "unchanged. Deferred source update requires its confirmed container.")
    assert stored(manager, row_id) == before


def test_confirm_send_refuses_a_batch_that_was_never_sent(runtime):
    manager = runtime
    queue, scheduler = manager._outbound_queue, manager._outbound_scheduler
    append(manager, "one", 1)
    batch = queue.aggregation_rows()[0]
    select_sender = manager.select_sender
    manager.select_sender = lambda row, now: SenderSelectionResult(terminal_error_class="BotRemoved")
    scheduler.dispatch_once()
    manager.select_sender = select_sender
    append(manager, "two", 2)
    later = [row.id for row in queue.aggregation_rows() if row.id != batch.id]
    held = stored(manager, batch.id)
    assert state_and_hold(manager, batch.id)[1].startswith("aggregation_failed:")
    manager.me = SimpleNamespace(id=42)
    reply = Message(77, datetime.datetime.fromtimestamp(3), Chat(-100, "supergroup"),
                    from_user=User(42, "bot", True), text="something else the bot said")
    with pytest.raises(ValueError, match="never sent to Telegram as one message"):
        manager.confirm_queued_delivery(batch.id, reply)
    assert stored(manager, batch.id) == held
    assert decode_aggregation(queue.aggregation_context(batch.id))["kind"] == "logical"
    assert len(later) == 1
    assert state_and_hold(manager, later[0])[0] == "queued"
    assert later[0] in [row.id for row in queue.heads(include_payload=False, ready_only=True)]


def test_receipt_stored_for_an_unsent_batch_by_an_older_version_is_reported_once_and_deferred(runtime, caplog):
    manager = runtime
    queue = manager._outbound_queue
    append(manager, "one", 1)
    row = queue.aggregation_rows()[0]
    # Older versions let a batch that was never sent keep a receipt; only the row itself is written here.
    message = manager.transport.send_message(-100, "body")
    receipt = manager.encode_queued_completion_receipt(message, manager.select_sender(row, 0).selection)
    queue.connection.execute(
        "UPDATE outbound_queue SET delivery_state='sent_pending', completion_receipt=?, delivery_hold=NULL "
        "WHERE id=?", (receipt, row.id))
    queue.connection.commit()
    scheduler = manager._outbound_scheduler
    with caplog.at_level(logging.WARNING):
        for attempt in (1, 2):
            assert scheduler.reconcile_sent_pending(row.id) == set()
            assert stored(manager, row.id) is not None
            attempts = queue.connection.execute(
                "SELECT reconcile_attempts FROM outbound_queue WHERE id=?", (row.id,)).fetchone()[0]
            assert attempts == attempt
            queue.connection.execute("UPDATE outbound_queue SET reconcile_after=0 WHERE id=?", (row.id,))
            queue.connection.commit()
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert f"Receipt of queue row {row.id} (aggregation kind 'logical') cannot be reconciled" in warnings[0].getMessage()
    assert "MsgLog reconciliation failed" not in caplog.text
    assert decode_aggregation(stored(manager, row.id)[8])["kind"] == "logical"


def _member(source_id):
    return {"origin_uid": "origin", "source_id": source_id}


@pytest.mark.parametrize("context, predecessor, waits", [
    ({"kind": "member_update", "updates": [_member("one")]}, None, True),
    ({"kind": "logical", "members": [_member("two")], "inherit": True}, None, True),
    ({"kind": "logical", "members": [_member("two")], "inherit": True, "incoming_boundary": True}, None, False),
    ({"kind": "logical", "members": [_member("two")], "inherit": False}, None, False),
    ({"kind": "boundary", "members": []}, None, False),
    ({"kind": "source", "member": _member("one")}, {"kind": "logical", "members": [_member("one")]}, True),
    ({"kind": "source", "member": _member("two")}, {"kind": "logical", "members": [_member("one")]}, False),
], ids=["container edit", "append into its container", "append after a Telegram message",
        "new batch", "independent message", "output of its member", "output of another member"])
def test_only_rows_that_need_a_failing_predecessor_on_telegram_wait_for_it(context, predecessor, waits):
    assert needs_delivered_predecessor(context, predecessor) is waits
