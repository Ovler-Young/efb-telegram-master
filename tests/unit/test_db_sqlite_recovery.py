from datetime import datetime
from types import SimpleNamespace

from peewee import SqliteDatabase

from efb_telegram_master.core.db import DatabaseManager
from efb_telegram_master.core.models import ChatAssoc, HistoryMigrationEntry, MsgLogIngestionScan, SlaveMessageDelivery, TopicAssoc
from efb_telegram_master.persistence.sqlite_postgresql_import import SQLitePostgresqlImportCoordinator
from tests.support.legacy_outbound_schema import create_legacy_historic_identity_source


def test_sqlite_import_snapshot_canonicalizes_legacy_historic_identities_without_mutating_source(tmp_path):
    source_db = SqliteDatabase(tmp_path / "tgdata.db")
    models = (ChatAssoc, TopicAssoc, HistoryMigrationEntry)
    source_db.connect()
    try:
        create_legacy_historic_identity_source(source_db)
        with source_db.bind_ctx(models):
            snapshot = SQLitePostgresqlImportCoordinator.sqlite_source_snapshot(source_db, models)

        rows_by_model = {projection.model: [dict(zip(projection.column_names, row)) for row in projection.rows] for projection in snapshot.projections}
        assert rows_by_model[ChatAssoc] == [{"id": 2, "master_uid": "master-new", "slave_uid": "slave-a"}]
        assert rows_by_model[TopicAssoc] == [{"id": 3, "topic_chat_id": "101", "message_thread_id": "201", "slave_uid": "slave-b"}]
        assert [row["source_master_msg_id"] for row in rows_by_model[HistoryMigrationEntry]] == ["10.2", "10.4"]
        assert source_db.execute_sql("SELECT COUNT(*) FROM chatassoc").fetchone() == (2,)
        assert source_db.execute_sql("SELECT COUNT(*) FROM topicassoc").fetchone() == (3,)
        assert source_db.execute_sql("SELECT COUNT(*) FROM historymigrationentry").fetchone() == (4,)
    finally:
        source_db.close()


def test_sqlite_import_snapshot_omits_missing_ingestion_rescan_requested_column(tmp_path):
    source_db = SqliteDatabase(tmp_path / "tgdata.db")
    source_db.connect()
    try:
        source_db.execute_sql(
            "CREATE TABLE msglogingestionscan ("
            "id INTEGER PRIMARY KEY, source_chat_id TEXT NOT NULL UNIQUE, scan_boundary INTEGER NOT NULL, cursor INTEGER NOT NULL, "
            "existing_streak INTEGER NOT NULL DEFAULT 0, scanned_count INTEGER NOT NULL DEFAULT 0, inserted_count INTEGER NOT NULL DEFAULT 0, "
            "existing_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires_at DATETIME, "
            "status TEXT NOT NULL DEFAULT 'pending', error TEXT, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        source_db.execute_sql(
            "INSERT INTO msglogingestionscan (source_chat_id, scan_boundary, cursor, existing_streak, scanned_count, inserted_count, "
            "existing_count, skipped_count, lease_owner, status, error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("100", 500, 0, 500, 500, 5, 495, 0, None, "complete", None, "2020-01-02 03:04:05", "2021-02-03 04:05:06"),
        )
        with source_db.bind_ctx([MsgLogIngestionScan]):
            snapshot = SQLitePostgresqlImportCoordinator.sqlite_source_snapshot(source_db, (MsgLogIngestionScan,))
    finally:
        source_db.close()

    projection = snapshot.projections[0]
    row = dict(zip(projection.column_names, projection.rows[0]))
    assert projection.model is MsgLogIngestionScan
    assert set(projection.column_names) == {field.column_name for field in MsgLogIngestionScan._meta.sorted_fields} - {"rescan_requested"}
    assert {key: row[key] for key in row if key not in {"created_at", "updated_at"}} == {
        "id": 1,
        "source_chat_id": "100",
        "scan_boundary": 500,
        "cursor": 0,
        "existing_streak": 500,
        "scanned_count": 500,
        "inserted_count": 5,
        "existing_count": 495,
        "skipped_count": 0,
        "lease_owner": None,
        "lease_expires_at": None,
        "lease_clock": None,
        "status": "complete",
        "error": None,
    }
    assert row["created_at"] == datetime(2020, 1, 2, 3, 4, 5)
    assert row["updated_at"] == datetime(2021, 2, 3, 4, 5, 6)


def test_sqlite_import_snapshot_injects_missing_delivery_lease_clock(tmp_path):
    source_db = SqliteDatabase(tmp_path / "tgdata.db")
    source_db.connect()
    try:
        source_db.execute_sql(
            "CREATE TABLE slavemessagedelivery ("
            "id INTEGER PRIMARY KEY, slave_origin_uid TEXT NOT NULL, slave_message_id TEXT NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'pending', lease_expires_at DATETIME, owner_token TEXT, "
            "UNIQUE(slave_origin_uid, slave_message_id))"
        )
        source_db.execute_sql(
            "INSERT INTO slavemessagedelivery (slave_origin_uid, slave_message_id, state, lease_expires_at, owner_token) VALUES (?, ?, ?, ?, ?)",
            ("tests.slave chat", "message", "pending", "2020-01-02 03:04:05", "owner"),
        )
        with source_db.bind_ctx([SlaveMessageDelivery]):
            snapshot = SQLitePostgresqlImportCoordinator.sqlite_source_snapshot(source_db, (SlaveMessageDelivery,))
    finally:
        source_db.close()

    projection = snapshot.projections[0]
    row = dict(zip(projection.column_names, projection.rows[0]))
    assert projection.model is SlaveMessageDelivery
    assert set(projection.column_names) == {field.column_name for field in SlaveMessageDelivery._meta.sorted_fields}
    assert row == {
        "id": 1,
        "slave_origin_uid": "tests.slave chat",
        "slave_message_id": "message",
        "state": "pending",
        "lease_expires_at": datetime(2020, 1, 2, 3, 4, 5),
        "owner_token": "owner",
        "lease_clock": None,
    }


def test_database_manager_uses_transactional_wal_sqlite(tmp_path, monkeypatch):
    monkeypatch.setattr("efb_telegram_master.persistence.database_initializer.utils.get_data_path", lambda _channel_id: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.sqlite", config=SimpleNamespace(database={})))
    try:
        assert isinstance(manager.current_database, SqliteDatabase)
        assert manager.current_database.pragma("journal_mode").lower() == "wal"
    finally:
        manager.stop_worker()
