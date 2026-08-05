import logging
from types import SimpleNamespace

from prometheus_client import CollectorRegistry, generate_latest

from efb_telegram_master.etm_metrics import Metrics, start_metrics_server


class BrokenCpuProcess:
    def cpu_percent(self, interval=None):
        raise RuntimeError("unavailable")

    @staticmethod
    def memory_info():
        return SimpleNamespace(rss=1)

    @staticmethod
    def io_counters():
        return SimpleNamespace(read_bytes=1, write_bytes=1)


def test_process_collector_logs_a_repeated_failure_once_and_keeps_other_metrics(caplog):
    metrics = Metrics(
        process_factory=BrokenCpuProcess,
        network_io_counters=lambda: SimpleNamespace(bytes_recv=1, bytes_sent=1),
    )

    with caplog.at_level(logging.WARNING, logger="efb_telegram_master.etm_metrics"):
        first_scrape = generate_latest(metrics.registry).decode()
        generate_latest(metrics.registry)

    assert "etm_process_resident_memory_bytes" in first_scrape
    assert [record.message for record in caplog.records].count(
        "Metrics collector process_cpu failed (RuntimeError)."
    ) == 1


def test_metrics_server_logs_the_port_chosen_by_the_os(caplog):
    with caplog.at_level(logging.INFO, logger="efb_telegram_master.etm_metrics"):
        metrics_server = start_metrics_server("127.0.0.1", 0, CollectorRegistry())

    try:
        assert metrics_server.server_address[1] > 0
        assert caplog.records[-1].args == (metrics_server.server_address,)
    finally:
        metrics_server.shutdown()
        metrics_server.server_close()
        metrics_server.thread.join(timeout=1)
