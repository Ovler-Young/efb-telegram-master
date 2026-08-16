import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from ehforwarderbot import MsgType
from ehforwarderbot.types import MessageID
from peewee import IntegrityError, PostgresqlDatabase, SqliteDatabase

from efb_telegram_master import db as db_module
from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.models import ChatAssoc, HistoryMigrationEntry, MsgLog, MsgLogIngestionScan, TopicAssoc, database
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

_LEGACY_INGESTION_SCAN_SCHEMA = """
CREATE TABLE "msglogingestionscan" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "source_chat_id" TEXT NOT NULL UNIQUE,
    "scan_boundary" INTEGER NOT NULL,
    "cursor" INTEGER NOT NULL,
    "existing_streak" INTEGER NOT NULL DEFAULT 0,
    "scanned_count" INTEGER NOT NULL DEFAULT 0,
    "inserted_count" INTEGER NOT NULL DEFAULT 0,
    "existing_count" INTEGER NOT NULL DEFAULT 0,
    "skipped_count" INTEGER NOT NULL DEFAULT 0,
    "lease_owner" TEXT,
    "lease_expires_at" {timestamp_type},
    "status" TEXT NOT NULL DEFAULT 'pending',
    "error" TEXT,
    "created_at" {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _create_old_msglog_schema(test_db, *, blob_type="BLOB", time_type="DATETIME"):
    test_db.execute_sql(_OLD_MSGLOG_SCHEMA.format(blob_type=blob_type, time_type=time_type))
    placeholders = ", ".join([test_db.param] * 6)
    test_db.execute_sql(
        f"INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, msg_type, sent_to) VALUES ({placeholders})",
        ("100.1", "old-message", "old text", "tests.slave chat", "Text", "tests.master"),
    )


def _create_legacy_ingestion_scan_schema(test_db, *, timestamp_type="DATETIME"):
    test_db.execute_sql(_LEGACY_INGESTION_SCAN_SCHEMA.format(timestamp_type=timestamp_type))


def _insert_legacy_ingestion_scan_rows(test_db):
    columns = (
        "source_chat_id",
        "scan_boundary",
        "cursor",
        "existing_streak",
        "scanned_count",
        "inserted_count",
        "existing_count",
        "skipped_count",
        "lease_owner",
        "status",
        "error",
    )
    placeholders = ", ".join([test_db.param] * len(columns))
    test_db.execute_sql(
        f"INSERT INTO msglogingestionscan ({', '.join(columns)}) VALUES ({placeholders})",
        ("100", 500, 0, 500, 500, 5, 495, 0, None, "complete", None),
    )
    test_db.execute_sql(
        f"INSERT INTO msglogingestionscan ({', '.join(columns)}) VALUES ({placeholders})",
        ("200", 900, 900, 0, 0, 0, 0, 0, None, "pending", None),
    )
    test_db.execute_sql(
        f"INSERT INTO msglogingestionscan ({', '.join(columns)}) VALUES ({placeholders})",
        ("300", 1000, 875, 125, 125, 20, 90, 15, "worker-a", "running", "temporary failure"),
    )


def _legacy_ingestion_scan_rows(test_db):
    return test_db.execute_sql(
        "SELECT source_chat_id, scan_boundary, cursor, existing_streak, scanned_count, inserted_count, "
        "existing_count, skipped_count, lease_owner, status, error, rescan_requested "
        "FROM msglogingestionscan ORDER BY source_chat_id"
    ).fetchall()


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
    assert {"source_chat_id", "cursor", "lease_owner", "existing_streak", "rescan_requested"}.issubset(scan_columns)


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
        msglog_indexes = {index.name for index in database.get_indexes("msglog")}
        query_plan = database.execute_sql(
            "EXPLAIN QUERY PLAN SELECT master_msg_id FROM msglog WHERE slave_origin_uid = ? ORDER BY time ASC, master_msg_id ASC LIMIT 2",
            ("tests.slave chat",),
        ).fetchall()
    finally:
        if second_manager is not None:
            second_manager.stop_worker()
        database.initialize(original_database)

    assert provenance_columns == ["provenance"]
    assert old_row.provenance == "live"
    assert live_row.provenance == "live"
    assert ingested_row.provenance == "mtproto_ingested"
    assert "msglog_slave_origin_uid_time_master_msg_id" in msglog_indexes
    assert any("msglog_slave_origin_uid_time_master_msg_id" in detail for *_ignored, detail in query_plan)


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


def test_legacy_ingestion_scan_schema_adds_rescan_requested_without_losing_states(tmp_path):
    original_database = database.obj
    test_db = SqliteDatabase(tmp_path / "tgdata.db")
    database.initialize(test_db)
    test_db.connect()
    try:
        _create_legacy_ingestion_scan_schema(test_db)
        _insert_legacy_ingestion_scan_rows(test_db)
        legacy_columns = {column.name for column in test_db.get_columns("msglogingestionscan")}

        DatabaseManager._ensure_historic_schema_columns(test_db)

        scan_columns = {column.name for column in test_db.get_columns("msglogingestionscan")}
        rows = _legacy_ingestion_scan_rows(test_db)
    finally:
        test_db.close()
        database.initialize(original_database)

    expected_columns = {field.column_name for field in MsgLogIngestionScan._meta.sorted_fields}
    assert legacy_columns == expected_columns - {"rescan_requested"}
    assert scan_columns == expected_columns
    assert rows == [
        ("100", 500, 0, 500, 500, 5, 495, 0, None, "complete", None, 0),
        ("200", 900, 900, 0, 0, 0, 0, 0, None, "pending", None, 0),
        ("300", 1000, 875, 125, 125, 20, 90, 15, "worker-a", "running", "temporary failure", 0),
    ]


def test_sqlite_ingestion_scan_migration_failure_rolls_back_schema_and_data(tmp_path, monkeypatch):
    original_database = database.obj
    test_db = SqliteDatabase(tmp_path / "tgdata.db")
    database.initialize(test_db)
    test_db.connect()
    original_migrate = db_module.migrate
    migration_calls = 0

    def fail_after_scan_migration(*operations):
        nonlocal migration_calls
        migration_calls += 1
        if migration_calls == 2:
            raise RuntimeError("forced migration failure")
        return original_migrate(*operations)

    try:
        _create_legacy_ingestion_scan_schema(test_db)
        _insert_legacy_ingestion_scan_rows(test_db)
        test_db.execute_sql("CREATE TABLE msglog (master_msg_id TEXT PRIMARY KEY)")
        test_db.execute_sql("INSERT INTO msglog VALUES ('100.1')")
        monkeypatch.setattr(db_module, "migrate", fail_after_scan_migration)

        with pytest.raises(RuntimeError, match="forced migration failure"):
            DatabaseManager._ensure_historic_schema_columns(test_db)

        scan_columns = {column.name for column in test_db.get_columns("msglogingestionscan")}
        msglog_columns = {column.name for column in test_db.get_columns("msglog")}
        scan_rows = test_db.execute_sql(
            "SELECT source_chat_id, scan_boundary, cursor, existing_streak, scanned_count, inserted_count, "
            "existing_count, skipped_count, lease_owner, status, error FROM msglogingestionscan ORDER BY source_chat_id"
        ).fetchall()
        msglog_rows = test_db.execute_sql("SELECT master_msg_id FROM msglog").fetchall()
    finally:
        test_db.close()
        database.initialize(original_database)

    assert migration_calls == 2
    assert "rescan_requested" not in scan_columns
    assert msglog_columns == {"master_msg_id"}
    assert scan_rows == [
        ("100", 500, 0, 500, 500, 5, 495, 0, None, "complete", None),
        ("200", 900, 900, 0, 0, 0, 0, 0, None, "pending", None),
        ("300", 1000, 875, 125, 125, 20, 90, 15, "worker-a", "running", "temporary failure"),
    ]
    assert msglog_rows == [("100.1",)]


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


def test_association_rescan_resets_completed_scans_and_marks_active_leases():
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    manager = MsgLogIngestionRepository("tests")
    try:
        test_db.create_tables([MsgLogIngestionScan])
        scan = manager.get_or_create_scan(100, 500)
        MsgLogIngestionScan.update(cursor=0, existing_streak=500, scanned_count=500, status="complete").where(MsgLogIngestionScan.id == scan.id).execute()

        assert manager.request_association_rescan(100) == "pending"
        reset = MsgLogIngestionScan.get_by_id(scan.id)
        assert (reset.status, reset.cursor, reset.existing_streak, reset.scanned_count) == ("pending", 500, 0, 0)

        assert manager.claim_scan(100, "worker-a", 60) is not None
        assert manager.request_association_rescan(100) == "running"
        running = MsgLogIngestionScan.get_by_id(scan.id)
        assert (running.status, running.rescan_requested) == ("running", True)
    finally:
        test_db.close()
        database.initialize(original_database)


@pytest.mark.parametrize(
    ("lease_owner", "lease_expires_at"),
    [("worker-a", datetime.now() - timedelta(seconds=1)), (None, None)],
    ids=["expired", "missing"],
)
def test_association_rescan_recovers_stale_running_leases(lease_owner, lease_expires_at):
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    manager = MsgLogIngestionRepository("tests")
    try:
        test_db.create_tables([MsgLogIngestionScan])
        scan = manager.get_or_create_scan(100, 500)
        MsgLogIngestionScan.update(
            status="running",
            cursor=1,
            existing_streak=499,
            scanned_count=499,
            inserted_count=20,
            existing_count=30,
            skipped_count=40,
            rescan_requested=True,
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
        ).where(MsgLogIngestionScan.id == scan.id).execute()

        assert manager.request_association_rescan(100) == "pending"
        recovered = MsgLogIngestionScan.get_by_id(scan.id)
    finally:
        test_db.close()
        database.initialize(original_database)

    assert (
        recovered.status,
        recovered.cursor,
        recovered.existing_streak,
        recovered.scanned_count,
        recovered.inserted_count,
        recovered.existing_count,
        recovered.skipped_count,
        recovered.rescan_requested,
        recovered.lease_owner,
        recovered.lease_expires_at,
    ) == ("pending", 500, 0, 0, 0, 0, 0, False, None, None)


def test_active_association_request_restarts_scan_without_releasing_its_lease():
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    manager = MsgLogIngestionRepository("tests")
    try:
        test_db.create_tables([MsgLogIngestionScan])
        manager.get_or_create_scan(100, 500)
        scan = manager.claim_scan(100, "remote-worker", 60)

        assert scan is not None
        assert manager.request_association_rescan(100) == "running"
        assert manager.complete_scan(scan, lease_owner="remote-worker") is True
        restarted = MsgLogIngestionScan.get_by_id(scan.id)
        assert (restarted.status, restarted.cursor, restarted.rescan_requested, restarted.lease_owner) == ("running", 500, False, "remote-worker")
        assert manager.complete_scan(scan, lease_owner="remote-worker") is False
        assert MsgLogIngestionScan.get_by_id(scan.id).status == "complete"
    finally:
        test_db.close()
        database.initialize(original_database)


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


def test_retryable_scan_is_resumable_for_its_source_group():
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    manager = MsgLogIngestionRepository("tests")
    try:
        test_db.create_tables([MsgLogIngestionScan])
        scan = manager.get_or_create_scan(100, 500)
        MsgLogIngestionScan.update(status="retryable-error").where(MsgLogIngestionScan.id == scan.id).execute()

        assert manager.get_resumable_scan(100).id == scan.id
        assert manager.get_resumable_scan(200) is None
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


def test_live_message_upsert_wins_when_ingestion_inserts_after_lookup(monkeypatch):
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
    original_get_or_none = MsgLog.get_or_none

    def insert_ingested_after_lookup(*_args, **_kwargs):
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
        return None

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        monkeypatch.setattr(MsgLog, "get_or_none", insert_ingested_after_lookup)
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=1))
        monkeypatch.setattr(MsgLog, "get_or_none", original_get_or_none)
        row = MsgLog.get_by_id("100.1")

    assert (row.provenance, row.slave_message_id, row.text) == ("live", "live-message", "live text")


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_HOST"), reason="PostgreSQL test environment is not configured")
def test_postgresql_legacy_ingestion_scan_schema_defaults_rescan_requested_false(tmp_path, monkeypatch):
    connection_kwargs = {
        "database": os.environ["TEST_POSTGRES_DB"],
        "host": os.environ["TEST_POSTGRES_HOST"],
        "port": int(os.environ["TEST_POSTGRES_PORT"]),
        "user": os.environ["TEST_POSTGRES_USER"],
        "password": os.environ["TEST_POSTGRES_PASSWORD"],
    }
    database_name = f"etm_scan_{uuid.uuid4().hex}"
    admin_db = PostgresqlDatabase(**connection_kwargs)
    admin_db.connect()
    admin_db.connection().autocommit = True
    original_database = database.obj
    manager = None
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        admin_db.execute_sql(f'CREATE DATABASE "{database_name}"')
        test_db = PostgresqlDatabase(database_name, **{key: value for key, value in connection_kwargs.items() if key != "database"})
        test_db.connect()
        _create_legacy_ingestion_scan_schema(test_db, timestamp_type="TIMESTAMP")
        _insert_legacy_ingestion_scan_rows(test_db)
        test_db.close()

        config = {"database": {"type": "postgresql", "database": database_name, **{key: value for key, value in connection_kwargs.items() if key != "database"}}}
        manager = DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-scan-upgrade", config=config))
        scan_columns = {column.name for column in database.get_columns("msglogingestionscan")}
        rows = _legacy_ingestion_scan_rows(database)
    finally:
        if manager is not None:
            manager.stop_worker()
        database.initialize(original_database)
        admin_db.execute_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
        admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_db.close()

    assert "rescan_requested" in scan_columns
    assert rows == [
        ("100", 500, 0, 500, 500, 5, 495, 0, None, "complete", None, False),
        ("200", 900, 900, 0, 0, 0, 0, 0, None, "pending", None, False),
        ("300", 1000, 875, 125, 125, 20, 90, 15, "worker-a", "running", "temporary failure", False),
    ]


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
        database.initialize(original_database)
        admin_db.execute_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
        admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_db.close()

    assert provenance_columns == ["provenance"]
    assert row.provenance == "live"
