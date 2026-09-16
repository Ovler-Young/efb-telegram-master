"""Regression checks for query plans, bounded BLOB reads and durable backoff."""

import sqlite3
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest

from efb_telegram_master import db as db_module
from efb_telegram_master.db import DatabaseManager, MsgLog, database
from efb_telegram_master.outbound import OutboundQueue, OutboundQueueScheduler
from tests.unit.test_outbound import DurableAdapter


@pytest.fixture
def manager(tmp_path, monkeypatch):
    previous = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _: tmp_path)
    instance = DatabaseManager(SimpleNamespace(channel_id="test.performance", config={}))
    yield instance
    instance.stop_worker()
    database.initialize(previous)


def test_lookup_indexes_cover_real_queries_and_survive_restart(manager, tmp_path):
    db = manager._managed_database
    with db.connection_context():
        for index in range(50):
            MsgLog.create(
                master_msg_id=f"master.{index}", slave_message_id=f"source.{index}",
                text="text", slave_origin_uid="slave.chat", msg_type="Text", sent_to="test",
            )
        queries = (
            MsgLog.select().where((MsgLog.slave_origin_uid == "slave.chat") & (MsgLog.slave_message_id == "source.49")).order_by(MsgLog.time.desc(nulls="LAST")).limit(1),
            MsgLog.select().where(MsgLog.slave_origin_uid == "slave.chat").order_by(MsgLog.time.desc(nulls="LAST")).limit(1),
            MsgLog.select().where((MsgLog.master_msg_id == "master.49") | (MsgLog.master_msg_id_alt == "master.49")).limit(1),
        )
        for query in queries:
            sql, args = query.sql()
            plan = " ".join(row[3] for row in db.execute_sql("EXPLAIN QUERY PLAN " + sql, args))
            assert "SEARCH" in plan
            assert "SCAN t1" not in plan
            assert "TEMP B-TREE" not in plan
    manager.stop_worker()
    again = DatabaseManager(SimpleNamespace(channel_id="test.performance", config={}))
    try:
        assert again.get_msg_log(master_msg_id="master.49") is not None
        assert again.get_msg_log(slave_msg_id="source.49", slave_origin_uid="slave.chat") is not None
        assert again._managed_database.is_closed()
    finally:
        again.stop_worker()


def test_partially_upgraded_schema_adds_only_missing_columns(tmp_path, monkeypatch):
    previous = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _: tmp_path)
    initial = DatabaseManager(SimpleNamespace(channel_id="test.schema", config={}))
    initial.stop_worker()
    with closing(sqlite3.connect(tmp_path / "tgdata.db")) as source:
        source.execute("DROP INDEX msglog_master_alt")
        source.execute("ALTER TABLE msglog DROP COLUMN master_msg_id_alt")
        source.execute("ALTER TABLE msglog DROP COLUMN pickle")
        source.execute("ALTER TABLE slavechatinfo DROP COLUMN pickle")
        source.execute("DROP INDEX history_generation_id")
        source.execute("DROP INDEX history_target_generation_position")
        source.execute("ALTER TABLE historymigrationentry DROP COLUMN generation")
        source.execute("DROP TABLE historymigrationtarget")
        source.execute(
            "INSERT INTO historymigrationentry "
            "(id, slave_chat_id, target_chat_id, source_master_msg_id, position, created_at) "
            "VALUES (74, 'slave.chat', '-1001', 'source.1', 0, '2026-01-01')"
        )
        source.commit()
    upgraded = DatabaseManager(SimpleNamespace(channel_id="test.schema", config={}))
    try:
        with upgraded._managed_database.connection_context():
            columns = {column.name for column in upgraded._managed_database.get_columns("msglog")}
            assert {"master_msg_id_alt", "pickle", "sender_bot_id"} <= columns
            assert "msglog_master_alt" in {index.name for index in upgraded._managed_database.get_indexes("msglog")}
            history_indexes = {index.name for index in upgraded._managed_database.get_indexes("historymigrationentry")}
            assert {"history_generation_id", "history_target_generation_position"} <= history_indexes
        assert upgraded.get_next_history_migration_target().ownership_key == "legacy:74"
    finally:
        upgraded.stop_worker()
        database.initialize(previous)


def fill_queue(queue, count, destinations=10, payload_size=65536, state="queued"):
    with queue.connection:
        queue.connection.executemany(
            "INSERT INTO outbound_queue(priority,telegram_chat_id,operation,payload,created_at,"
            "log_context,delivery_state,completion_receipt) VALUES(?,?,?,?,?,?,?,?)",
            [((index // destinations) % 2, index % destinations, "send_message", b"x" * payload_size,
              1000.0, b"context", state, b"receipt" if state == "sent_pending" else None)
             for index in range(count)],
        )


def test_queue_loads_one_blob_per_destination_and_keeps_priority_fifo(tmp_path):
    queue = OutboundQueue(tmp_path)
    fill_queue(queue, 1000)
    tracemalloc.start()
    try:
        heads = queue.heads()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    # Old implementation allocated ~64 MB even though only ten rows survived.
    assert peak < 2 * 1024 * 1024
    assert [row.id for row in heads] == list(range(11, 21))
    assert all(row.priority == 1 and len(row.payload) == 65536 for row in heads)
    queue.delete(11)
    assert queue.heads()[0].id == 31
    queue.close()


def test_sent_pending_never_fetches_upload_blobs_and_is_bounded(tmp_path):
    queue = OutboundQueue(tmp_path)
    fill_queue(queue, 1000, state="sent_pending")
    tracemalloc.start()
    try:
        rows = queue.sent_pending(due_before=1000, limit=32)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(rows) == 32
    assert all(row.payload == b"" and row.log_context == b"context" for row in rows)
    assert peak < 1024 * 1024
    queue.close()


def test_failed_reconciliation_is_fair_bounded_and_not_retried_on_each_wake(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("efb_telegram_master.outbound.time.time", lambda: now[0])
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: now[0])
    queue = OutboundQueue(tmp_path)
    fill_queue(queue, 70, payload_size=10, state="sent_pending")
    adapter = DurableAdapter(reconcile=False)
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        scheduler.dispatch_once()
        assert len(adapter.reconciled) == 32
        for _ in range(20):
            scheduler.dispatch_once()
        assert len(adapter.reconciled) == 32  # New wakes cannot bypass recovery pacing.
        now[0] += 0.25
        scheduler.dispatch_once()
        assert len(adapter.reconciled) == 64
        now[0] += 0.25
        scheduler.dispatch_once()
        assert len(adapter.reconciled) == 70
        assert len(set(adapter.reconciled)) == 70
        for _ in range(20):
            scheduler.dispatch_once()
        assert len(adapter.reconciled) == 70
        assert adapter.calls == []  # Already sent rows must never go to Telegram again.
        adapter.reconcile = True
        now[0] = 1001.0
        for _ in range(3):
            scheduler.dispatch_once()
            now[0] += 0.25
        assert queue.sent_pending() == []
    queue.close()


def test_reconciliation_delay_doubles_caps_and_survives_restart(tmp_path):
    queue = OutboundQueue(tmp_path)
    fill_queue(queue, 1, payload_size=10, state="sent_pending")
    now = 1000.0
    for delay in (1, 2, 4, 8, 16, 32, 60, 60):
        queue.defer_reconciliation([1], now)
        assert queue.sent_pending(due_before=now + delay - 0.01) == []
        assert [row.id for row in queue.sent_pending(due_before=now + delay)] == [1]
        queue.close()
        queue = OutboundQueue(tmp_path)
        assert queue.sent_pending(due_before=now + delay - 0.01) == []
        now += delay
    queue.close()


def test_single_completion_does_not_rescan_other_receipts(tmp_path):
    queue = OutboundQueue(tmp_path)
    fill_queue(queue, 70, payload_size=10, state="sent_pending")
    adapter = DurableAdapter(reconcile=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        assert scheduler.reconcile_sent_pending(row_id=70) == {70}
        assert adapter.reconciled == [70]
        assert len(queue.sent_pending()) == 69
    queue.close()


def test_backoff_persistence_failure_stops_scheduler_without_losing_receipt(tmp_path, monkeypatch):
    queue = OutboundQueue(tmp_path)
    fill_queue(queue, 1, payload_size=10, state="sent_pending")
    adapter = DurableAdapter(reconcile=False)

    def fail(*args):
        raise sqlite3.OperationalError("injected retry persistence failure")

    monkeypatch.setattr(queue, "defer_reconciliation", fail)
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        scheduler.dispatch_once()
        assert scheduler.stopping
        assert [row.id for row in queue.sent_pending()] == [1]
        assert adapter.calls == []
    queue.close()


def test_scheduler_does_not_load_blobs_for_waiting_destinations(tmp_path, monkeypatch):
    from efb_telegram_master.outbound import SenderSelectionResult

    queue = OutboundQueue(tmp_path)
    fill_queue(queue, 100, destinations=10)
    adapter = DurableAdapter(reconcile=True)
    loads = []
    original = queue.load_queued

    def load(row_id):
        loads.append(row_id)
        return original(row_id)

    monkeypatch.setattr(queue, "load_queued", load)
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        scheduler.in_flight_destinations.update(range(10))
        scheduler.dispatch_once()
        assert loads == []
        scheduler.in_flight_destinations.clear()
        scheduler._permits.acquire()
        scheduler.dispatch_once()
        assert loads == []
        scheduler._permits.release()
        with monkeypatch.context() as unavailable:
            unavailable.setattr(adapter, "select_sender", lambda row, now: SenderSelectionResult(retry_at=now + 60))
            scheduler.dispatch_once()
            assert loads == []
        monkeypatch.setattr(adapter, "acquire_sender_limits", lambda selection, chat_id: False)
        for _ in range(3):
            scheduler.dispatch_once()
        assert loads == []
        assert len(queue.heads(include_payload=False)) == 10
        assert adapter.calls == []
    queue.close()


def test_history_preparation_and_restart_do_not_materialize_the_backlog(manager):
    import logging
    import threading
    from concurrent.futures import Future
    from efb_telegram_master.chat_binding import ChatBindingManager

    count = 512
    db = manager._managed_database
    with db.connection_context(), db.atomic():
        db.connection().executemany(
            "INSERT INTO msglog(master_msg_id,slave_message_id,text,slave_origin_uid,msg_type,sent_to,media_type,time) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ((f"123.{i:05d}", str(i), "x" * 32768, "history", "Image", "test", "Photo",
              None if i % 3 == 0 else "2020-01-01 00:00:00") for i in range(count)),
        )
    binding = object.__new__(ChatBindingManager)
    binding.db = manager
    binding.logger = logging.getLogger("tests.history-memory")
    binding._history_migration_lock = threading.Lock()
    tracemalloc.start()
    try:
        assert binding._queue_history_migration_entries("history", 123) == count
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    with db.connection_context(), db.atomic():
        expected = [row[0] for row in db.execute_sql(
            "SELECT source_master_msg_id FROM historymigrationentry ORDER BY position"
        )]
        assert len(set(expected)) == count
        # The resumed table also contains substantial text, independently of
        # the large source MsgLog that was paged above.
        db.execute_sql("UPDATE historymigrationentry SET formatted_text = ?", ("h" * 32768,))
    sent = []

    def enqueue(**kwargs):
        sent.extend(kwargs["history_entry_ids"])
        future = Future()
        future.set_result(None)
        return future

    binding.bot = SimpleNamespace(
        enqueue_history_operation=enqueue, history_ownership_page=lambda **kwargs: [],
        owned_history_entries=lambda keys: set(), forget_history_entries=lambda keys: None,
    )
    tracemalloc.start()
    try:
        binding._process_pending_history_migrations()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024
    assert len(sent) == count and len(set(sent)) == count
    assert not manager.has_pending_history_migrations()
