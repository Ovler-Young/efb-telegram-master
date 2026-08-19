import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from ehforwarderbot import MsgType
from ehforwarderbot.types import MessageID
from peewee import IntegrityError, PostgresqlDatabase, SqliteDatabase

from efb_telegram_master.core.db import DatabaseManager
from efb_telegram_master.core.models import DATABASE_MODELS, ChatAssoc, HistoryMigrationEntry, MsgLog, MsgLogIngestionScan, TopicAssoc
from efb_telegram_master.delivery.message import ETMMsg
from efb_telegram_master.delivery.msg_type import TGMsgType
from efb_telegram_master.persistence import database_initializer
from efb_telegram_master.persistence.msglog_ingestion_repository import MsgLogIngestionRepository
from efb_telegram_master.persistence.msglog_repository import MsgLogRepository
from efb_telegram_master.persistence.schema_migration import DatabaseSchemaMigrator
from tests.unit.msglog_schema_support import create_old_msglog_schema, msglog_values, postgres_connection_kwargs


def test_database_upgrade_adds_msglog_provenance_without_losing_rows(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        create_old_msglog_schema(raw_db)
    finally:
        raw_db.close()

    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)
    config = SimpleNamespace(database={})
    first_manager = DatabaseManager(SimpleNamespace(channel_id="tests.upgrade", config=config))
    second_manager = None
    model_binding = None
    try:
        first_manager.stop_worker()
        second_manager = DatabaseManager(SimpleNamespace(channel_id="tests.upgrade", config=config))
        model_binding = second_manager.current_database.bind_ctx(DATABASE_MODELS)
        model_binding.__enter__()
        old_row = MsgLog.get_by_id("100.1")
        live_row = MsgLog.create(**msglog_values("100.2"))
        ingested_row = MsgLog.create(**msglog_values("100.3", provenance="mtproto_ingested"))
        provenance_columns = [column.name for column in second_manager.current_database.get_columns("msglog") if column.name == "provenance"]
    finally:
        if second_manager is not None:
            second_manager.stop_worker()
        if model_binding is not None:
            model_binding.__exit__(None, None, None)

    assert provenance_columns == ["provenance"]
    assert old_row.provenance == "live"
    assert live_row.provenance == "live"
    assert ingested_row.provenance == "mtproto_ingested"


def test_concurrent_sqlite_msglog_provenance_upgrade_is_idempotent(tmp_path):
    test_db = SqliteDatabase(tmp_path / "tgdata.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    model_binding = test_db.bind_ctx(DATABASE_MODELS)
    model_binding.__enter__()
    test_db.connect()
    try:
        create_old_msglog_schema(test_db)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _index: DatabaseSchemaMigrator(test_db).create(), range(2)))
        row = MsgLog.get_by_id("100.1")
        provenance_columns = [column.name for column in test_db.get_columns("msglog") if column.name == "provenance"]
    finally:
        test_db.close()
        model_binding.__exit__(None, None, None)

    assert provenance_columns == ["provenance"]
    assert row.provenance == "live"


def test_ingestion_claim_persist_and_idempotence_are_atomic():
    test_db = SqliteDatabase(":memory:")
    model_binding = test_db.bind_ctx(DATABASE_MODELS)
    model_binding.__enter__()
    test_db.connect()
    manager = MsgLogIngestionRepository("tests", test_db)
    try:
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
        scan = manager.get_or_create_scan(100, 500)
        assert manager.claim_scan(100, "worker-a", 60) is not None
        assert manager.claim_scan(100, "worker-b", 60) is None
        content = SimpleNamespace(text="ingested", media_type="Text", mime=None, msg_type="Text", time=datetime(2026, 8, 4))
        assert manager.persist_item(scan, source_message_id=500, classification="eligible", slave_uid="tests.slave target", message=content, lease_owner="worker-a") == "inserted"
        assert manager.persist_item(scan, source_message_id=500, classification="eligible", slave_uid="tests.slave target", message=content, lease_owner="worker-a") == "existing"
        row = MsgLog.get_by_id("100.500")
    finally:
        test_db.close()
        model_binding.__exit__(None, None, None)

    assert row.provenance == "mtproto_ingested"
    assert row.slave_message_id == "mtproto-ingested:100.500"


def test_live_and_ingestion_fallback_times_are_utc_naive_and_sort_together():
    original_timezone = os.environ.get("TZ")
    os.environ["TZ"] = "Pacific/Kiritimati"
    time.tzset()
    test_db = SqliteDatabase(":memory:")
    model_binding = test_db.bind_ctx(DATABASE_MODELS)
    model_binding.__enter__()
    test_db.connect()
    manager = MsgLogIngestionRepository("tests", test_db)
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
        MsgLog.create(**msglog_values("live"))
        scan = manager.get_or_create_scan(100, 2)
        assert manager.claim_scan(100, "worker-a", 60) is not None
        content = SimpleNamespace(text="ingested", media_type="Text", mime=None, msg_type="Text", time=None)
        assert manager.persist_item(scan, source_message_id=2, classification="eligible", slave_uid="tests.slave target", message=content, lease_owner="worker-a") == "inserted"
        rows = list(MsgLog.select().order_by(MsgLog.time, MsgLog.master_msg_id))
    finally:
        test_db.close()
        model_binding.__exit__(None, None, None)
        if original_timezone is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = original_timezone
        time.tzset()

    after = datetime.now(timezone.utc).replace(tzinfo=None)
    assert [row.master_msg_id for row in rows] == ["live", "100.2"]
    assert all(row.time.tzinfo is None for row in rows)
    assert all(before <= row.time <= after for row in rows)


def test_live_message_overwrites_synthetic_provenance():
    test_db = SqliteDatabase(":memory:")
    manager = MsgLogRepository(test_db)
    message = ETMMsg(
        uid=MessageID("live-message"),
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.slave", uid="author"),
        text="live text",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests.master"),
    )
    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        MsgLog.create(
            **msglog_values(
                "100.1", slave_message_id="mtproto-ingested:100.1", text="ingested", slave_origin_uid="tests.slave stale", slave_member_uid="tests.slave __self__", provenance="mtproto_ingested"
            )
        )
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=1))
        row = MsgLog.get_by_id("100.1")

    assert (row.provenance, row.slave_message_id, row.text) == ("live", "live-message", "live text")


def test_live_message_upsert_wins_when_ingestion_inserts_after_lookup(monkeypatch):
    test_db = SqliteDatabase(":memory:")
    manager = MsgLogRepository(test_db)
    message = ETMMsg(
        uid=MessageID("live-message"),
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.slave", uid="author"),
        text="live text",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests.master"),
    )
    original_get_or_none = MsgLog.get_or_none

    def insert_ingested_after_lookup(*_args, **_kwargs):
        MsgLog.create(
            **msglog_values(
                "100.1", slave_message_id="mtproto-ingested:100.1", text="ingested", slave_origin_uid="tests.slave stale", slave_member_uid="tests.slave __self__", provenance="mtproto_ingested"
            )
        )
        return None

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        monkeypatch.setattr(MsgLog, "get_or_none", insert_ingested_after_lookup)
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=1))
        monkeypatch.setattr(MsgLog, "get_or_none", original_get_or_none)
        row = MsgLog.get_by_id("100.1")

    assert (row.provenance, row.slave_message_id, row.text) == ("live", "live-message", "live text")


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_HOST"), reason="PostgreSQL test environment is not configured")
def test_postgresql_upgrade_adds_msglog_provenance_without_losing_rows(tmp_path, monkeypatch):
    connection_kwargs = postgres_connection_kwargs()
    database_name = f"etm_msglog_{uuid.uuid4().hex}"
    admin_db = PostgresqlDatabase(**connection_kwargs)
    admin_db.connect()
    admin_db.connection().autocommit = True
    first_manager = None
    second_manager = None
    model_binding = None
    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        admin_db.execute_sql(f'CREATE DATABASE "{database_name}"')
        test_db = PostgresqlDatabase(database_name, **{key: value for key, value in connection_kwargs.items() if key != "database"})
        test_db.connect()
        create_old_msglog_schema(test_db, blob_type="BYTEA", time_type="TIMESTAMP")
        test_db.close()
        config = SimpleNamespace(database={"type": "postgresql", "database": database_name, **{key: value for key, value in connection_kwargs.items() if key != "database"}})
        first_manager = DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-upgrade", config=config))
        first_manager.stop_worker()
        second_manager = DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-upgrade", config=config))
        model_binding = second_manager.current_database.bind_ctx(DATABASE_MODELS)
        model_binding.__enter__()
        row = MsgLog.get_by_id("100.1")
        provenance_columns = [column.name for column in second_manager.current_database.get_columns("msglog") if column.name == "provenance"]
        ChatAssoc.create(master_uid="master", slave_uid="slave")
        TopicAssoc.create(topic_chat_id="100", message_thread_id="200", slave_uid="slave")
        HistoryMigrationEntry.create(slave_chat_id="slave", target_chat_id="100", source_master_msg_id="100.1", position=0)
        with pytest.raises(IntegrityError):
            ChatAssoc.create(master_uid="master-other", slave_uid="slave")
        with pytest.raises(IntegrityError):
            TopicAssoc.create(topic_chat_id="100", message_thread_id="200", slave_uid="slave-other")
        with pytest.raises(IntegrityError):
            HistoryMigrationEntry.create(slave_chat_id="slave", target_chat_id="100", source_master_msg_id="100.2", position=0)
    finally:
        if second_manager is not None:
            second_manager.stop_worker()
        elif first_manager is not None:
            first_manager.stop_worker()
        if model_binding is not None:
            model_binding.__exit__(None, None, None)
        admin_db.execute_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
        admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_db.close()

    assert provenance_columns == ["provenance"]
    assert row.provenance == "live"
