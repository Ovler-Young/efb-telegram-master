import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from ehforwarderbot import MsgType
from ehforwarderbot.types import MessageID
from peewee import PostgresqlDatabase, SqliteDatabase

from efb_telegram_master import db as db_module
from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.models import MsgLog, MsgLogIngestionScan, database
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.msglog_ingestion_repository import MsgLogIngestionRepository
from efb_telegram_master.msglog_repository import MsgLogRepository

_OLD_MSGLOG_SCHEMA = """
CREATE TABLE "msglog" (
    "master_msg_id" TEXT NOT NULL PRIMARY KEY,
    "master_msg_id_alt" TEXT,
    "slave_message_id" TEXT NOT NULL,
    "text" TEXT NOT NULL,
    "slave_origin_uid" TEXT NOT NULL,
    "slave_origin_display_name" TEXT,
    "slave_member_uid" TEXT,
    "slave_member_display_name" TEXT,
    "media_type" TEXT,
    "mime" TEXT,
    "file_id" TEXT,
    "file_unique_id" TEXT,
    "msg_type" TEXT NOT NULL,
    "pickle" {blob_type},
    "sent_to" TEXT NOT NULL,
    "sender_bot_id" TEXT,
    "time" {time_type}
)
"""


def _create_old_msglog_schema(test_db, *, blob_type="BLOB", time_type="DATETIME"):
    test_db.execute_sql(_OLD_MSGLOG_SCHEMA.format(blob_type=blob_type, time_type=time_type))
    placeholders = ", ".join([test_db.param] * 6)
    test_db.execute_sql(
        f"INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, msg_type, sent_to) VALUES ({placeholders})",
        ("100.1", "old-message", "old text", "tests.slave chat", "Text", "tests.master"),
    )


def _msglog_values(master_msg_id, **values):
    return {
        "master_msg_id": master_msg_id,
        "slave_message_id": f"slave-{master_msg_id}",
        "text": "text",
        "slave_origin_uid": "tests.slave chat",
        "slave_member_uid": "tests.slave author",
        "msg_type": "Text",
        "sent_to": "tests.master",
        **values,
    }


def test_database_restart_retains_msglog_provenance_and_ingestion_scan(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    first_manager = DatabaseManager(SimpleNamespace(channel_id="tests.fresh", config={}))
    second_manager = None
    try:
        first_manager.stop_worker()
        second_manager = DatabaseManager(SimpleNamespace(channel_id="tests.fresh", config={}))
        msglog_columns = {column.name for column in database.get_columns("msglog")}
        scan_columns = {column.name for column in database.get_columns("msglogingestionscan")}
        defaults = {column.name: column.default for column in database.get_columns("msglog")}
    finally:
        if second_manager is not None:
            second_manager.stop_worker()
        database.initialize(original_database)

    assert "provenance" in msglog_columns
    assert defaults["provenance"] == "'live'"
    assert {"source_chat_id", "cursor", "lease_owner", "existing_streak"}.issubset(scan_columns)


def test_database_upgrade_adds_msglog_provenance_without_losing_rows(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        _create_old_msglog_schema(raw_db)
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    first_manager = DatabaseManager(SimpleNamespace(channel_id="tests.upgrade", config={}))
    second_manager = None
    try:
        first_manager.stop_worker()
        second_manager = DatabaseManager(SimpleNamespace(channel_id="tests.upgrade", config={}))
        old_row = MsgLog.get_by_id("100.1")
        live_row = MsgLog.create(**_msglog_values("100.2"))
        ingested_row = MsgLog.create(**_msglog_values("100.3", provenance="mtproto_ingested"))
        provenance_columns = [column.name for column in database.get_columns("msglog") if column.name == "provenance"]
    finally:
        if second_manager is not None:
            second_manager.stop_worker()
        database.initialize(original_database)

    assert provenance_columns == ["provenance"]
    assert old_row.provenance == "live"
    assert live_row.provenance == "live"
    assert ingested_row.provenance == "mtproto_ingested"


def test_concurrent_sqlite_msglog_provenance_upgrade_is_idempotent(tmp_path):
    original_database = database.obj
    test_db = SqliteDatabase(tmp_path / "tgdata.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    database.initialize(test_db)
    test_db.connect()
    try:
        _create_old_msglog_schema(test_db)
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _index: DatabaseManager._create(), range(2)))
        row = MsgLog.get_by_id("100.1")
        provenance_columns = [column.name for column in test_db.get_columns("msglog") if column.name == "provenance"]
    finally:
        test_db.close()
        database.initialize(original_database)

    assert provenance_columns == ["provenance"]
    assert row.provenance == "live"


def test_ingestion_claim_persist_and_idempotence_are_atomic():
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    manager = MsgLogIngestionRepository("tests")
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
        database.initialize(original_database)

    assert row.provenance == "mtproto_ingested"
    assert row.slave_message_id == "mtproto-ingested:100.500"


def test_expired_scan_is_resumable_after_restart():
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    manager = MsgLogIngestionRepository("tests")
    try:
        test_db.create_tables([MsgLogIngestionScan])
        scan = manager.get_or_create_scan(100, 500)
        assert manager.claim_scan(100, "worker-a", 60) is not None
        MsgLogIngestionScan.update(lease_expires_at=datetime.now() - timedelta(seconds=1)).where(MsgLogIngestionScan.id == scan.id).execute()
        resumed = manager.get_resumable_scans()
        assert [item.source_chat_id for item in resumed] == ["100"]
        assert manager.claim_scan(100, "worker-b", 60) is not None
    finally:
        test_db.close()
        database.initialize(original_database)


def test_live_message_overwrites_synthetic_provenance():
    test_db = SqliteDatabase(":memory:")
    manager = MsgLogRepository()
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
            master_msg_id="100.1",
            slave_message_id="mtproto-ingested:100.1",
            text="ingested",
            slave_origin_uid="tests.slave stale",
            slave_member_uid="tests.slave __self__",
            msg_type="Text",
            sent_to="tests.master",
            provenance="mtproto_ingested",
        )
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=1))
        row = MsgLog.get_by_id("100.1")

    assert (row.provenance, row.slave_message_id, row.text) == ("live", "live-message", "live text")


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_HOST"), reason="PostgreSQL test environment is not configured")
def test_postgresql_upgrade_adds_msglog_provenance_without_losing_rows(tmp_path, monkeypatch):
    connection_kwargs = {
        "database": os.environ["TEST_POSTGRES_DB"],
        "host": os.environ["TEST_POSTGRES_HOST"],
        "port": int(os.environ["TEST_POSTGRES_PORT"]),
        "user": os.environ["TEST_POSTGRES_USER"],
        "password": os.environ["TEST_POSTGRES_PASSWORD"],
    }
    database_name = f"etm_msglog_{uuid.uuid4().hex}"
    admin_db = PostgresqlDatabase(**connection_kwargs)
    admin_db.connect()
    admin_db.connection().autocommit = True
    original_database = database.obj
    first_manager = None
    second_manager = None
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        admin_db.execute_sql(f'CREATE DATABASE "{database_name}"')
        test_db = PostgresqlDatabase(database_name, **{key: value for key, value in connection_kwargs.items() if key != "database"})
        test_db.connect()
        _create_old_msglog_schema(test_db, blob_type="BYTEA", time_type="TIMESTAMP")
        test_db.close()
        config = {"database": {"type": "postgresql", "database": database_name, **{key: value for key, value in connection_kwargs.items() if key != "database"}}}
        first_manager = DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-upgrade", config=config))
        first_manager.stop_worker()
        second_manager = DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-upgrade", config=config))
        row = MsgLog.get_by_id("100.1")
        provenance_columns = [column.name for column in database.get_columns("msglog") if column.name == "provenance"]
    finally:
        if second_manager is not None:
            second_manager.stop_worker()
        elif first_manager is not None:
            first_manager.stop_worker()
        database.initialize(original_database)
        admin_db.execute_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
        admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_db.close()

    assert provenance_columns == ["provenance"]
    assert row.provenance == "live"
