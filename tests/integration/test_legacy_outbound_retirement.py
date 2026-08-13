import threading
import uuid
from types import SimpleNamespace

import pytest
from peewee import PostgresqlDatabase, SqliteDatabase

from efb_telegram_master import db as db_module
from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.models import ChatAssoc, HistoryMigrationEntry, MsgLog, SlaveChatInfo, TopicAssoc, database
from tests.support.legacy_outbound_schema import legacy_outbound_models


@pytest.fixture
def poll_bot():
    """Keep database-retirement tests independent of Telegram polling."""


def _database_kwargs(config):
    return {key: value for key, value in config.items() if key != "type"}


def _new_database(admin_db, config):
    database_name = f"etm_legacy_{uuid.uuid4().hex}"
    admin_db.execute_sql(f'CREATE DATABASE "{database_name}"')
    return database_name, PostgresqlDatabase(database_name, **{key: value for key, value in _database_kwargs(config).items() if key != "database"})


def _drop_database(admin_db, database_name):
    admin_db.execute_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
    admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')


@pytest.mark.integration
def test_postgresql_startup_preserves_non_empty_historical_outbound_tables(integration_postgres_config, tmp_path, monkeypatch):
    admin_db = PostgresqlDatabase(**_database_kwargs(integration_postgres_config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    original_database = database.obj
    database_name = None
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        database_name, legacy_db = _new_database(admin_db, integration_postgres_config)
        legacy_db.connect()
        workflow, task = legacy_outbound_models(legacy_db)
        legacy_db.create_tables([workflow, task])
        workflow.create()
        task.create(source_key="source", target_chat_id=1, operation="send_message", payload="secret", workflow_id=1)
        workflow_columns = {column.name: column for column in legacy_db.get_columns("outboundworkflow")}
        task_columns = {column.name: column for column in legacy_db.get_columns("outboundtask")}
        assert (
            DatabaseManager._legacy_default_category(
                legacy_db, "outboundworkflow", "id", DatabaseManager._legacy_column_type(workflow_columns["id"].data_type), workflow_columns["id"].primary_key, workflow_columns["id"].default
            )
            == "auto_pk"
        )
        assert (
            DatabaseManager._legacy_default_category(
                legacy_db, "outboundtask", "id", DatabaseManager._legacy_column_type(task_columns["id"].data_type), task_columns["id"].primary_key, task_columns["id"].default
            )
            == "auto_pk"
        )
        assert (
            DatabaseManager._legacy_default_category(
                legacy_db,
                "outboundworkflow",
                "created_at",
                DatabaseManager._legacy_column_type(workflow_columns["created_at"].data_type),
                workflow_columns["created_at"].primary_key,
                workflow_columns["created_at"].default,
            )
            == "current_timestamp"
        )
        assert (
            DatabaseManager._legacy_default_category(
                legacy_db,
                "outboundtask",
                "accepted_at",
                DatabaseManager._legacy_column_type(task_columns["accepted_at"].data_type),
                task_columns["accepted_at"].primary_key,
                task_columns["accepted_at"].default,
            )
            == "current_timestamp"
        )
        assert tuple((index.name, tuple(index.columns), index.unique) for index in legacy_db.get_indexes("outboundtask")) == (
            ("outboundtask_pkey", ("id",), True),
            ("outboundtask_workflow_id_step_index", ("workflow_id", "step_index"), True),
            ("outboundtask_source_key_priority_accepted_at_id", ("source_key", "priority", "accepted_at", "id"), False),
            ("outboundtask_state_available_at", ("state", "available_at"), False),
            ("outboundtask_workflow_id", ("workflow_id",), False),
        )
        assert DatabaseManager._legacy_outbound_schema_error(legacy_db, "outboundworkflow") is None
        assert DatabaseManager._legacy_outbound_schema_error(legacy_db, "outboundtask") is None
        legacy_db.close()

        config = {"database": {"type": "postgresql", "database": database_name, **{key: value for key, value in _database_kwargs(integration_postgres_config).items() if key != "database"}}}
        with pytest.raises(RuntimeError, match="automatic replay is disabled"):
            DatabaseManager(SimpleNamespace(channel_id="tests.postgresql", config=config))
        assert database.is_closed()
        check_db = PostgresqlDatabase(database_name, **{key: value for key, value in _database_kwargs(integration_postgres_config).items() if key != "database"})
        check_db.connect()
        try:
            assert set(DatabaseManager._LEGACY_OUTBOUND_TABLES).issubset(check_db.get_tables())
            assert check_db.execute_sql("SELECT COUNT(*) FROM outboundworkflow").fetchone()[0] == 1
            assert check_db.execute_sql("SELECT COUNT(*) FROM outboundtask").fetchone()[0] == 1
        finally:
            check_db.close()
    finally:
        database.initialize(original_database)
        if database_name is not None:
            _drop_database(admin_db, database_name)
        admin_db.close()


@pytest.mark.integration
def test_postgresql_startup_imports_populated_sqlite_and_renames_source(integration_postgres_config, tmp_path, monkeypatch):
    admin_db = PostgresqlDatabase(**_database_kwargs(integration_postgres_config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    database_name = None
    manager = None
    original_database = database.obj
    source_path = tmp_path / "tgdata.db"
    source_db = SqliteDatabase(source_path)
    models = (ChatAssoc, TopicAssoc, SlaveChatInfo, MsgLog, HistoryMigrationEntry)
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        source_db.connect()
        with source_db.bind_ctx(models):
            source_db.create_tables(models)
            ChatAssoc.create(master_uid="master", slave_uid="slave")
            TopicAssoc.create(topic_chat_id="10", message_thread_id="20", slave_uid="slave")
            SlaveChatInfo.create(
                slave_channel_id="tests.slave",
                slave_channel_emoji="x",
                slave_chat_uid="slave",
                slave_chat_name="Source chat",
                slave_chat_type="group",
            )
            MsgLog.create(
                master_msg_id="10.1",
                slave_message_id="source-message",
                text="source text",
                slave_origin_uid="slave",
                slave_member_uid="member",
                msg_type="Text",
                sent_to="master",
            )
            HistoryMigrationEntry.create(slave_chat_id="slave", target_chat_id="10", source_master_msg_id="10.1", position=0)
        source_db.close()
        database_name, _target = _new_database(admin_db, integration_postgres_config)
        config = {"database": {"type": "postgresql", "database": database_name, **{key: value for key, value in _database_kwargs(integration_postgres_config).items() if key != "database"}}}
        manager = DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-import", config=config))
        assert ChatAssoc.select().count() == 1
        assert TopicAssoc.select().count() == 1
        assert SlaveChatInfo.select().count() == 1
        assert MsgLog.get_by_id("10.1").text == "source text"
        assert HistoryMigrationEntry.select().count() == 1
        assert not source_path.exists()
        assert source_path.with_suffix(".db.migrated").exists()
    finally:
        if not source_db.is_closed():
            source_db.close()
        if manager is not None:
            manager.stop_worker()
        database.initialize(original_database)
        if database_name is not None:
            _drop_database(admin_db, database_name)
        admin_db.close()



@pytest.mark.integration
def test_postgresql_retirement_advisory_lock_serializes_concurrent_startups(integration_postgres_config):
    admin_db = PostgresqlDatabase(**_database_kwargs(integration_postgres_config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    database_name = None
    first_ready = threading.Event()
    release_first = threading.Event()
    errors = []
    original_validate = DatabaseManager._validate_legacy_outbound_schema
    patch = pytest.MonkeyPatch()

    def wait_after_first_lock(cls, current_database, table_names):
        if not first_ready.is_set():
            first_ready.set()
            assert release_first.wait(10)
        return original_validate(current_database, table_names)

    def retire():
        connection = PostgresqlDatabase(database_name, **{key: value for key, value in _database_kwargs(integration_postgres_config).items() if key != "database"})
        connection.connect()
        try:
            DatabaseManager._retire_legacy_outbound_tables_for_database(connection)
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    try:
        database_name, legacy_db = _new_database(admin_db, integration_postgres_config)
        legacy_db.connect()
        workflow, task = legacy_outbound_models(legacy_db)
        legacy_db.create_tables([workflow, task])
        legacy_db.close()
        patch.setattr(DatabaseManager, "_validate_legacy_outbound_schema", classmethod(wait_after_first_lock))
        first = threading.Thread(target=retire)
        second = threading.Thread(target=retire)
        first.start()
        assert first_ready.wait(10)
        second.start()
        assert second.is_alive()
        release_first.set()
        first.join(10)
        second.join(10)
        assert not first.is_alive() and not second.is_alive()
        assert not errors
        check_db = PostgresqlDatabase(database_name, **{key: value for key, value in _database_kwargs(integration_postgres_config).items() if key != "database"})
        check_db.connect()
        try:
            assert not set(DatabaseManager._LEGACY_OUTBOUND_TABLES) & set(check_db.get_tables())
        finally:
            check_db.close()
    finally:
        patch.undo()
        if database_name is not None:
            _drop_database(admin_db, database_name)
        admin_db.close()


@pytest.mark.integration
def test_postgresql_retirement_rolls_back_when_workflow_drop_fails(integration_postgres_config, monkeypatch):
    admin_db = PostgresqlDatabase(**_database_kwargs(integration_postgres_config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    database_name = None
    drop_calls = 0
    original_drop_tables = PostgresqlDatabase.drop_tables

    def fail_workflow_drop(instance, models, **kwargs):
        nonlocal drop_calls
        drop_calls += 1
        if drop_calls == 2:
            raise RuntimeError("workflow drop failed")
        return original_drop_tables(instance, models, **kwargs)

    try:
        database_name, legacy_db = _new_database(admin_db, integration_postgres_config)
        legacy_db.connect()
        workflow, task = legacy_outbound_models(legacy_db)
        legacy_db.create_tables([workflow, task])
        monkeypatch.setattr(PostgresqlDatabase, "drop_tables", fail_workflow_drop)
        with pytest.raises(RuntimeError, match="workflow drop failed"):
            DatabaseManager._retire_legacy_outbound_tables_for_database(legacy_db)
        assert set(DatabaseManager._LEGACY_OUTBOUND_TABLES).issubset(legacy_db.get_tables())
        assert workflow.select().count() == 0
        assert task.select().count() == 0
        assert DatabaseManager._legacy_outbound_schema_error(legacy_db, "outboundtask") is None
        legacy_db.close()
    finally:
        if database_name is not None:
            _drop_database(admin_db, database_name)
        admin_db.close()
