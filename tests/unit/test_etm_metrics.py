import logging
from types import SimpleNamespace
from urllib.request import urlopen

import pytest
from peewee import SqliteDatabase
from prometheus_client import generate_latest

from efb_telegram_master.etm_metrics import DestinationQueueSnapshot, Metrics, WorkerSnapshot
from efb_telegram_master.models import ChatAssoc, HistoryMigrationEntry, MsgLog, SlaveChatInfo, TopicAssoc, database
from efb_telegram_master.persistence.chat_association_repository import ChatAssociationRepository
from efb_telegram_master.persistence.history_migration_repository import HistoryMigrationRepository
from efb_telegram_master.persistence.msglog_repository import MsgLogRepository
from efb_telegram_master.persistence.slave_chat_info_repository import SlaveChatInfoRepository
from efb_telegram_master.runtime.metrics_runtime import start_metrics_server

_DATABASE_METHOD_OPERATIONS = (
    "stop_worker",
    "add_chat_assoc",
    "remove_chat_assoc",
    "get_master_msg_id",
    "get_chat_assoc",
    "add_topic_assoc",
    "get_topic_thread_id",
    "get_topic_slave",
    "get_topic_slaves",
    "remove_topic_assoc",
    "add_or_update_message_log",
    "get_msg_log",
    "delete_msg_log",
    "get_slave_chat_info",
    "set_slave_chat_info",
    "delete_slave_chat_info",
    "get_recent_slave_chats",
    "get_last_message",
    "get_recent_messages",
    "replace_history_migration_entries",
    "has_pending_history_migrations",
    "get_next_history_migration_target",
    "get_history_migration_entry_page",
    "delete_history_migration_entry",
    "get_recent_msglog_page",
    "claim_slave_message_delivery",
    "complete_slave_message_delivery",
    "renew_slave_message_delivery",
    "release_slave_message_delivery",
    "get_resumable_msglog_ingestion_scans",
)


class BrokenCpuProcess:
    def cpu_percent(self, interval=None):
        raise RuntimeError("unavailable")

    @staticmethod
    def memory_info():
        return SimpleNamespace(rss=1)

    @staticmethod
    def io_counters():
        return SimpleNamespace(read_bytes=1, write_bytes=1)


class SupportedProcess:
    def __init__(self):
        self.cpu_samples = iter((0.0, 25.0))

    def cpu_percent(self, interval=None):
        assert interval is None
        return next(self.cpu_samples)

    @staticmethod
    def memory_info():
        return SimpleNamespace(rss=2048)

    @staticmethod
    def io_counters():
        return SimpleNamespace(read_bytes=1024, write_bytes=512)


class NoIoProcess(SupportedProcess):
    @staticmethod
    def io_counters():
        raise OSError("I/O unavailable")


def test_database_metric_operations_are_recordable_and_reject_unknown_labels():
    metrics = Metrics()

    for method in _DATABASE_METHOD_OPERATIONS:
        metrics.record_database_method_call(method, 0.0, "success")

    with pytest.raises(ValueError, match="database method is invalid"):
        metrics.record_database_method_call("unknown_database_method", 0.0, "success")

    rendered = generate_latest(metrics.registry).decode()
    for method in _DATABASE_METHOD_OPERATIONS:
        assert f'etm_database_method_duration_seconds_count{{method="{method}"}} 1.0' in rendered


def test_database_metric_decorators_record_real_repository_operations():
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    metrics = Metrics()
    chat_associations = ChatAssociationRepository(test_database)
    history_migrations = HistoryMigrationRepository(test_database)
    msglogs = MsgLogRepository(test_database)
    slave_chat_info = SlaveChatInfoRepository(test_database)
    for repository in (chat_associations, history_migrations, msglogs, slave_chat_info):
        repository._metrics = metrics

    try:
        test_database.create_tables([ChatAssoc, TopicAssoc, HistoryMigrationEntry, MsgLog, SlaveChatInfo])

        chat_associations.add_chat_assoc("metrics-master", "metrics-slave")
        assert chat_associations.get_chat_assoc(master_uid="metrics-master") == ["metrics-slave"]
        assert history_migrations.get_entries_page("metrics-slave", 12345, None, None, 1) == []
        assert msglogs.get_recent_message_page("metrics-slave", None, 1) == []
        assert slave_chat_info.get_slave_chat_info("metrics", "slave") is None

        rendered = generate_latest(metrics.registry).decode()
    finally:
        test_database.close()
        database.initialize(original_database)

    for method in (
        "add_chat_assoc",
        "get_chat_assoc",
        "get_history_migration_entry_page",
        "get_recent_msglog_page",
        "get_slave_chat_info",
    ):
        assert f'etm_database_method_duration_seconds_count{{method="{method}"}} 1.0' in rendered


def test_process_collector_logs_a_repeated_failure_once_and_keeps_other_metrics(caplog):
    metrics = Metrics(
        process_factory=BrokenCpuProcess,
        network_io_counters=lambda: SimpleNamespace(bytes_recv=1, bytes_sent=1),
    )

    with caplog.at_level(logging.WARNING, logger="efb_telegram_master.etm_metrics"):
        first_scrape = generate_latest(metrics.registry).decode()
        second_scrape = generate_latest(metrics.registry).decode()

    assert "etm_process_resident_memory_bytes" in first_scrape
    assert "etm_host_network_receive_bytes_total 1.0" in first_scrape
    assert "etm_host_network_transmit_bytes_total 1.0" in first_scrape
    assert "etm_host_network_receive_bytes_total 1.0" in second_scrape
    assert [record.message for record in caplog.records].count("Metrics collector process_cpu failed (RuntimeError).") == 1


def test_process_collector_renders_supported_process_observations():
    metrics = Metrics(process_factory=SupportedProcess, network_io_counters=lambda: SimpleNamespace(bytes_recv=3072, bytes_sent=4096))

    rendered = generate_latest(metrics.registry).decode()

    assert "etm_process_cpu_utilization_ratio 0.25" in rendered
    assert "etm_process_resident_memory_bytes 2048.0" in rendered
    assert "etm_process_disk_read_bytes_total 1024.0" in rendered
    assert "etm_process_disk_write_bytes_total 512.0" in rendered
    assert "etm_host_network_receive_bytes_total 3072.0" in rendered
    assert "etm_host_network_transmit_bytes_total 4096.0" in rendered


def test_process_collector_omits_unsupported_io_observations_without_suppressing_network(caplog):
    metrics = Metrics(process_factory=NoIoProcess, network_io_counters=lambda: SimpleNamespace(bytes_recv=1, bytes_sent=2))

    with caplog.at_level(logging.WARNING, logger="efb_telegram_master.etm_metrics"):
        rendered = generate_latest(metrics.registry).decode()
        second_scrape = generate_latest(metrics.registry).decode()

    assert "etm_process_resident_memory_bytes 2048.0" in rendered
    assert "etm_process_disk_read_bytes_total" not in rendered
    assert "etm_process_disk_write_bytes_total" not in rendered
    assert "etm_host_network_receive_bytes_total 1.0" in rendered
    assert "etm_host_network_transmit_bytes_total 2.0" in rendered
    assert "etm_host_network_receive_bytes_total 1.0" in second_scrape
    assert [record.message for record in caplog.records].count("Metrics collector process_disk failed (OSError).") == 1


def test_snapshot_collectors_render_bounded_aggregate_metrics():
    metrics = Metrics()
    metrics.register_destination_queue_collector(lambda: (DestinationQueueSnapshot("least", 1, 3.0), DestinationQueueSnapshot("deepest", 3, 2.0), DestinationQueueSnapshot("middle", 2, None)), top_n=2)
    metrics.register_worker_collector(lambda: WorkerSnapshot(healthy=True, in_flight=4))
    metrics.register_cooldown_collector(lambda: {"main": 1.5, "auxiliary": 0.0})
    metrics.register_auxiliary_count_collector(lambda: {"enabled": 2, "disabled": 1})
    metrics.register_membership_cache_collector(lambda: {"member": 4, "not_member": 2, "unknown_probe_pending": 1})
    metrics.register_rate_limit_occupancy_collector(lambda: {"global": 0.5, "chat": 0.25})

    rendered = generate_latest(metrics.registry).decode()

    assert 'etm_outbound_destination_queue_depth{destination="deepest"} 3.0' in rendered
    assert 'etm_outbound_destination_oldest_age_seconds{destination="deepest"} 2.0' in rendered
    assert 'etm_outbound_destination_queue_depth{destination="middle"} 2.0' in rendered
    assert 'etm_outbound_destination_queue_depth{destination="least"}' not in rendered
    assert "etm_outbound_worker_healthy 1.0" in rendered
    assert "etm_outbound_worker_in_flight 4.0" in rendered
    assert 'etm_outbound_cooldown_seconds{sender_kind="main"} 1.5' in rendered
    assert 'etm_outbound_cooldown_seconds{sender_kind="auxiliary"} 0.0' in rendered
    assert 'etm_auxiliary_bots{state="enabled"} 2.0' in rendered
    assert 'etm_auxiliary_bots{state="disabled"} 1.0' in rendered
    assert 'etm_auxiliary_membership_cache_entries{state="unknown_probe_pending"} 1.0' in rendered
    assert 'etm_rate_limit_occupancy{scope="chat"} 0.25' in rendered


def test_snapshot_collectors_render_empty_snapshots_without_labels():
    metrics = Metrics()
    metrics.register_destination_queue_collector(lambda: (), top_n=0)
    metrics.register_cooldown_collector(lambda: {})
    metrics.register_auxiliary_count_collector(lambda: {})
    metrics.register_membership_cache_collector(lambda: {})
    metrics.register_rate_limit_occupancy_collector(lambda: {})

    rendered = generate_latest(metrics.registry).decode()

    assert "etm_outbound_destination_queue_depth{" not in rendered
    assert "etm_outbound_cooldown_seconds{" not in rendered
    assert "etm_auxiliary_bots{" not in rendered
    assert "etm_auxiliary_membership_cache_entries{" not in rendered
    assert "etm_rate_limit_occupancy{" not in rendered


def test_membership_probe_timeout_is_a_bounded_metric_outcome():
    metrics = Metrics()

    metrics.record_membership_probe("timeout")

    rendered = generate_latest(metrics.registry).decode()
    assert 'etm_auxiliary_membership_probes_total{outcome="timeout"} 1.0' in rendered


def test_metrics_server_logs_the_port_chosen_by_the_os(caplog):
    metrics = Metrics(process_factory=SupportedProcess, network_io_counters=lambda: SimpleNamespace(bytes_recv=1, bytes_sent=2))
    with caplog.at_level(logging.INFO, logger="efb_telegram_master.runtime.metrics_runtime"):
        metrics_server = start_metrics_server("127.0.0.1", 0, metrics.registry)

    try:
        assert metrics_server.server_address[1] > 0
        assert caplog.records[-1].args == (metrics_server.server_address,)
        host, port = metrics_server.server_address
        with urlopen(f"http://{host}:{port}/metrics", timeout=2) as response:
            assert response.status == 200
            assert "etm_process_resident_memory_bytes" in response.read().decode()
    finally:
        metrics_server.shutdown()
        metrics_server.server_close()
        metrics_server.thread.join(timeout=1)
        assert not metrics_server.thread.is_alive()
