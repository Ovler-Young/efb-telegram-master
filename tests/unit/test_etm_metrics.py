from types import SimpleNamespace
from urllib.request import urlopen

import pytest
from prometheus_client import generate_latest

from efb_telegram_master.etm_metrics import (
    DestinationQueueSnapshot,
    Metrics,
    WorkerSnapshot,
    start_metrics_server,
)


def _render(metrics: Metrics) -> str:
    return generate_latest(metrics.registry).decode()


class _SupportedProcess:
    def __init__(self) -> None:
        self.cpu_percent_values = iter((0.0, 12.5))

    def cpu_percent(self, interval: None) -> float:
        assert interval is None
        return next(self.cpu_percent_values)

    @staticmethod
    def memory_info() -> SimpleNamespace:
        return SimpleNamespace(rss=4096)

    @staticmethod
    def io_counters() -> SimpleNamespace:
        return SimpleNamespace(read_bytes=1024, write_bytes=2048)


def _supported_host_network_counters() -> SimpleNamespace:
    return SimpleNamespace(bytes_recv=3072, bytes_sent=4096)


def test_process_collector_renders_supported_process_observations():
    metrics = Metrics(
        process_factory=_SupportedProcess,
        network_io_counters=_supported_host_network_counters,
    )

    rendered = _render(metrics)

    assert "etm_process_cpu_utilization_ratio 0.125" in rendered
    assert "etm_process_resident_memory_bytes 4096.0" in rendered
    assert "etm_process_disk_read_bytes_total 1024.0" in rendered
    assert "etm_process_disk_write_bytes_total 2048.0" in rendered
    assert "etm_host_network_receive_bytes_total 3072.0" in rendered
    assert "etm_host_network_transmit_bytes_total 4096.0" in rendered
    assert "# HELP etm_host_network_receive_bytes_total Cumulative network bytes received by the host." in rendered


class _UnsupportedIoProcess:
    def cpu_percent(self, interval: None) -> float:
        assert interval is None
        return 0.0

    @staticmethod
    def memory_info() -> SimpleNamespace:
        return SimpleNamespace(rss=1024)

    @staticmethod
    def io_counters() -> SimpleNamespace:
        raise NotImplementedError


def test_process_collector_omits_unsupported_io_observations():
    def unsupported_network_io_counters() -> SimpleNamespace:
        raise NotImplementedError

    rendered = _render(
        Metrics(
            process_factory=_UnsupportedIoProcess,
            network_io_counters=unsupported_network_io_counters,
        )
    )

    assert "etm_process_resident_memory_bytes 1024.0" in rendered
    assert "etm_process_disk_read_bytes_total" not in rendered
    assert "etm_process_disk_write_bytes_total" not in rendered
    assert "etm_host_network_receive_bytes_total" not in rendered
    assert "etm_host_network_transmit_bytes_total" not in rendered


class _PartiallyFailingProcess:
    def cpu_percent(self, interval: None) -> float:
        assert interval is None
        raise OSError("process exited")

    @staticmethod
    def memory_info() -> SimpleNamespace:
        return SimpleNamespace(rss=2048)

    @staticmethod
    def io_counters() -> SimpleNamespace:
        raise OSError("I/O unavailable")


def test_process_collector_isolates_metric_source_errors():
    rendered = _render(
        Metrics(
            process_factory=_PartiallyFailingProcess,
            network_io_counters=_supported_host_network_counters,
        )
    )

    assert "etm_process_cpu_utilization_ratio" not in rendered
    assert "etm_process_resident_memory_bytes 2048.0" in rendered
    assert "etm_process_disk_read_bytes_total" not in rendered
    assert "etm_host_network_receive_bytes_total 3072.0" in rendered
    assert "etm_host_network_transmit_bytes_total 4096.0" in rendered


def test_queue_metrics_render_every_closed_matrix_event():
    metrics = Metrics()

    metrics.record_enqueued("blocking", "send_message")
    metrics.set_queue_depth(3)
    metrics.record_removal("normal", "delete_message", "terminal_discard", 1.25)
    metrics.record_dequeued("normal", "delete_message")
    metrics.record_dispatch_failure("normal", "delete_message")
    metrics.increment_in_flight("blocking", "send_message", "main")
    metrics.decrement_in_flight("blocking", "send_message", "main")
    metrics.record_completion("blocking", "send_message", "main", "failure")
    metrics.record_queue_dispatch("submitted")
    metrics.record_queue_wait("blocking", "send_message", 0.25)
    metrics.record_executor_attempt_duration("blocking", "send_message", "failure", 0.5)
    metrics.record_queue_lifetime("blocking", "send_message", "failure", 1.0)
    metrics.record_retry("blocking", "send_message", "rate_limit")
    metrics.record_failure("blocking", "send_message", "execution")

    rendered = _render(metrics)

    assert 'etm_outbound_enqueued_total{operation="send_message",priority="blocking"} 1.0' in rendered
    assert "etm_outbound_queue_depth 3.0" in rendered
    assert 'etm_outbound_queue_residence_seconds_count{operation="delete_message",outcome="terminal_discard",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_queue_removals_total{operation="delete_message",outcome="terminal_discard",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_dequeued_total{operation="delete_message",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_dispatch_failures_total{operation="delete_message",priority="normal"} 1.0' in rendered
    assert 'etm_outbound_in_flight{operation="send_message",priority="blocking",sender_kind="main"} 0.0' in rendered
    assert 'etm_outbound_completions_total{operation="send_message",outcome="failure",priority="blocking",sender_kind="main"} 1.0' in rendered
    assert 'etm_outbound_queue_dispatches_total{outcome="submitted"} 1.0' in rendered
    assert 'etm_outbound_queue_wait_seconds_count{operation="send_message",priority="blocking"} 1.0' in rendered
    assert 'etm_outbound_executor_attempt_duration_seconds_count{operation="send_message",outcome="failure",priority="blocking"} 1.0' in rendered
    assert 'etm_outbound_queue_lifetime_seconds_count{operation="send_message",outcome="failure",priority="blocking"} 1.0' in rendered
    assert 'etm_outbound_retries_total{operation="send_message",priority="blocking",reason="rate_limit"} 1.0' in rendered
    assert 'etm_outbound_failures_total{operation="send_message",priority="blocking",stage="execution"} 1.0' in rendered


def test_snapshot_collectors_render_bounded_aggregate_metrics():
    metrics = Metrics()
    metrics.register_destination_queue_collector(
        lambda: (
            DestinationQueueSnapshot("least", 1, 3.0),
            DestinationQueueSnapshot("deepest", 3, 2.0),
            DestinationQueueSnapshot("middle", 2, None),
        ),
        top_n=2,
    )
    metrics.register_worker_collector(lambda: WorkerSnapshot(healthy=True, in_flight=4))
    metrics.register_cooldown_collector(lambda: {"main": 1.5, "auxiliary": 0.0})
    metrics.register_auxiliary_count_collector(lambda: {"enabled": 2, "disabled": 1})
    metrics.register_membership_cache_collector(
        lambda: {"member": 4, "not_member": 2, "unknown_probe_pending": 1}
    )
    metrics.register_rate_limit_occupancy_collector(lambda: {"global": 0.5, "chat": 0.25})

    rendered = _render(metrics)

    assert 'etm_outbound_destination_queue_depth{destination="deepest"} 3.0' in rendered
    assert 'etm_outbound_destination_queue_depth{destination="middle"} 2.0' in rendered
    assert 'etm_outbound_destination_queue_depth{destination="least"}' not in rendered
    assert 'etm_outbound_destination_oldest_age_seconds{destination="deepest"} 2.0' in rendered
    assert 'etm_outbound_destination_oldest_age_seconds{destination="middle"}' not in rendered
    assert "etm_outbound_worker_healthy 1.0" in rendered
    assert "etm_outbound_worker_in_flight 4.0" in rendered
    assert 'etm_outbound_cooldown_seconds{sender_kind="main"} 1.5' in rendered
    assert 'etm_auxiliary_bots{state="enabled"} 2.0' in rendered
    assert 'etm_auxiliary_membership_cache_entries{state="unknown_probe_pending"} 1.0' in rendered
    assert 'etm_rate_limit_occupancy{scope="chat"} 0.25' in rendered


def test_snapshot_collectors_render_empty_snapshots_without_labels():
    metrics = Metrics()
    metrics.register_destination_queue_collector(lambda: (), top_n=0)
    metrics.register_cooldown_collector(lambda: {})
    metrics.register_auxiliary_count_collector(lambda: {})
    metrics.register_membership_cache_collector(lambda: {})
    metrics.register_rate_limit_occupancy_collector(lambda: {})

    rendered = _render(metrics)

    assert "etm_outbound_destination_queue_depth{" not in rendered
    assert "etm_outbound_cooldown_seconds{" not in rendered
    assert "etm_auxiliary_bots{" not in rendered
    assert "etm_auxiliary_membership_cache_entries{" not in rendered
    assert "etm_rate_limit_occupancy{" not in rendered


def test_metrics_server_serves_http_and_thread_stops_after_shutdown():
    metrics = Metrics()
    metrics.set_queue_depth(2)
    server = start_metrics_server("127.0.0.1", 0, metrics.registry)
    try:
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/metrics", timeout=2) as response:
            assert response.status == 200
            assert "etm_outbound_queue_depth 2.0" in response.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        server.thread.join(timeout=2)

    assert not server.thread.is_alive()


@pytest.mark.parametrize(
    "call",
    [
        lambda metrics: metrics.record_enqueued("urgent", "send_message"),
        lambda metrics: metrics.record_enqueued("normal", "get_me"),
        lambda metrics: metrics.record_removal("normal", "send_message", "requeued", 0.0),
        lambda metrics: metrics.increment_in_flight("normal", "send_message", "bot-42"),
        lambda metrics: metrics.record_completion("normal", "send_message", "main", "cancelled"),
        lambda metrics: metrics.set_queue_depth(-1),
        lambda metrics: metrics.record_queue_dispatch("reserved"),
        lambda metrics: metrics.record_queue_wait("normal", "send_message", -0.1),
        lambda metrics: metrics.record_retry("normal", "send_message", "unbounded"),
        lambda metrics: metrics.record_failure("normal", "send_message", "retry"),
    ],
)
def test_queue_metrics_reject_unbounded_or_invalid_values(call):
    with pytest.raises(ValueError):
        call(Metrics())
