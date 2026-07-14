from prometheus_client import generate_latest

from efb_telegram_master.etm_metrics import Metrics


def _render(metrics: Metrics) -> str:
    return generate_latest(metrics.registry).decode()


def test_manager_state_exporter_preserves_source_lane_labels_for_shared_chat(tmp_path):
    import datetime
    from types import SimpleNamespace

    from peewee import SqliteDatabase

    from efb_telegram_master.db import OutboundTask, OutboundWorkflow
    from efb_telegram_master.etm_metrics import _ManagerStateExporter
    from efb_telegram_master.outbound import OutboundPayloadCodec, OutboundRepository, OutboundTaskSpec

    test_db = SqliteDatabase(":memory:")
    with test_db.bind_ctx([OutboundWorkflow, OutboundTask]):
        test_db.create_tables([OutboundWorkflow, OutboundTask])
        repository = OutboundRepository(OutboundPayloadCodec(tmp_path / "spool"))
        for source_key in ("slave.a", "slave.b"):
            repository.create_workflow([OutboundTaskSpec(
                source_key=source_key,
                slave_id=source_key,
                priority=False,
                target_chat_id=100,
                message_thread_id=None,
                operation="send_message",
                args=(),
                kwargs={"chat_id": 100, "text": source_key},
            )])
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        OutboundTask.update(accepted_at=now - datetime.timedelta(seconds=10)).where(
            OutboundTask.source_key == "slave.a"
        ).execute()
        OutboundTask.update(state="leased").where(
            OutboundTask.source_key == "slave.a"
        ).execute()
        OutboundTask.update(
            accepted_at=now - datetime.timedelta(seconds=5),
            available_at=now + datetime.timedelta(seconds=10),
        ).where(OutboundTask.source_key == "slave.b").execute()
        exporter = _ManagerStateExporter(SimpleNamespace())

        assert sorted(exporter.queue_depth_rows()) == [
            ("slave.a", 100, "normal", 1),
            ("slave.b", 100, "normal", 1),
        ]
        ages = sorted(exporter.queue_oldest_age_rows())
        assert 9.0 <= ages[0][3] <= 11.0
        assert 4.0 <= ages[1][3] <= 6.0
        summary = exporter.queue_summary()
        assert summary == {
            "queued_tasks": 2,
            "queued_targets": 2,
            "max_target_depth": 1,
            "queue_oldest_age": summary["queue_oldest_age"],
            "retry_targets": 1,
        }
        assert 9.0 <= summary["queue_oldest_age"] <= 11.0


def test_topn_queue_collector_caps_rows_and_omits_zero_depths():
    rows = [(f"slave.{i}", i, "normal", i) for i in range(25)]
    rows.append(("slave.zero", 999, "normal", 0))
    metrics = Metrics(top_n=3)
    metrics.register_topn(lambda: rows)

    rendered = _render(metrics)

    assert 'source_key="slave.24"' in rendered
    assert 'chat_id="24"' in rendered
    assert 'priority="normal"' in rendered
    assert 'source_key="slave.23"' in rendered
    assert 'source_key="slave.22"' in rendered
    assert 'source_key="slave.21"' not in rendered
    assert "slave.zero" not in rendered


def test_topn_queue_collector_returns_empty_family_on_snapshot_error():
    metrics = Metrics(top_n=20)

    def broken_snapshot():
        raise RuntimeError("snapshot failed")

    metrics.register_topn(broken_snapshot)

    rendered = _render(metrics)

    assert "etm_send_queue_target_depth" in rendered
    assert "source_key=" not in rendered


def test_topn_queue_collector_recomputes_each_scrape_without_stale_labels():
    rows = [("slave.a", 1, "normal", 10), ("slave.b", 2, "normal", 5)]
    metrics = Metrics(top_n=20)
    metrics.register_topn(lambda: rows)

    first = _render(metrics)
    rows[:] = [("slave.c", 3, "blocking", 7)]
    second = _render(metrics)

    assert 'source_key="slave.a"' in first
    assert 'source_key="slave.a"' not in second
    assert 'source_key="slave.c"' in second


def test_topn_queue_age_collector_caps_rows_and_omits_zero_ages():
    rows = [(f"slave.{i}", i, "normal", float(i)) for i in range(25)]
    rows.append(("slave.zero", 999, "normal", 0.0))
    metrics = Metrics(top_n=2)
    metrics.register_queue_oldest_age_topn(lambda: rows)

    rendered = _render(metrics)

    assert "etm_send_queue_target_oldest_age_seconds" in rendered
    assert 'source_key="slave.24"' in rendered
    assert 'source_key="slave.23"' in rendered
    assert 'source_key="slave.22"' not in rendered
    assert "slave.zero" not in rendered


def test_bot_chat_occupancy_collector_renders_distribution_rows():
    metrics = Metrics()
    metrics.register_bot_chat_occupancy(
        lambda: [
            ("aux", 123, "botA", 0, 18, "available", 112),
            ("aux", 123, "botA", 1, 18, "available", 12),
            ("aux", 123, "botA", 18, 18, "cooling", 4),
            ("aux", 456, "botB", 0, 18, "available", 0),
        ]
    )

    rendered = _render(metrics)

    assert "etm_bot_chat_rate_limit_occupancy_chats" in rendered
    assert 'bot_id="123"' in rendered
    assert 'username="botA"' in rendered
    assert 'used="0"' in rendered
    assert 'state="cooling"' in rendered
    assert 'bot_id="456"' not in rendered


def test_bot_chat_occupancy_collector_recomputes_each_scrape_without_stale_labels():
    rows = [("aux", 123, "botA", 1, 18, "available", 2)]
    metrics = Metrics()
    metrics.register_bot_chat_occupancy(lambda: rows)

    first = _render(metrics)
    rows[:] = [("aux", 456, "botB", 2, 18, "available", 3)]
    second = _render(metrics)

    assert 'bot_id="123"' in first
    assert 'bot_id="123"' not in second
    assert 'bot_id="456"' in second


def test_bot_chat_occupancy_collector_returns_empty_family_on_snapshot_error():
    metrics = Metrics()

    def broken_snapshot():
        raise RuntimeError("snapshot failed")

    metrics.register_bot_chat_occupancy(broken_snapshot)

    rendered = _render(metrics)

    assert "etm_bot_chat_rate_limit_occupancy_chats" in rendered
    assert "bot_id=" not in rendered


def test_membership_probe_metric_renders_bounded_labels():
    metrics = Metrics()

    metrics.membership_probe(123, "botA", "ok_member")

    rendered = _render(metrics)

    assert 'etm_membership_probe_total{bot_id="123",outcome="ok_member",username="botA"} 1.0' in rendered


def test_state_collectors_render_current_rows_without_zero_membership_or_cooldown_rows():
    metrics = Metrics()
    metrics.register_bot_cooldowns(
        lambda: [
            ("aux", 123, "botA", 2, 15.5),
            ("aux", 456, "botB", 0, 0.0),
        ]
    )
    metrics.register_membership_cache(
        lambda: [
            (123, "botA", "member", 5),
            (123, "botA", "not_member", 0),
            (123, "botA", "unknown_probe_pending", 1),
        ]
    )
    metrics.register_reserved_slots(
        lambda: [
            ("main", "main", "", 3),
            ("aux", 123, "botA", 7),
        ]
    )

    rendered = _render(metrics)

    assert 'etm_bot_chat_cooldown_chats{bot_id="123",sender="aux",username="botA"} 2.0' in rendered
    assert 'etm_bot_chat_cooldown_max_seconds{bot_id="123",sender="aux",username="botA"} 15.5' in rendered
    assert 'bot_id="456"' not in rendered
    assert 'etm_membership_cache_chats{bot_id="123",status="member",username="botA"} 5.0' in rendered
    assert 'status="not_member"' not in rendered
    assert 'status="unknown_probe_pending"' in rendered
    assert 'etm_reserved_slots{bot_id="main",sender="main",username=""} 3.0' in rendered
    assert 'etm_reserved_slots{bot_id="123",sender="aux",username="botA"} 7.0' in rendered


def test_snapshot_updates_queue_oldest_age_gauge():
    metrics = Metrics()

    metrics.snapshot(
        queued_tasks=2,
        queued_targets=1,
        max_target_depth=2,
        queue_oldest_age=12.5,
        in_flight=0,
        disabled_bot_chats=0,
        retry_targets=0,
        worker_alive=True,
    )

    rendered = _render(metrics)

    assert "etm_send_queue_oldest_age_seconds 12.5" in rendered
    assert "etm_retry_targets 0.0" in rendered


def test_outbound_lifecycle_metrics_render_lane_and_recovery_events():
    metrics = Metrics()

    metrics.task_enqueued(priority=False)
    metrics.task_enqueued(priority=True)
    metrics.recovered("ambiguous_requeued", 2)
    metrics.recovered("sent_pending_log")
    metrics.waiter_timed_out("send_message")
    metrics.workflow_terminal("completed")
    metrics.workflow_terminal("dead")
    metrics.lease_heartbeat(3)

    rendered = _render(metrics)

    assert 'etm_send_tasks_enqueued_total{priority="normal"} 1.0' in rendered
    assert 'etm_send_tasks_enqueued_total{priority="blocking"} 1.0' in rendered
    assert 'etm_outbound_recovery_total{outcome="ambiguous_requeued"} 2.0' in rendered
    assert 'etm_outbound_recovery_total{outcome="sent_pending_log"} 1.0' in rendered
    assert 'etm_outbound_waiter_timeouts_total{operation="send_message"} 1.0' in rendered
    assert 'etm_outbound_workflows_terminal_total{state="completed"} 1.0' in rendered
    assert 'etm_outbound_workflows_terminal_total{state="dead"} 1.0' in rendered
    assert "etm_outbound_lease_heartbeats_total 3.0" in rendered
