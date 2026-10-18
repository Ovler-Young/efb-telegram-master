"""Prometheus evidence through durable aggregation and queue boundaries."""

import copy

import pytest
from prometheus_client import generate_latest

from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.outbound import QueueRequest
from efb_telegram_master.queued_log import decode_aggregation, encode_aggregation
from tests.unit.test_live_aggregate_runtime import runtime, append, complete


@pytest.fixture
def observed(runtime):
    metrics = Metrics()
    runtime._metrics = metrics
    runtime._outbound_queue.metrics = metrics
    runtime.channel.db.set_metrics(metrics)
    runtime._outbound_queue.refresh_depth()
    return runtime, metrics


def sample(metrics, name, labels=None):
    return metrics.registry.get_sample_value("etm_" + name, labels or {})


def test_cancelled_rows_reach_zero_depth_and_preserve_source_state(observed):
    manager, metrics = observed
    key, member = append(manager, "pending", 1)
    assert manager.live_aggregation.remove_source_member(key, (key[0], "pending"))
    assert manager._outbound_queue.cancelled_source((key[0], "pending"))
    assert sample(metrics, "outbound_queue_depth") == 0
    labels = dict(operation="send_message", priority="normal", outcome="cancelled")
    assert sample(metrics, "outbound_queue_removals_total", labels) == 1
    assert sample(metrics, "outbound_queue_residence_seconds_count", labels) == 1
    # Two standalone versions of the same source are both durably withdrawn.
    context = dict(format_version=1, kind="source", key=key, member=member)
    requests = [QueueRequest("send_message", (), {"chat_id": -100, "text": "version"},
                            encode_aggregation(context)) for _ in range(2)]
    manager._outbound_queue.enqueue_many(requests, manager._queue_operation)
    assert len(manager._outbound_queue.cancel_pending_source(key, member)) == 2
    assert manager._outbound_queue.cancel_pending_source(key, member) == {}
    assert sample(metrics, "outbound_queue_depth") == 0
    assert sample(metrics, "outbound_queue_removals_total", labels) == 3
    assert "outbound_completions_total{" not in generate_latest(metrics.registry).decode()


@pytest.mark.parametrize("retain_before", [False, True])
def test_logical_split_only_counts_deleted_original(observed, retain_before):
    manager, metrics = observed
    append(manager, "first", 1)
    row = manager._outbound_queue.aggregation_rows()[0]
    context = decode_aggregation(row.log_context)
    request = QueueRequest("send_message", (), {"chat_id": -100, "text": "independent"})
    manager._outbound_queue.enqueue_many([request], manager._queue_operation,
        logical_split=dict(row_id=row.id, before=context if retain_before else None))
    assert sample(metrics, "outbound_queue_depth") == (2 if retain_before else 1)
    labels = dict(operation="send_message", priority="normal", outcome="replaced")
    assert sample(metrics, "outbound_queue_removals_total", labels) == (None if retain_before else 1)
    assert sample(metrics, "outbound_enqueued_total", dict(operation="send_message", priority="normal")) == 2


def test_handoff_insert_rollback_and_idempotence(observed):
    manager, metrics = observed
    key, _ = append(manager, "first", 1)
    parent = manager._outbound_queue.aggregation_rows()[0]
    context = decode_aggregation(parent.log_context)
    context["handoff"] = True
    request = QueueRequest("edit_message_text", (), {"chat_id": -100, "message_id": 1, "text": "hint", "_required_sender_bot_id": "__main__"},
                           encode_aggregation(dict(context, kind="member_update", routing_hint=True)),
                           predecessor_id=parent.id)
    def broken_successor(payload):
        raise RuntimeError("successor transaction failure")
    with pytest.raises(RuntimeError, match="successor transaction failure"):
        manager._outbound_queue.handoff_aggregation(parent.id, encode_aggregation(context), broken_successor,
                                                    requests=[request], operation_resolver=manager._queue_operation)
    labels = dict(operation="edit_message_text", priority="normal")
    assert sample(metrics, "outbound_enqueued_total", labels) is None
    assert sample(metrics, "outbound_queue_depth") == 1
    identifiers, changed = manager._outbound_queue.handoff_aggregation(parent.id, encode_aggregation(context), lambda p: p,
                                                    requests=[request], operation_resolver=manager._queue_operation)
    assert changed and len(identifiers) == 1
    assert sample(metrics, "outbound_queue_depth") == 2
    assert sample(metrics, "outbound_enqueued_total", labels) == 1
    assert manager._outbound_queue.handoff_aggregation(parent.id, encode_aggregation(context), lambda p: p,
                                                    requests=[request], operation_resolver=manager._queue_operation) == ([], False)
    assert manager._outbound_queue.handoff_aggregation(-1, encode_aggregation(context), lambda p: p) == ([], False)
    assert sample(metrics, "outbound_enqueued_total", labels) == 1


def test_source_log_reads_have_registered_database_metrics(observed):
    manager, metrics = observed
    key, _ = append(manager, "first", 1)
    complete(manager)
    assert len(manager.channel.db.get_source_message_logs(key[0], "first")) == 1
    assert sample(metrics, "database_method_duration_seconds_count", dict(method="get_source_message_logs")) == 1


def test_admissions_publication_updates_and_real_http_scrape(observed):
    import pickle
    from unittest.mock import patch
    from urllib.request import urlopen
    from telegram.error import BadRequest
    from efb_telegram_master.etm_metrics import start_metrics_server
    from efb_telegram_master.db import MsgLog

    manager, metrics = observed
    key, member = append(manager, "first", 1)
    append(manager, "first", 1.05)
    append(manager, "second", 1.1)
    assert sample(metrics, "aggregation_sources_total") == 2
    with patch("efb_telegram_master.live_aggregate.time.time", return_value=2):
        complete(manager)
    assert sample(metrics, "aggregation_containers_total") == 1
    assert sample(metrics, "aggregation_published_batch_members_sum") == 2
    assert sample(metrics, "aggregation_member_confirmation_seconds_sum") == pytest.approx(1.9)
    append(manager, "third", 10)
    complete(manager)
    payload_before_update = sample(metrics, "aggregation_payload_bytes_sum")
    revised = copy.deepcopy(member)
    revised["source_revision"] = 2
    manager.live_aggregation.queue_container_update("-100.1", [revised], key=key)
    manager.transport.error = BadRequest("Message is not modified")
    complete(manager)
    assert sample(metrics, "aggregation_sources_total") == 3
    assert sample(metrics, "aggregation_containers_total") == 1
    assert sample(metrics, "aggregation_published_batch_members_count") == 2
    assert sample(metrics, "aggregation_published_batch_members_sum") == 3
    assert sample(metrics, "aggregation_member_confirmation_seconds_count") == 3
    assert sample(metrics, "aggregation_payload_bytes_count") == 3
    full_payload = len(pickle.dumps(MsgLog.get().aggregate["children"], protocol=5))
    assert full_payload > 0
    assert sample(metrics, "aggregation_payload_bytes_sum") - payload_before_update == full_payload
    assert sample(metrics, "aggregation_rpc_requests_total", dict(operation="edit_message_text", purpose="aggregate", sender_kind="main")) == 2
    server = start_metrics_server("127.0.0.1", 0, metrics.registry)
    try:
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/metrics", timeout=2) as response:
            body = response.read().decode()
            assert response.status == 200
            assert "etm_aggregation_published_batch_members_sum 3.0" in body
            assert "etm_aggregation_payload_bytes_count 3.0" in body
            assert "etm_outbound_queue_depth 0.0" in body
    finally:
        server.shutdown()
        server.server_close()
        server.thread.join(timeout=2)


def test_publication_waits_for_durable_handoff_and_deduplicates_reconciliation(observed, monkeypatch):
    from efb_telegram_master.live_aggregate import LiveTextAggregation
    manager, metrics = observed
    append(manager, "first", 1)
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    next(iter(scheduler.in_flight.values())).future.result(timeout=2)
    finalizer = manager.channel.db.finalize_aggregate_message
    def unavailable(*args, **kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(manager.channel.db, "finalize_aggregate_message", unavailable)
    scheduler.harvest_completed()
    pending = manager._outbound_queue.sent_pending()[0]
    assert sample(metrics, "aggregation_payload_bytes_count") == 0
    monkeypatch.setattr(manager.channel.db, "finalize_aggregate_message", finalizer)
    delete = manager._outbound_queue.delete
    monkeypatch.setattr(manager._outbound_queue, "delete", unavailable)
    scheduler.reconcile_sent_pending(pending.id)
    # It was deferred after DB failure; make this receipt immediately retryable.
    manager._outbound_queue.connection.execute("UPDATE outbound_queue SET reconcile_after=0")
    manager._outbound_queue.connection.commit()
    scheduler.reconcile_sent_pending(pending.id)
    assert sample(metrics, "aggregation_payload_bytes_count") == 1
    assert decode_aggregation(manager._outbound_queue.aggregation_context(pending.id))["handoff"]
    monkeypatch.setattr(manager._outbound_queue, "delete", delete)
    manager.live_aggregation = LiveTextAggregation(manager)
    manager._outbound_queue.connection.execute("UPDATE outbound_queue SET reconcile_after=0")
    manager._outbound_queue.connection.commit()
    assert scheduler.reconcile_sent_pending(pending.id) == {pending.id}
    assert sample(metrics, "aggregation_containers_total") == 1
    assert sample(metrics, "aggregation_published_batch_members_count") == 1
    assert sample(metrics, "aggregation_member_confirmation_seconds_count") == 1
    assert sample(metrics, "aggregation_payload_bytes_count") == 1
    assert len(manager.transport.calls) == 1


def test_rpc_counts_parse_fallback_and_blocking_migration(observed, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from telegram.error import BadRequest, ChatMigrated
    from efb_telegram_master.outbound import QueuePersistenceError, SenderSelection

    manager, metrics = observed
    append(manager, "first", 1)
    row = manager._outbound_queue.aggregation_rows()[0]
    selection = SenderSelection(manager.transport, None)
    with pytest.raises(QueuePersistenceError, match="materialized"):
        manager.execute_queued_call(row, (), {}, selection)
    labels = dict(operation="send_message", purpose="aggregate", sender_kind="main")
    assert sample(metrics, "aggregation_rpc_requests_total", labels) is None
    row = manager.live_aggregation.materialize(row, selection)
    args, kwargs = manager._outbound_queue.decode_payload_raw(row.payload)
    original = manager.transport.send_message
    errors = iter([BadRequest("Can't parse entities: bad formatting"), None, ChatMigrated(-200), None])
    def send(chat_id, text, **kw):
        error = next(errors)
        if error:
            raise error
        return original(chat_id, text, **kw)
    monkeypatch.setattr(manager.transport, "send_message", send)
    manager.execute_queued_call(row, args, kwargs, selection)
    assert sample(metrics, "aggregation_rpc_requests_total", labels) == 2
    manager.channel.chat_binding = SimpleNamespace(chat_migration_by_id=lambda *args: None)
    migrated = manager.execute_queued_call(replace(row, priority=1), args, kwargs, selection)
    assert migrated.chat_id == -200
    assert sample(metrics, "aggregation_rpc_requests_total", labels) == 4


def test_rpc_retry_and_actual_late_claim_operation(observed):
    from telegram.error import RetryAfter
    manager, metrics = observed
    append(manager, "first", 1)
    manager.transport.error = RetryAfter(0)
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    with pytest.raises(RetryAfter):
        next(iter(scheduler.in_flight.values())).future.result(timeout=2)
    scheduler.harvest_completed()
    manager.transport.error = None
    manager.sender_id = "700"
    complete(manager)
    append(manager, "second", 10)
    complete(manager)
    assert sample(metrics, "aggregation_rpc_requests_total", dict(operation="send_message", purpose="aggregate", sender_kind="main")) == 1
    assert sample(metrics, "aggregation_rpc_requests_total", dict(operation="send_message", purpose="aggregate", sender_kind="auxiliary")) == 1
    assert sample(metrics, "aggregation_rpc_requests_total", dict(operation="edit_message_text", purpose="aggregate", sender_kind="auxiliary")) == 1
    assert sample(metrics, "aggregation_containers_total") == 1


def test_metric_failures_preserve_admission_rpc_and_handoff(observed, monkeypatch):
    manager, metrics = observed
    def broken(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")
    monkeypatch.setattr(metrics, "record_enqueued", broken)
    monkeypatch.setattr(metrics, "record_aggregation_source", broken)
    monkeypatch.setattr(metrics, "record_aggregation_rpc", broken)
    monkeypatch.setattr(metrics, "record_aggregation_payload", broken)
    key, _ = append(manager, "first", 1)
    complete(manager)
    assert manager.channel.db.resolve_source_member(key[0], "first", "-100")
    assert len(manager.transport.calls) == 1
    assert sample(metrics, "outbound_queue_depth") == 0
    assert sample(metrics, "aggregation_containers_total") == 1


def test_redirect_hint_payload_and_workflow_rpc_purposes(observed):
    import pickle
    from efb_telegram_master.db import MsgLog
    from tests.unit.test_live_aggregate_source import processor, edit
    manager, metrics = observed
    append(manager, "first", 1)
    append(manager, "second", 1.1, "y" * 400)
    complete(manager)
    payload_before = sample(metrics, "aggregation_payload_bytes_sum")
    edit(processor(manager), "first", "x" * 3950)
    complete(manager)  # Independent replacement.
    assert sample(metrics, "aggregation_payload_bytes_count") == 1
    assert sample(metrics, "outbound_queue_depth") == 1  # Routing hint remains durable.
    complete(manager)
    children = MsgLog.get_by_id("-100.1").aggregate["children"]
    assert children[0]["status"] == "redirected"
    assert sample(metrics, "aggregation_payload_bytes_sum") - payload_before == len(pickle.dumps(children, protocol=5))
    assert sample(metrics, "aggregation_payload_bytes_count") == 2
    assert sample(metrics, "aggregation_sources_total") == 2
    assert sample(metrics, "aggregation_containers_total") == 1
    assert sample(metrics, "aggregation_member_confirmation_seconds_count") == 2
    assert sample(metrics, "aggregation_rpc_requests_total", dict(operation="send_message", purpose="redirect", sender_kind="main")) == 1
    assert sample(metrics, "aggregation_rpc_requests_total", dict(operation="edit_message_text", purpose="routing_hint", sender_kind="main")) == 1
    assert sample(metrics, "outbound_enqueued_total", dict(operation="edit_message_text", priority="normal")) == 1


def test_source_and_boundary_requests_do_not_admit_oversized_members(observed):
    from efb_telegram_master.aggregate import make_source_member
    from tests.unit.test_live_aggregate import source
    from tests.unit.test_live_aggregate_source import processor, edit
    manager, metrics = observed
    key, _ = append(manager, "first", 1)
    complete(manager)
    # Existing scalar outputs keep source context when they are updated later.
    edit(processor(manager), "first", "x" * 4090)
    manager.transport.send_document = lambda chat_id, document, caption=None, **kwargs: manager.transport._save(chat_id, 3, "attachment", None)
    complete(manager)
    complete(manager)  # Full content attachment.
    complete(manager)  # Routing hint.
    edit(processor(manager), "first", "short independent edit")
    complete(manager)
    oversized = make_source_member(source("oversized", "z" * 10000))
    assert not manager.live_aggregation.append(key, oversized)
    assert sample(metrics, "aggregation_sources_total") == 1
    assert sample(metrics, "aggregation_rpc_requests_total", dict(operation="edit_message_text", purpose="source", sender_kind="main")) == 1
    assert sample(metrics, "aggregation_rpc_requests_total", dict(operation="send_document", purpose="boundary", sender_kind="main")) == 1
