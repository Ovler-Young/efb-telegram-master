from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from peewee import SqliteDatabase

from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.persistence.database_initializer import DatabaseInitializer


def test_database_manager_exposes_repositories_after_initializer_prepares_database(monkeypatch, tmp_path):
    prepared_database = SqliteDatabase(tmp_path / "tgdata.db")
    initializer = Mock()
    initializer.initialize.return_value = (prepared_database, Path(tmp_path))
    monkeypatch.setattr("efb_telegram_master.db.DatabaseInitializer", lambda *_args: initializer)

    manager = DatabaseManager(SimpleNamespace(channel_id="tests.database", config={}))

    assert manager.current_database is prepared_database
    assert manager.chat_associations.database is prepared_database
    assert manager.msglogs.database is prepared_database
    assert manager._base_path == tmp_path


def test_initializer_closes_connected_database_when_schema_preparation_fails(monkeypatch, tmp_path):
    database = Mock()
    database.connect.return_value = True
    logger = Mock()
    initializer = DatabaseInitializer(SimpleNamespace(channel_id="tests.database", config=SimpleNamespace(database={})), logger)
    monkeypatch.setattr(initializer, "_build_database", lambda _path: database)
    monkeypatch.setattr("efb_telegram_master.persistence.database_initializer.DatabaseSchemaMigrator.create", lambda _self: (_ for _ in ()).throw(RuntimeError("schema failed")))
    monkeypatch.setattr("efb_telegram_master.persistence.database_initializer.utils.get_data_path", lambda _channel_id: tmp_path)

    with pytest.raises(RuntimeError, match="schema failed"):
        initializer.initialize()

    database.close.assert_called_once_with()
