from efb_telegram_master.etm_metrics import Metrics


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
