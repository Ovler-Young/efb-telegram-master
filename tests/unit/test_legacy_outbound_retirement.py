from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from peewee import Model, SqliteDatabase

from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.legacy_outbound_retirement import LegacyOutboundRetirement
from efb_telegram_master.persistence import database_initializer
from efb_telegram_master.persistence.schema_migration import DatabaseSchemaMigrator
from tests.support.legacy_outbound_schema import create_legacy_outbound_schema


def test_database_manager_stops_and_closes_postgresql_pool_when_retirement_fails(monkeypatch):
    pool = Mock()
    pooled_database = patch("playhouse.postgres_ext.PooledPostgresqlExtDatabase", return_value=pool)
    pooled_database.start()
    pool.connect.return_value = True
    pool.is_closed.return_value = False
    monkeypatch.setattr(DatabaseSchemaMigrator, "create", lambda _self: None)
    monkeypatch.setattr(LegacyOutboundRetirement, "retire_tables", lambda _self: (_ for _ in ()).throw(RuntimeError("retirement failed")))
    try:
        with pytest.raises(RuntimeError, match="retirement failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-failure", config=SimpleNamespace(database={"type": "postgresql"})))

        pool.connect.assert_called_once_with()
        pool.stop.assert_called_once_with()
        pool.close.assert_called_once_with()
    finally:
        pooled_database.stop()


def test_startup_retires_empty_legacy_outbound_tables_idempotently(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        create_legacy_outbound_schema(raw_db)
    finally:
        raw_db.close()

    manager = None
    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        manager = DatabaseManager(SimpleNamespace(channel_id="tests.legacy", config=SimpleNamespace(database={})))
        table_names = set(manager.current_database.get_tables())
        assert not set(LegacyOutboundRetirement.TABLES) & table_names
        assert {"chatassoc", "msglog", "historymigrationentry", "msglogingestionscan"}.issubset(table_names)
        LegacyOutboundRetirement(manager.current_database).retire_tables()
    finally:
        if manager is not None:
            manager.stop_worker()


def test_legacy_outbound_retirement_directly_retires_empty_schema_idempotently(tmp_path):
    raw_db = SqliteDatabase(tmp_path / "tgdata.db")
    raw_db.connect()
    try:
        create_legacy_outbound_schema(raw_db)

        LegacyOutboundRetirement(raw_db).retire_tables()
        LegacyOutboundRetirement(raw_db).retire_tables()

        assert not set(LegacyOutboundRetirement.TABLES) & set(raw_db.get_tables())
    finally:
        raw_db.close()


def test_startup_aborts_when_legacy_outbound_table_retirement_fails(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        create_legacy_outbound_schema(raw_db)
    finally:
        raw_db.close()

    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)

    def legacy_table_model(_table_name, current_database):
        class LegacyOutboundTable(Model):
            class Meta:
                database = current_database
                table_name = "outboundworkflow"

        return LegacyOutboundTable

    monkeypatch.setattr(LegacyOutboundRetirement, "_table_model", staticmethod(legacy_table_model))
    monkeypatch.setattr(SqliteDatabase, "drop_tables", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("drop failed")))

    try:
        with pytest.raises(RuntimeError, match="drop failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.legacy-drop", config=SimpleNamespace(database={})))
    finally:
        pass


def test_sqlite_legacy_retirement_rolls_back_when_workflow_drop_fails(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        create_legacy_outbound_schema(raw_db)
    finally:
        raw_db.close()

    original_drop_tables = SqliteDatabase.drop_tables
    dropped_tables = []

    def fail_workflow_drop(instance, models, **kwargs):
        dropped_tables.append(models[0]._meta.table_name)
        if len(dropped_tables) == 2:
            raise RuntimeError("workflow drop failed")
        return original_drop_tables(instance, models, **kwargs)

    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(SqliteDatabase, "drop_tables", fail_workflow_drop)
    try:
        with pytest.raises(RuntimeError, match="workflow drop failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.rollback", config=SimpleNamespace(database={})))
        assert dropped_tables == ["outboundtask", "outboundworkflow"]
        restored_db = SqliteDatabase(database_path)
        restored_db.connect()
        try:
            assert set(LegacyOutboundRetirement.TABLES).issubset(restored_db.get_tables())
            assert restored_db.execute_sql("SELECT COUNT(*) FROM outboundworkflow").fetchone()[0] == 0
            assert restored_db.execute_sql("SELECT COUNT(*) FROM outboundtask").fetchone()[0] == 0
            assert LegacyOutboundRetirement.schema_error(restored_db, "outboundtask") is None
        finally:
            restored_db.close()
    finally:
        pass
