from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from ehforwarderbot import MsgType
from ehforwarderbot.types import MessageID
from peewee import SqliteDatabase

from efb_telegram_master import db as db_module
from efb_telegram_master.db import DatabaseManager, MsgLog, MsgLogIngestionScan, database
from efb_telegram_master.msg_type import TGMsgType


def test_fresh_database_defines_msglog_provenance_and_ingestion_scan(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.fresh", config={}))
    try:
        msglog_columns = {column.name for column in database.get_columns("msglog")}
        scan_columns = {column.name for column in database.get_columns("msglogingestionscan")}
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert "provenance" in msglog_columns
    assert {"source_chat_id", "cursor", "lease_owner", "existing_streak"}.issubset(scan_columns)


def test_ingestion_claim_persist_and_idempotence_are_atomic():
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
        content = SimpleNamespace(
            text="ingested", media_type="Text", mime=None, msg_type="Text", time=datetime(2026, 8, 4),
        )
        assert manager.persist_msglog_ingestion_item(
            scan, source_message_id=500, classification="eligible", slave_uid="tests.slave target",
            message=content, lease_owner="worker-a",
        ) == "inserted"
        assert manager.persist_msglog_ingestion_item(
            scan, source_message_id=500, classification="eligible", slave_uid="tests.slave target",
            message=content, lease_owner="worker-a",
        ) == "existing"
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
    manager = object.__new__(DatabaseManager)
    try:
        test_db.create_tables([MsgLogIngestionScan])
        scan = manager.get_or_create_msglog_ingestion_scan(100, 500)
        assert manager.claim_msglog_ingestion_scan(100, "worker-a", 60) is not None
        MsgLogIngestionScan.update(lease_expires_at=datetime.now() - timedelta(seconds=1)).where(
            MsgLogIngestionScan.id == scan.id
        ).execute()
        resumed = manager.get_resumable_msglog_ingestion_scans()
        assert [item.source_chat_id for item in resumed] == ["100"]
        assert manager.claim_msglog_ingestion_scan(100, "worker-b", 60) is not None
    finally:
        test_db.close()
        database.initialize(original_database)


def test_live_message_overwrites_synthetic_provenance():
    test_db = SqliteDatabase(":memory:")
    manager = object.__new__(DatabaseManager)
    manager.logger = Mock()
    message = SimpleNamespace(
        uid=MessageID("live-message"),
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.slave", uid="author"),
        text="live text", type=MsgType.Text, type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests.master"), file_id=None, file_unique_id=None,
        mime=None, is_system=False, attributes=None, commands=None, substitutions=None,
        target=None, sender_bot_id=None, reactions={},
    )
    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        MsgLog.create(
            master_msg_id="100.1", slave_message_id="mtproto-ingested:100.1", text="ingested",
            slave_origin_uid="tests.slave stale", slave_member_uid="tests.slave __self__",
            msg_type="Text", sent_to="tests.master", provenance="mtproto_ingested",
        )
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=1))
        row = MsgLog.get_by_id("100.1")

    assert (row.provenance, row.slave_message_id, row.text) == ("live", "live-message", "live text")
