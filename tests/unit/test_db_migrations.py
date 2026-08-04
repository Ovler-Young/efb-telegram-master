import os
import logging
import pickle
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from prometheus_client import generate_latest

from ehforwarderbot import Message, MsgType
from ehforwarderbot.types import MessageID

from efb_telegram_master import db as db_module
from efb_telegram_master import utils
from efb_telegram_master.db import (
    ChatAssoc,
    DatabaseManager,
    HistoryMigrationEntry,
    MsgLog,
    MsgLogIngestionScan,
    TopicAssoc,
    TopicRecoveryEntry,
    database,
)
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID, TelegramTopicID


def test_msglog_schema_has_durable_delivery_queue_id(channel):
    columns = {column.name for column in channel.db.database.get_columns("msglog")} if hasattr(channel.db, "database") else None
    if columns is None:
        from efb_telegram_master.db import database
        columns = {column.name for column in database.get_columns("msglog")}
    assert {"sender_bot_id", "delivery_queue_id"}.issubset(columns)


def test_msglog_ingestion_schema_has_live_provenance_and_durable_scan_state():
    from peewee import SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    with test_db.bind_ctx([MsgLog, MsgLogIngestionScan]):
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
        MsgLog.create(
            master_msg_id="100.1", slave_message_id="slave-1", text="first",
            slave_origin_uid="tests.slave", msg_type="Text", sent_to="tests",
        )
        columns = {column.name for column in test_db.get_columns("msglogingestionscan")}
        row = MsgLog.get()

    assert row.provenance == "live"
    assert {
        "source_chat_id", "scan_boundary", "cursor", "existing_streak", "scanned_count",
        "inserted_count", "existing_count", "skipped_count", "lease_owner",
        "lease_expires_at", "status", "error",
    }.issubset(columns)


def test_msglog_ingestion_database_api_is_leased_and_idempotent():
    from peewee import SqliteDatabase

    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    manager = object.__new__(DatabaseManager)
    manager.channel = SimpleNamespace(channel_id="tests")
    try:
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
        scan = manager.get_or_create_msglog_ingestion_scan(100, 500)
        assert manager.claim_msglog_ingestion_scan(100, "worker-a", 60) is not None
        assert manager.claim_msglog_ingestion_scan(100, "worker-b", 60) is None

        content = SimpleNamespace(text="ingested", media_type="Text", mime=None, msg_type="Text")
        assert manager.persist_msglog_ingestion_item(
            scan, source_message_id=500, classification="eligible", slave_uid="tests.slave",
            message=content, lease_owner="worker-a",
        ) == "inserted"
        assert manager.persist_msglog_ingestion_item(
            scan, source_message_id=500, classification="eligible", slave_uid="tests.slave",
            message=content, lease_owner="worker-a",
        ) == "existing"
        row = MsgLog.get(MsgLog.master_msg_id == "100.500")
        assert row.provenance == "mtproto_ingested"
        assert row.file_id is None
        assert row.pickle is None

        manager.finish_msglog_ingestion_scan(
            scan, status="retryable-error", error="temporary", lease_owner="worker-a",
        )
        assert manager.claim_msglog_ingestion_scan(100, "worker-b", 60) is not None
    finally:
        test_db.close()
        database.initialize(original_database)


def test_fresh_sqlite_msglog_rejects_duplicate_delivery_queue_id():
    from peewee import IntegrityError, SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        MsgLog.create(
            master_msg_id="100.1", slave_message_id="slave-1", text="first",
            slave_origin_uid="tests.slave", msg_type="Text", sent_to="tests",
            delivery_queue_id="queue-duplicate",
        )
        with pytest.raises(IntegrityError):
            MsgLog.create(
                master_msg_id="100.2", slave_message_id="slave-2", text="second",
                slave_origin_uid="tests.slave", msg_type="Text", sent_to="tests",
                delivery_queue_id="queue-duplicate",
            )


def test_sqlite_upgrade_rejects_duplicate_msglog_delivery_queue_ids(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    channel = SimpleNamespace(channel_id="tests.msglog-queue-id-migration", config={})
    first = DatabaseManager(channel)
    try:
        database.execute_sql("DROP INDEX IF EXISTS msglog_delivery_queue_id")
        database.execute_sql(
            "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, "
            "msg_type, sent_to, delivery_queue_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("100.1", "slave-1", "first", "tests.slave", "Text", "tests", "queue-duplicate"),
        )
        database.execute_sql(
            "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, "
            "msg_type, sent_to, delivery_queue_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("100.2", "slave-2", "second", "tests.slave", "Text", "tests", "queue-duplicate"),
        )
    finally:
        first.stop_worker()

    try:
        with pytest.raises(RuntimeError, match="Resolve duplicate MsgLog delivery_queue_id values"):
            DatabaseManager(channel)
    finally:
        database.close()
        database.initialize(original_database)


def test_sqlite_upgrade_creates_msglog_queue_id_index(tmp_path, monkeypatch):
    from peewee import IntegrityError

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    channel = SimpleNamespace(channel_id="tests.msglog-queue-id-index", config={})
    first = DatabaseManager(channel)
    try:
        database.execute_sql("DROP INDEX IF EXISTS msglog_delivery_queue_id")
        database.execute_sql(
            "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, "
            "msg_type, sent_to, delivery_queue_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("100.1", "slave-1", "first", "tests.slave", "Text", "tests", "queue-one"),
        )
    finally:
        first.stop_worker()

    upgraded = DatabaseManager(channel)
    try:
        with pytest.raises(IntegrityError):
            database.execute_sql(
                "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, "
                "msg_type, sent_to, delivery_queue_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("100.2", "slave-2", "second", "tests.slave", "Text", "tests", "queue-one"),
            )
    finally:
        upgraded.stop_worker()
        database.initialize(original_database)


def test_sqlite_startup_adds_msglog_provenance_and_ingestion_scan(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    channel = SimpleNamespace(channel_id="tests.msglog-ingestion-migration", config={})
    first = DatabaseManager(channel)
    try:
        MsgLogIngestionScan.drop_table()
        MsgLog.drop_table()
        database.execute_sql(
            "CREATE TABLE msglog (master_msg_id TEXT PRIMARY KEY, master_msg_id_alt TEXT NULL, "
            "slave_message_id TEXT NOT NULL, text TEXT NOT NULL, slave_origin_uid TEXT NOT NULL, "
            "slave_origin_display_name TEXT NULL, slave_member_uid TEXT NULL, "
            "slave_member_display_name TEXT NULL, media_type TEXT NULL, mime TEXT NULL, "
            "file_id TEXT NULL, file_unique_id TEXT NULL, msg_type TEXT NOT NULL, pickle BLOB NULL, "
            "sent_to TEXT NOT NULL, sender_bot_id TEXT NULL, delivery_queue_id TEXT NULL UNIQUE, "
            "time DATETIME NULL)"
        )
        database.execute_sql(
            "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, msg_type, sent_to) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("100.1", "slave-1", "before upgrade", "tests.slave", "Text", "tests"),
        )
    finally:
        first.stop_worker()

    upgraded = DatabaseManager(channel)
    try:
        columns = {column.name for column in database.get_columns("msglog")}
        scan_columns = {column.name for column in database.get_columns("msglogingestionscan")}
        provenance = MsgLog.get(MsgLog.master_msg_id == "100.1").provenance
    finally:
        upgraded.stop_worker()
        database.initialize(original_database)

    assert "provenance" in columns
    assert provenance == "live"
    assert {"source_chat_id", "lease_owner", "existing_streak"}.issubset(scan_columns)


def test_postgresql_msglog_ingestion_migration_adds_provenance_before_scan_table(monkeypatch):
    operations = []

    class FakeDatabase:
        obj = object()

        @staticmethod
        def get_columns(_table_name):
            return []

        @staticmethod
        def execute_sql(sql, _parameters=None):
            operations.append(sql)

        @staticmethod
        def create_tables(models, safe=False):
            operations.append(("create_tables", tuple(model.__name__ for model in models), safe))

    class FakeMigrator:
        def __init__(self, _database):
            pass

        @staticmethod
        def add_column(table_name, column_name, _field):
            operations.append(("add_column", table_name, column_name))
            return "add-column"

    manager = object.__new__(DatabaseManager)
    manager._migrator_cls = FakeMigrator
    monkeypatch.setattr(db_module, "database", FakeDatabase())
    monkeypatch.setattr(db_module, "migrate", lambda *steps: operations.append(("migrate", steps)))

    manager._migrate(8)

    assert operations[0] == ("add_column", "msglog", "provenance")
    assert operations[1][0] == "migrate"
    assert operations[2] == "UPDATE msglog SET provenance = 'live' WHERE provenance IS NULL"
    assert operations[3] == ("create_tables", ("MsgLogIngestionScan",), True)


def test_postgresql_msglog_queue_id_migration_adds_column_before_unique_index(monkeypatch):
    operations = []

    class Cursor:
        @staticmethod
        def fetchone():
            return None

    class FakeDatabase:
        obj = object()
        column_reads = 0

        @staticmethod
        def get_tables():
            return ["msglog"]

        @classmethod
        def get_columns(cls, _table_name):
            cls.column_reads += 1
            if cls.column_reads == 1:
                return []
            return [SimpleNamespace(name="delivery_queue_id")]

        @staticmethod
        def execute_sql(sql, _parameters=None):
            operations.append(sql)
            return Cursor()

    class FakeMigrator:
        def __init__(self, _database):
            pass

        @staticmethod
        def add_column(table_name, column_name, _field):
            operations.append(("add_column", table_name, column_name))
            return "add-column"

    manager = object.__new__(DatabaseManager)
    manager._migrator_cls = FakeMigrator
    manager._is_sqlite = False
    monkeypatch.setattr(db_module, "database", FakeDatabase())
    monkeypatch.setattr(db_module, "migrate", lambda *steps: operations.append(("migrate", steps)))

    manager._migrate(5)

    assert operations[0] == ("add_column", "msglog", "delivery_queue_id")
    assert operations[1][0] == "migrate"
    assert "SELECT delivery_queue_id" in operations[2]
    assert "CREATE UNIQUE INDEX IF NOT EXISTS msglog_delivery_queue_id" in operations[3]


def test_database_method_metrics_record_success_failure_and_bounded_labels(channel):
    metrics = Metrics()
    channel.db.set_metrics(metrics)

    assert channel.db.get_chat_assoc(master_uid="metrics-master") == []
    with pytest.raises(ValueError, match="mutual exclusive"):
        channel.db.get_msg_log()
    with pytest.raises(ValueError, match="database method is invalid"):
        metrics.record_database_method_call("SELECT * FROM msglog", 0.1, "success")

    rendered = generate_latest(metrics.registry).decode()

    assert 'etm_database_method_duration_seconds_count{method="get_chat_assoc"} 1.0' in rendered
    assert 'etm_database_method_duration_seconds_sum{method="get_chat_assoc"}' in rendered
    assert 'etm_database_method_duration_seconds_count{method="get_msg_log"} 1.0' in rendered
    assert 'etm_database_method_failures_total{method="get_msg_log"} 1.0' in rendered
    assert "metrics-master" not in rendered
    assert "SELECT * FROM msglog" not in rendered


def test_history_migration_entry_table_exists():
    from peewee import SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    with test_db.bind_ctx([HistoryMigrationEntry, MsgLog]):
        test_db.create_tables([HistoryMigrationEntry, MsgLog])
        history_columns = {column.name for column in test_db.get_columns("historymigrationentry")}
        msglog_columns = {column.name for column in test_db.get_columns("msglog")}

    assert {
        "slave_chat_id",
        "target_chat_id",
        "message_thread_id",
        "source_master_msg_id",
        "formatted_text",
        "position",
    }.issubset(history_columns)
    assert "source_master_msg_id" not in msglog_columns


def test_msglog_queue_id_reconciliation_reuses_the_existing_row():
    from peewee import SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    manager = object.__new__(DatabaseManager)
    manager.logger = Mock()
    message = SimpleNamespace(
        uid=MessageID("slave-1"), chat=SimpleNamespace(module_id="tests", uid="chat"),
        author=SimpleNamespace(module_id="tests", uid="author"), text="message",
        type=MsgType.Text, type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests"), file_id=None, file_unique_id=None,
        mime=None, is_system=False, attributes=None, commands=None, substitutions=None,
        target=None, sender_bot_id=None, reactions={},
    )

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        for _ in range(2):
            manager.add_or_update_message_log(
                message, SimpleNamespace(chat_id=100, message_id=1),
                delivery_queue_id="queue-reconcile",
            )

        assert MsgLog.select().count() == 1
        assert MsgLog.get().delivery_queue_id == "queue-reconcile"


def test_database_manager_uses_transactional_wal_sqlite(tmp_path, monkeypatch):
    from peewee import SqliteDatabase

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    channel = SimpleNamespace(channel_id="tests.sqlite", config={})
    manager = DatabaseManager(channel)
    try:
        assert isinstance(database.obj, SqliteDatabase)
        assert database.obj.pragma("journal_mode").lower() == "wal"
    finally:
        manager.stop_worker()
        database.initialize(original_database)


def test_sqlite_startup_upgrades_topic_recovery_entry_without_queue_id(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    channel = SimpleNamespace(channel_id="tests.topic-recovery-migration", config={})
    first = DatabaseManager(channel)
    try:
        TopicRecoveryEntry.drop_table()
        database.execute_sql("DROP INDEX IF EXISTS topicrecoveryentry_delivery_queue_id")
        database.execute_sql(
            "CREATE TABLE topicrecoveryentry ("
            "id INTEGER PRIMARY KEY, scan_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL, "
            "classification TEXT NOT NULL, status TEXT NOT NULL, target_message_id INTEGER NULL, "
            "error TEXT NULL, idempotency_key TEXT NOT NULL UNIQUE, updated_at DATETIME NOT NULL)"
        )
        assert "topicrecoveryentry_delivery_queue_id" not in {
            index.name for index in database.get_indexes("topicrecoveryentry")
        }
    finally:
        first.stop_worker()

    upgraded = DatabaseManager(channel)
    try:
        columns = {column.name for column in database.get_columns("topicrecoveryentry")}
        indexes = {index.name for index in database.get_indexes("topicrecoveryentry")}
    finally:
        upgraded.stop_worker()
        database.initialize(original_database)

    assert "delivery_queue_id" in columns
    assert "topicrecoveryentry_delivery_queue_id" in indexes


def test_startup_observes_raw_legacy_rows_without_mutating_them(tmp_path, monkeypatch, caplog):
    from peewee import SqliteDatabase

    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        raw_db.execute_sql(
            "CREATE TABLE outbound_workflow (id INTEGER PRIMARY KEY, state TEXT, marker TEXT)"
        )
        raw_db.execute_sql(
            "CREATE TABLE outbound_task (id INTEGER PRIMARY KEY, state TEXT, marker TEXT)"
        )
        raw_db.execute_sql(
            "INSERT INTO outbound_workflow (id, state, marker) VALUES (1, 'completed', 'workflow-marker')"
        )
        for index, state in enumerate(DatabaseManager._LEGACY_OUTBOUND_STATES, start=1):
            raw_db.execute_sql(
                "INSERT INTO outbound_task (id, state, marker) VALUES (?, ?, ?)",
                (index, state, f"marker-{index}"),
            )
        snapshot = {
            table: raw_db.execute_sql(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in DatabaseManager._LEGACY_OUTBOUND_TABLES
        }
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    channel = SimpleNamespace(channel_id="tests.legacy", config={})
    with caplog.at_level(logging.WARNING, logger="efb_telegram_master.db"):
        manager = DatabaseManager(channel)
    try:
        observed = {
            table: database.execute_sql(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in DatabaseManager._LEGACY_OUTBOUND_TABLES
        }
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert observed == snapshot
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "workflows=1 tasks=8" in warnings[0]
    for state in DatabaseManager._LEGACY_OUTBOUND_STATES:
        assert f"{state}=1" in warnings[0]


def test_topic_assoc_table_exists_and_round_trips(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(11111)
    thread_id = TelegramTopicID(22222)

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    assoc = channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    assert isinstance(assoc, TopicAssoc)
    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) == thread_id
    channel.db.remove_topic_assoc(slave_uid=slave_uid)


def test_create_missing_tables_preserves_existing_chat_assoc(channel):
    channel.db.add_chat_assoc("master-existing", "slave-existing", multiple_slave=True)
    TopicAssoc.drop_table(safe=True)
    HistoryMigrationEntry.drop_table(safe=True)

    channel.db._create_missing_tables()

    assert TopicAssoc.table_exists()
    assert HistoryMigrationEntry.table_exists()
    assert ChatAssoc.get_or_none(ChatAssoc.master_uid == "master-existing") is not None
    channel.db.remove_chat_assoc(master_uid="master-existing")


def test_topic_assoc_is_replaced_for_same_slave_and_thread(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(33333)

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, TelegramTopicID(44444), slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, TelegramTopicID(55555), slave_uid)

    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) == TelegramTopicID(55555)
    rows = list(TopicAssoc.select().where(TopicAssoc.slave_uid == slave_uid))
    assert len(rows) == 1
    channel.db.remove_topic_assoc(slave_uid=slave_uid)


def test_remove_chat_assoc_removes_topic_assoc(channel):
    channel.db.add_chat_assoc("master-topic-cleanup", "slave-topic-cleanup", multiple_slave=True)
    channel.db.add_topic_assoc(TelegramChatID(66666), TelegramTopicID(77777), "slave-topic-cleanup")

    channel.db.remove_chat_assoc(master_uid="master-topic-cleanup")

    assert channel.db.get_topic_thread_id("slave-topic-cleanup", TelegramChatID(66666)) is None


def test_add_or_update_message_log_persists_sender_bot_id(channel, slave):
    chat = slave.chat_with_alias
    author = chat.self
    etm_msg = ETMMsg(
        uid=MessageID("db-test-message"),
        chat=channel.chat_manager.update_chat_obj(chat),
        author=channel.chat_manager.get_or_enrol_member(chat, author),
        text="db test",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=channel,
    )

    master_message = SimpleNamespace(chat_id=123456, message_id=654321)
    channel.db.add_or_update_message_log(etm_msg, master_message, sender_bot_id="777")

    stored = channel.db.get_msg_log(master_msg_id=utils.message_id_to_str(TelegramChatID(123456), TelegramMessageID(654321)))
    assert stored is not None
    assert stored.sender_bot_id == "777"

    stored.delete_instance()


def test_build_etm_msg_restores_sender_bot_id(channel, slave):
    chat = slave.chat_with_alias
    etm_msg = ETMMsg(
        uid=MessageID("restored-message"),
        chat=channel.chat_manager.update_chat_obj(chat),
        author=channel.chat_manager.get_or_enrol_member(chat, chat.other),
        text="restored",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=channel,
    )
    master_message = SimpleNamespace(chat_id=4444, message_id=5555)
    channel.db.add_or_update_message_log(etm_msg, master_message, sender_bot_id="888")

    row = channel.db.get_msg_log(master_msg_id="4444.5555")
    assert row is not None
    restored = row.build_etm_msg(channel.chat_manager)
    assert restored.sender_bot_id == "888"

    row.delete_instance()


def test_reaction_alternate_updates_one_canonical_row_and_clears_retraction():
    from peewee import SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    manager = object.__new__(DatabaseManager)
    manager.logger = Mock()
    reactor = SimpleNamespace(module_id="tests.mocks.slave", uid="reactor")
    message = SimpleNamespace(
        uid=MessageID("reaction-message"),
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.mocks.slave", uid="author"),
        text="message",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
        file_id=None,
        file_unique_id=None,
        mime=None,
        is_system=False,
        attributes=None,
        commands=None,
        substitutions=None,
        target=None,
        sender_bot_id=None,
        reactions={},
    )

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=10))

        message.reactions = {"R0": [reactor]}
        manager.add_or_update_message_log(
            message,
            SimpleNamespace(chat_id=100, message_id=11),
            old_message_id=(TelegramChatID(100), TelegramMessageID(10)),
            sender_bot_id="777",
        )
        row = MsgLog.get()
        assert MsgLog.select().count() == 1
        assert (row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == ("100.10", "100.11", "777")
        assert pickle.loads(bytes(row.pickle))["reactions"] == {"R0": ("tests.mocks.slave reactor",)}

        message.reactions = {}
        manager.add_or_update_message_log(
            message,
            SimpleNamespace(chat_id=100, message_id=11),
            old_message_id=(TelegramChatID(100), TelegramMessageID(11)),
            sender_bot_id="777",
        )
        row = MsgLog.get()
        assert MsgLog.select().count() == 1
        assert (row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == ("100.10", "100.11", "777")
        assert row.pickle is None

        message.reactions = {"R1": [reactor]}
        manager.add_or_update_message_log(
            message,
            SimpleNamespace(chat_id=100, message_id=12),
            old_message_id=(TelegramChatID(100), TelegramMessageID(11)),
            sender_bot_id="888",
        )
        row = MsgLog.get()
        assert MsgLog.select().count() == 1
        assert (row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == ("100.10", "100.12", "888")
        assert pickle.loads(bytes(row.pickle))["reactions"] == {"R1": ("tests.mocks.slave reactor",)}


def test_reaction_alternate_db_failures_preserve_then_update_canonical_row():
    from peewee import SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    manager = object.__new__(DatabaseManager)
    manager.logger = Mock()
    reactor = SimpleNamespace(module_id="tests.mocks.slave", uid="reactor")
    message = SimpleNamespace(
        uid=MessageID("reaction-message"), chat=SimpleNamespace(module_id="tests.mocks.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.mocks.slave", uid="author"), text="message", type=MsgType.Text,
        type_telegram=TGMsgType.Text, deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
        file_id=None, file_unique_id=None, mime=None, is_system=False, attributes=None, commands=None,
        substitutions=None, target=None, sender_bot_id=None, reactions={"NEW": [reactor]},
    )
    scenarios = (
        (None, 10, 11, 12),
        ("100.11", 11, 12, 13),
    )

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        for initial_alt, old_id, failed_id, success_id in scenarios:
            MsgLog.delete().execute()
            MsgLog.create(
                master_msg_id="100.10", master_msg_id_alt=initial_alt, slave_message_id="reaction-message",
                text="old", slave_origin_uid="tests.mocks.slave chat",
                slave_member_uid="tests.mocks.slave author", msg_type=MsgType.Text.name,
                sent_to="tests.mocks.slave", sender_bot_id="700",
                pickle=pickle.dumps({"reactions": {"OLD": ("tests.mocks.slave reactor",)}}),
            )

            with patch.object(MsgLog, "save", side_effect=RuntimeError("db failed")):
                with pytest.raises(RuntimeError, match="db failed"):
                    manager.add_or_update_message_log(
                        message, SimpleNamespace(chat_id=100, message_id=failed_id),
                        old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="800",
                    )

            row = MsgLog.get()
            assert (row.master_msg_id_alt, row.sender_bot_id) == (initial_alt, "700")
            assert pickle.loads(bytes(row.pickle))["reactions"] == {
                "OLD": ("tests.mocks.slave reactor",),
            }

            manager.add_or_update_message_log(
                message, SimpleNamespace(chat_id=100, message_id=success_id),
                old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="900",
            )
            row = MsgLog.get()
            assert MsgLog.select().count() == 1
            assert (row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == (
                "100.10", f"100.{success_id}", "900",
            )
            assert pickle.loads(bytes(row.pickle))["reactions"] == {
                "NEW": ("tests.mocks.slave reactor",),
            }

            message.reactions = {"FINAL": [reactor]}
            manager.add_or_update_message_log(
                message, SimpleNamespace(chat_id=100, message_id=success_id),
                old_message_id=(TelegramChatID(100), TelegramMessageID(success_id)), sender_bot_id="900",
            )
            row = MsgLog.get()
            assert MsgLog.select().count() == 1
            assert row.master_msg_id_alt == f"100.{success_id}"
            assert pickle.loads(bytes(row.pickle))["reactions"] == {
                "FINAL": ("tests.mocks.slave reactor",),
            }
            message.reactions = {"NEW": [reactor]}


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_HOST"),
    reason="PostgreSQL test environment is not configured",
)
def test_postgresql_startup_observes_raw_legacy_rows_without_mutating_them(
    tmp_path, monkeypatch, caplog
):
    from peewee import PostgresqlDatabase

    connection_kwargs = {
        "database": os.environ["TEST_POSTGRES_DB"],
        "host": os.environ["TEST_POSTGRES_HOST"],
        "port": int(os.environ["TEST_POSTGRES_PORT"]),
        "user": os.environ["TEST_POSTGRES_USER"],
        "password": os.environ["TEST_POSTGRES_PASSWORD"],
    }
    database_name = f"etm_legacy_{uuid.uuid4().hex}"
    admin_db = PostgresqlDatabase(**connection_kwargs)
    admin_db.connect()
    admin_db.connection().autocommit = True
    original_database = database.obj
    manager = None
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        admin_db.execute_sql(f'CREATE DATABASE "{database_name}"')
        test_db = PostgresqlDatabase(
            database_name,
            **{key: value for key, value in connection_kwargs.items() if key != "database"},
        )
        test_db.connect()
        with test_db.atomic():
            test_db.execute_sql("CREATE TABLE outbound_workflow (id BIGSERIAL PRIMARY KEY, marker TEXT)")
            test_db.execute_sql(
                "CREATE TABLE outbound_task (id BIGSERIAL PRIMARY KEY, state TEXT, marker TEXT)"
            )
            test_db.execute_sql("INSERT INTO outbound_workflow (marker) VALUES ('workflow-marker')")
            for index, state in enumerate(DatabaseManager._LEGACY_OUTBOUND_STATES, start=1):
                test_db.execute_sql(
                    "INSERT INTO outbound_task (state, marker) VALUES (%s, %s)",
                    (state, f"marker-{index}"),
                )
        snapshot = {
            table: test_db.execute_sql(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in DatabaseManager._LEGACY_OUTBOUND_TABLES
        }
        test_db.close()

        channel = SimpleNamespace(
            channel_id="tests.postgresql",
            config={
                "database": {
                    "type": "postgresql",
                    "database": database_name,
                    **{key: value for key, value in connection_kwargs.items() if key != "database"},
                }
            },
        )
        with caplog.at_level(logging.WARNING, logger="efb_telegram_master.db"):
            manager = DatabaseManager(channel)

        live_db = database.obj
        observed = {
            table: live_db.execute_sql(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in DatabaseManager._LEGACY_OUTBOUND_TABLES
        }
        assert observed == snapshot
        assert "Retained legacy outbound rows: workflows=1 tasks=8" in caplog.text
    finally:
        if manager is not None:
            manager.stop_worker()
        database.initialize(original_database)
        admin_db.execute_sql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database_name,),
        )
        admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_db.close()
