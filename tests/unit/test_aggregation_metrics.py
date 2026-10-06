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
