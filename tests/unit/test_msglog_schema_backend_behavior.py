import os
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from peewee import PostgresqlDatabase, SqliteDatabase

from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.models import DATABASE_MODELS, MsgLogIngestionScan
from efb_telegram_master.persistence import database_initializer
from efb_telegram_master.persistence.msglog_ingestion_repository import MsgLogIngestionCompletion, MsgLogIngestionRepository
from tests.unit.msglog_schema_support import create_legacy_ingestion_scan_schema, insert_legacy_ingestion_scan_rows, legacy_ingestion_scan_rows, postgres_connection_kwargs


def test_association_rescan_resets_completed_scans_and_marks_active_leases():
    test_db = SqliteDatabase(":memory:")
    model_binding = test_db.bind_ctx(DATABASE_MODELS)
    model_binding.__enter__()
    test_db.connect()
    manager = MsgLogIngestionRepository("tests", test_db)
    try:
        test_db.create_tables([MsgLogIngestionScan])
        scan = manager.get_or_create_scan(100, 500)
        MsgLogIngestionScan.update(cursor=0, existing_streak=500, scanned_count=500, status="complete", lease_owner="stale-worker", lease_expires_at=datetime.now() - timedelta(seconds=1)).where(
            MsgLogIngestionScan.id == scan.id
        ).execute()
        assert manager.request_association_rescan(100) == "pending"
        reset = MsgLogIngestionScan.get_by_id(scan.id)
        assert (reset.status, reset.cursor, reset.existing_streak, reset.scanned_count, reset.lease_owner, reset.lease_expires_at) == ("pending", 500, 0, 0, None, None)
        assert manager.claim_scan(100, "worker-a", 60) is not None
        assert manager.request_association_rescan(100) == "running"
        running = MsgLogIngestionScan.get_by_id(scan.id)
        assert (running.status, running.rescan_requested) == ("running", True)
    finally:
        test_db.close()
        model_binding.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("lease_owner", "lease_expires_at"),
    [("worker-a", datetime.now() - timedelta(seconds=1)), (None, None)],
    ids=["expired", "missing"],
)
def test_association_rescan_recovers_stale_running_leases(lease_owner, lease_expires_at):
    test_db = SqliteDatabase(":memory:")
    model_binding = test_db.bind_ctx(DATABASE_MODELS)
    model_binding.__enter__()
    test_db.connect()
    manager = MsgLogIngestionRepository("tests", test_db)
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
        model_binding.__exit__(None, None, None)

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
    test_db = SqliteDatabase(":memory:")
    model_binding = test_db.bind_ctx(DATABASE_MODELS)
    model_binding.__enter__()
    test_db.connect()
    manager = MsgLogIngestionRepository("tests", test_db)
    try:
        test_db.create_tables([MsgLogIngestionScan])
        manager.get_or_create_scan(100, 500)
        scan = manager.claim_scan(100, "remote-worker", 60)
        assert scan is not None
        assert manager.request_association_rescan(100) == "running"
        assert manager.complete_scan(scan, lease_owner="remote-worker") is MsgLogIngestionCompletion.RESCAN
        restarted = MsgLogIngestionScan.get_by_id(scan.id)
        assert (restarted.status, restarted.cursor, restarted.rescan_requested, restarted.lease_owner) == ("running", 500, False, "remote-worker")
        assert manager.complete_scan(scan, lease_owner="remote-worker") is MsgLogIngestionCompletion.COMPLETE
        assert MsgLogIngestionScan.get_by_id(scan.id).status == "complete"
    finally:
        test_db.close()
        model_binding.__exit__(None, None, None)


def test_expired_scan_is_resumable_after_restart():
    test_db = SqliteDatabase(":memory:")
    model_binding = test_db.bind_ctx(DATABASE_MODELS)
    model_binding.__enter__()
    test_db.connect()
    manager = MsgLogIngestionRepository("tests", test_db)
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
        model_binding.__exit__(None, None, None)


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_HOST"), reason="PostgreSQL test environment is not configured")
def test_postgresql_legacy_ingestion_scan_schema_defaults_rescan_requested_false(tmp_path, monkeypatch):
    connection_kwargs = postgres_connection_kwargs()
    database_name = f"etm_scan_{uuid.uuid4().hex}"
    admin_db = PostgresqlDatabase(**connection_kwargs)
    admin_db.connect()
    admin_db.connection().autocommit = True
    manager = None
    model_binding = None
    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        admin_db.execute_sql(f'CREATE DATABASE "{database_name}"')
        test_db = PostgresqlDatabase(database_name, **{key: value for key, value in connection_kwargs.items() if key != "database"})
        test_db.connect()
        create_legacy_ingestion_scan_schema(test_db, timestamp_type="TIMESTAMP")
        insert_legacy_ingestion_scan_rows(test_db)
        test_db.close()
        config = SimpleNamespace(database={"type": "postgresql", "database": database_name, **{key: value for key, value in connection_kwargs.items() if key != "database"}})
        manager = DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-scan-upgrade", config=config))
        model_binding = manager.current_database.bind_ctx(DATABASE_MODELS)
        model_binding.__enter__()
        scan_columns = {column.name for column in manager.current_database.get_columns("msglogingestionscan")}
        rows = legacy_ingestion_scan_rows(manager.current_database)
        lease_clocks = manager.current_database.execute_sql("SELECT lease_clock FROM msglogingestionscan ORDER BY source_chat_id").fetchall()
    finally:
        if manager is not None:
            manager.stop_worker()
        if model_binding is not None:
            model_binding.__exit__(None, None, None)
        admin_db.execute_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
        admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_db.close()

    assert {"lease_clock", "rescan_requested"}.issubset(scan_columns)
    assert lease_clocks == [(None,), (None,), (None,)]
    assert rows == [
        ("100", 500, 0, 500, 500, 5, 495, 0, None, "complete", None, False),
        ("200", 900, 900, 0, 0, 0, 0, 0, None, "pending", None, False),
        ("300", 1000, 875, 125, 125, 20, 90, 15, "worker-a", "running", "temporary failure", False),
    ]
