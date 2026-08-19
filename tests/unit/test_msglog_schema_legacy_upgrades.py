import pytest
from peewee import SqliteDatabase

from efb_telegram_master import db as db_module
from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.models import MsgLogIngestionScan, database
from tests.unit.msglog_schema_support import create_legacy_ingestion_scan_schema, create_old_msglog_schema, insert_legacy_ingestion_scan_rows, legacy_ingestion_scan_rows


def test_msglog_migration_preserves_legacy_naive_and_null_times():
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    try:
        create_old_msglog_schema(test_db)
        test_db.execute_sql(
            "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, msg_type, sent_to, time) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("100.2", "legacy-naive", "text", "tests.slave chat", "Text", "tests.master", "2020-01-02 03:04:05"),
        )
        test_db.execute_sql(
            "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, msg_type, sent_to, time) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("100.3", "legacy-null", "text", "tests.slave chat", "Text", "tests.master", None),
        )
        DatabaseManager._ensure_historic_schema_columns(test_db)
        rows = test_db.execute_sql("SELECT master_msg_id, time FROM msglog ORDER BY master_msg_id").fetchall()
    finally:
        test_db.close()
        database.initialize(original_database)

    assert rows == [("100.1", None), ("100.2", "2020-01-02 03:04:05"), ("100.3", None)]


def test_legacy_ingestion_scan_schema_adds_rescan_requested_without_losing_states(tmp_path):
    original_database = database.obj
    test_db = SqliteDatabase(tmp_path / "tgdata.db")
    database.initialize(test_db)
    test_db.connect()
    try:
        create_legacy_ingestion_scan_schema(test_db)
        insert_legacy_ingestion_scan_rows(test_db)
        legacy_columns = {column.name for column in test_db.get_columns("msglogingestionscan")}
        DatabaseManager._ensure_historic_schema_columns(test_db)
        scan_columns = {column.name for column in test_db.get_columns("msglogingestionscan")}
        rows = legacy_ingestion_scan_rows(test_db)
        lease_clocks = test_db.execute_sql("SELECT lease_clock FROM msglogingestionscan ORDER BY source_chat_id").fetchall()
    finally:
        test_db.close()
        database.initialize(original_database)

    expected_columns = {field.column_name for field in MsgLogIngestionScan._meta.sorted_fields}
    assert legacy_columns == expected_columns - {"rescan_requested", "lease_clock"}
    assert scan_columns == expected_columns
    assert lease_clocks == [(None,), (None,), (None,)]
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
        create_legacy_ingestion_scan_schema(test_db)
        insert_legacy_ingestion_scan_rows(test_db)
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
