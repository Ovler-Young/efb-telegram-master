from efb_telegram_master.etm_metrics import Metrics


def test_manager_state_exporter_preserves_source_labels_for_shared_chat_fifo():
    import collections
    import threading
    from types import SimpleNamespace
    from unittest.mock import patch

    from efb_telegram_master.bot_manager import QueuedSendTask
    from efb_telegram_master.etm_metrics import _ManagerStateExporter

    first = QueuedSendTask(
        ("slave.a", 100), lambda: None, (), {}, "t1", enqueued_at=90.0, not_before=110.0
    )
    second = QueuedSendTask(
        ("slave.b", 100), lambda: None, (), {}, "t2", enqueued_at=95.0
    )
    manager = SimpleNamespace(
        _send_queues={100: collections.deque([first, second])},
        _send_queues_lock=threading.Lock(),
    )
    exporter = _ManagerStateExporter(manager)

    with patch("efb_telegram_master.etm_metrics.time.monotonic", return_value=100.0), \
            patch("efb_telegram_master.etm_metrics.time.time", return_value=100.0):
        assert sorted(exporter.queue_depth_rows()) == [
            ("slave.a", 100, 1),
            ("slave.b", 100, 1),
        ]
        assert sorted(exporter.queue_oldest_age_rows()) == [
            ("slave.a", 100, 10.0),
            ("slave.b", 100, 5.0),
        ]
        assert exporter.queue_summary() == {
            "queued_tasks": 2,
            "queued_targets": 1,
            "max_target_depth": 2,
            "queue_oldest_age": 10.0,
            "retry_targets": 1,
        }


def test_topn_queue_collector_caps_rows_and_omits_zero_depths():
    rows = [(f"slave.{i}", i, i) for i in range(25)]
    rows.append(("slave.zero", 999, 0))
    metrics = Metrics(top_n=3)
    metrics.register_topn(lambda: rows)

    rendered = metrics.render().decode()

    assert 'slave_id="slave.24"' in rendered
    assert 'chat_id="24"' in rendered
    assert 'slave_id="slave.23"' in rendered
    assert 'slave_id="slave.22"' in rendered
    assert 'slave_id="slave.21"' not in rendered
    assert "slave.zero" not in rendered


def test_topn_queue_collector_returns_empty_family_on_snapshot_error():
    metrics = Metrics(top_n=20)

    def broken_snapshot():
        raise RuntimeError("snapshot failed")

    metrics.register_topn(broken_snapshot)

    rendered = metrics.render().decode()

    assert "etm_send_queue_target_depth" in rendered
    assert "slave_id=" not in rendered


def test_topn_queue_collector_recomputes_each_scrape_without_stale_labels():
    rows = [("slave.a", 1, 10), ("slave.b", 2, 5)]
    metrics = Metrics(top_n=20)
    metrics.register_topn(lambda: rows)

    first = metrics.render().decode()
    rows[:] = [("slave.c", 3, 7)]
    second = metrics.render().decode()

    assert 'slave_id="slave.a"' in first
    assert 'slave_id="slave.a"' not in second
    assert 'slave_id="slave.c"' in second


def test_topn_queue_age_collector_caps_rows_and_omits_zero_ages():
    rows = [(f"slave.{i}", i, float(i)) for i in range(25)]
    rows.append(("slave.zero", 999, 0.0))
    metrics = Metrics(top_n=2)
    metrics.register_queue_oldest_age_topn(lambda: rows)

    rendered = metrics.render().decode()

    assert "etm_send_queue_target_oldest_age_seconds" in rendered
    assert 'slave_id="slave.24"' in rendered
    assert 'slave_id="slave.23"' in rendered
    assert 'slave_id="slave.22"' not in rendered
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

    rendered = metrics.render().decode()

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

    first = metrics.render().decode()
    rows[:] = [("aux", 456, "botB", 2, 18, "available", 3)]
    second = metrics.render().decode()

    assert 'bot_id="123"' in first
    assert 'bot_id="123"' not in second
    assert 'bot_id="456"' in second


def test_bot_chat_occupancy_collector_returns_empty_family_on_snapshot_error():
    metrics = Metrics()

    def broken_snapshot():
        raise RuntimeError("snapshot failed")

    metrics.register_bot_chat_occupancy(broken_snapshot)

    rendered = metrics.render().decode()

    assert "etm_bot_chat_rate_limit_occupancy_chats" in rendered
    assert "bot_id=" not in rendered


def test_debug_event_metrics_render_bounded_labels():
    metrics = Metrics()

    metrics.dispatch_blocked("local_rate_limit")
    metrics.sender_selection("aux", "skipped", "no_aux_member")
    metrics.send_failure("main", "bad_request", "send_message")
    metrics.bad_request("send_message", "invalid_markup")
    metrics.membership_probe(123, "botA", "ok_member")

    rendered = metrics.render().decode()

    assert 'etm_send_dispatch_blocked_total{reason="local_rate_limit"} 1.0' in rendered
    assert 'etm_sender_selection_total{reason="no_aux_member",result="skipped",sender="aux"} 1.0' in rendered
    assert 'etm_telegram_send_failures_total{error_type="bad_request",method="send_message",sender="main"} 1.0' in rendered
    assert 'etm_bad_request_total{method="send_message",reason_class="invalid_markup"} 1.0' in rendered
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

    rendered = metrics.render().decode()

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

    rendered = metrics.render().decode()

    assert "etm_send_queue_oldest_age_seconds 12.5" in rendered
    assert "etm_retry_targets 0.0" in rendered
