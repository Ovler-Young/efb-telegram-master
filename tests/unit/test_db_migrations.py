from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from ehforwarderbot import MsgType
from ehforwarderbot.types import MessageID
from peewee import SqliteDatabase

from efb_telegram_master import db as db_module
from efb_telegram_master.db import DatabaseManager, MsgLog, database
from efb_telegram_master.msg_type import TGMsgType

_LEGACY_INGESTION_SCAN_SCHEMA = """
CREATE TABLE msglogingestionscan (
    id INTEGER NOT NULL PRIMARY KEY,
    source_chat_id TEXT NOT NULL UNIQUE,
    scan_boundary INTEGER NOT NULL,
    cursor INTEGER NOT NULL,
    existing_streak INTEGER NOT NULL,
    scanned_count INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL,
    existing_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at DATETIME,
    status TEXT NOT NULL,
    error TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""


def test_fresh_database_defines_msglog_provenance(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.fresh", config={}))
    try:
        msglog_columns = {column.name for column in database.get_columns("msglog")}
        tables = set(database.get_tables())
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert "provenance" in msglog_columns
    assert "msglogingestionscan" not in tables


def test_startup_retires_the_exact_legacy_msglog_ingestion_scan_table(tmp_path, monkeypatch):
    original_database = database.obj
    database_path = tmp_path / "tgdata.db"
    legacy_database = SqliteDatabase(database_path)
    legacy_database.connect()
    legacy_database.execute_sql(_LEGACY_INGESTION_SCAN_SCHEMA)
    legacy_database.close()
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)

    manager = DatabaseManager(SimpleNamespace(channel_id="tests.legacy", config={}))
    try:
        tables = set(database.get_tables())
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert "msglogingestionscan" not in tables


def test_startup_keeps_a_same_name_table_with_a_different_schema_signature(tmp_path, monkeypatch):
    original_database = database.obj
    database_path = tmp_path / "tgdata.db"
    collision_database = SqliteDatabase(database_path)
    collision_database.connect()
    collision_database.execute_sql(_LEGACY_INGESTION_SCAN_SCHEMA.replace("id INTEGER NOT NULL PRIMARY KEY", "id TEXT NOT NULL PRIMARY KEY", 1))
    collision_database.close()
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)

    manager = DatabaseManager(SimpleNamespace(channel_id="tests.collision", config={}))
    try:
        tables = set(database.get_tables())
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert "msglogingestionscan" in tables


def test_ingested_msglog_persistence_is_idempotent():
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    manager = object.__new__(DatabaseManager)
    manager.channel = SimpleNamespace(channel_id="tests")
    try:
        test_db.create_tables([MsgLog])
        content = SimpleNamespace(
            text="ingested",
            media_type="Text",
            mime=None,
            msg_type="Text",
            time=datetime(2026, 8, 4),
        )
        assert (
            manager.persist_ingested_msglog(100, 500, "tests.slave target", content)
            == "inserted"
        )
        assert (
            manager.persist_ingested_msglog(100, 500, "tests.slave target", content)
            == "existing"
        )
        row = MsgLog.get_by_id("100.500")
    finally:
        test_db.close()
        database.initialize(original_database)

    assert row.provenance == "mtproto_ingested"
    assert row.slave_message_id == "mtproto-ingested:100.500"


def test_live_message_overwrites_synthetic_provenance():
    test_db = SqliteDatabase(":memory:")
    manager = object.__new__(DatabaseManager)
    manager.logger = Mock()
    message = SimpleNamespace(
        uid=MessageID("live-message"),
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.slave", uid="author"),
        text="live text",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests.master"),
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
