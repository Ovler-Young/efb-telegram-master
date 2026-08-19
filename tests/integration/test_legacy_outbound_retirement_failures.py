from types import SimpleNamespace

import pytest
from peewee import PostgresqlDatabase

from efb_telegram_master.core.db import DatabaseManager
from efb_telegram_master.core.legacy_outbound_retirement import LegacyOutboundRetirement
from efb_telegram_master.persistence import database_initializer
from tests.integration import legacy_outbound_retirement_helpers
from tests.integration.legacy_outbound_retirement_helpers import database_kwargs, drop_database, new_database, temporary_postgresql_database
from tests.support.legacy_outbound_schema import create_legacy_outbound_schema

poll_bot = legacy_outbound_retirement_helpers.poll_bot


@pytest.mark.integration
def test_postgresql_startup_preserves_non_empty_legacy_outbound_tables(integration_postgres_config, tmp_path, monkeypatch):
    admin_db = PostgresqlDatabase(**database_kwargs(integration_postgres_config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    database_name = None
    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        database_name, legacy_db = new_database(admin_db, integration_postgres_config)
        legacy_db.connect()
        workflow, task = create_legacy_outbound_schema(legacy_db)
        workflow.create()
        task.create(
            source_key="source",
            target_chat_id=1,
            operation="send_message",
            payload="durable payload",
            workflow_id=1,
        )
        legacy_db.close()

        config = {"database": {"type": "postgresql", "database": database_name, **{key: value for key, value in database_kwargs(integration_postgres_config).items() if key != "database"}}}
        with pytest.raises(RuntimeError, match="Legacy durable outbound data detected: automatic replay is disabled"):
            DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-non-empty-legacy", config=SimpleNamespace(database=config["database"])))

        preserved_db = PostgresqlDatabase(database_name, **{key: value for key, value in database_kwargs(integration_postgres_config).items() if key != "database"})
        preserved_db.connect()
        try:
            assert set(LegacyOutboundRetirement.TABLES).issubset(preserved_db.get_tables())
            assert preserved_db.execute_sql("SELECT state FROM outboundworkflow").fetchone() == ("active",)
            assert preserved_db.execute_sql("SELECT payload FROM outboundtask").fetchone() == ("durable payload",)
        finally:
            preserved_db.close()
    finally:
        if database_name is not None:
            drop_database(admin_db, database_name)
        admin_db.close()


@pytest.mark.integration
def test_postgresql_retirement_rolls_back_when_workflow_drop_fails(integration_postgres_config, monkeypatch):
    drop_calls = 0
    original_drop_tables = PostgresqlDatabase.drop_tables

    def fail_workflow_drop(instance, models, **kwargs):
        nonlocal drop_calls
        drop_calls += 1
        if drop_calls == 2:
            raise RuntimeError("workflow drop failed")
        return original_drop_tables(instance, models, **kwargs)

    with temporary_postgresql_database(integration_postgres_config) as (_database_name, legacy_db):
        legacy_db.connect()
        workflow, task = create_legacy_outbound_schema(legacy_db)
        monkeypatch.setattr(PostgresqlDatabase, "drop_tables", fail_workflow_drop)
        with pytest.raises(RuntimeError, match="workflow drop failed"):
            LegacyOutboundRetirement(legacy_db).retire_tables()
        assert set(LegacyOutboundRetirement.TABLES).issubset(legacy_db.get_tables())
        assert workflow.select().count() == 0
        assert task.select().count() == 0
        assert LegacyOutboundRetirement.schema_error(legacy_db, "outboundtask") is None
        legacy_db.close()
