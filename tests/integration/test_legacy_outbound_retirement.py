import threading
from types import SimpleNamespace

import pytest
from peewee import PostgresqlDatabase, SqliteDatabase

from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.legacy_outbound_retirement import LegacyOutboundRetirement
from efb_telegram_master.models import DATABASE_MODELS, ChatAssoc, HistoryMigrationEntry, MsgLog, MsgLogIngestionScan, SlaveChatInfo, SlaveMessageDelivery, TopicAssoc
from efb_telegram_master.persistence import database_initializer
from efb_telegram_master.persistence import sqlite_postgresql_import as sqlite_import_module
from efb_telegram_master.persistence.schema_migration import DatabaseSchemaMigrator
from efb_telegram_master.persistence.sqlite_postgresql_import import SQLitePostgresqlImportCoordinator
from tests.integration import legacy_outbound_retirement_helpers
from tests.integration.legacy_outbound_retirement_helpers import database_kwargs, drop_database, new_database
from tests.support.legacy_outbound_schema import create_legacy_historic_identity_source, create_legacy_outbound_schema

poll_bot = legacy_outbound_retirement_helpers.poll_bot


@pytest.mark.integration
def test_postgresql_retirement_drops_frozen_historical_schema(integration_postgres_config, tmp_path, monkeypatch):
    admin_db = PostgresqlDatabase(**database_kwargs(integration_postgres_config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    first_manager = None
    second_manager = None
    database_name = None
    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        database_name, legacy_db = new_database(admin_db, integration_postgres_config)
        legacy_db.connect()
        create_legacy_outbound_schema(legacy_db)
        legacy_db.close()

        config = {"database": {"type": "postgresql", "database": database_name, **{key: value for key, value in database_kwargs(integration_postgres_config).items() if key != "database"}}}
        first_manager = DatabaseManager(SimpleNamespace(channel_id="tests.postgresql", config=SimpleNamespace(database=config["database"])))
        table_names = set(first_manager.current_database.get_tables())
        assert not set(LegacyOutboundRetirement.TABLES) & table_names
        assert {"chatassoc", "msglog", "historymigrationentry", "msglogingestionscan"}.issubset(table_names)
        first_manager.stop_worker()
        second_manager = DatabaseManager(SimpleNamespace(channel_id="tests.postgresql", config=SimpleNamespace(database=config["database"])))
    finally:
        if second_manager is not None:
            second_manager.stop_worker()
        elif first_manager is not None:
            first_manager.stop_worker()
        if database_name is not None:
            drop_database(admin_db, database_name)
        admin_db.close()


@pytest.mark.integration
def test_postgresql_startup_preserves_sqlite_snapshot_content_provenance_and_archive(integration_postgres_config, tmp_path, monkeypatch):
    admin_db = PostgresqlDatabase(**database_kwargs(integration_postgres_config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    database_name = None
    manager = None
    source_path = tmp_path / "tgdata.db"
    migrated_path = source_path.with_suffix(".db.migrated")
    source_db = SqliteDatabase(source_path, pragmas={"journal_mode": "wal"})
    models = (ChatAssoc, TopicAssoc, SlaveChatInfo, MsgLog, HistoryMigrationEntry, MsgLogIngestionScan, SlaveMessageDelivery)
    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        source_db.connect()
        with source_db.bind_ctx(models):
            source_db.create_tables(models)
            ChatAssoc.create(id=101, master_uid="master", slave_uid="slave")
            TopicAssoc.create(id=102, topic_chat_id="10", message_thread_id="20", slave_uid="slave")
            SlaveChatInfo.create(
                id=103,
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
            HistoryMigrationEntry.create(id=104, slave_chat_id="slave", target_chat_id="10", source_master_msg_id="10.1", position=0)
            MsgLogIngestionScan.create(id=105, source_chat_id="10", scan_boundary=100, cursor=100)
            SlaveMessageDelivery.create(id=106, slave_origin_uid="slave", slave_message_id="source-message")
        database_name, _target = new_database(admin_db, integration_postgres_config)
        config = {"database": {"type": "postgresql", "database": database_name, **{key: value for key, value in database_kwargs(integration_postgres_config).items() if key != "database"}}}
        assert source_path.with_name("tgdata.db-wal").exists()
        migrated_path.write_bytes(b"operator archive")
        with pytest.raises(RuntimeError, match="both tgdata.db and tgdata.db.migrated exist"):
            DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-import", config=SimpleNamespace(database=config["database"])))
        assert migrated_path.read_bytes() == b"operator archive"
        migrated_path.unlink()
        original_link = sqlite_import_module.os.link
        monkeypatch.setattr(sqlite_import_module.os, "link", lambda *_args: (_ for _ in ()).throw(OSError("finalization interrupted")))
        with pytest.raises(RuntimeError, match="finalization failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-import", config=SimpleNamespace(database=config["database"])))
        assert source_path.exists()
        assert not source_path.with_suffix(".db.migrated").exists()
        monkeypatch.setattr(sqlite_import_module.os, "link", original_link)
        manager = DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-import", config=SimpleNamespace(database=config["database"])))
        with manager.current_database.bind_ctx(DATABASE_MODELS):
            assert ChatAssoc.select().count() == 1
            assert TopicAssoc.select().count() == 1
            assert SlaveChatInfo.select().count() == 1
            assert MsgLog.get_by_id("10.1").text == "source text"
            assert HistoryMigrationEntry.select().count() == 1
            assert MsgLogIngestionScan.select().count() == 1
            assert SlaveMessageDelivery.select().count() == 1
            assert ChatAssoc.create(master_uid="master-next", slave_uid="slave-next").id == 102
            assert TopicAssoc.create(topic_chat_id="11", message_thread_id="21", slave_uid="slave-next").id == 103
            assert (
                SlaveChatInfo.create(
                    slave_channel_id="tests.slave",
                    slave_channel_emoji="x",
                    slave_chat_uid="slave-next",
                    slave_chat_name="Imported chat",
                    slave_chat_type="group",
                ).id
                == 104
            )
            assert HistoryMigrationEntry.create(slave_chat_id="slave-next", target_chat_id="11", source_master_msg_id="11.1", position=0).id == 105
            assert MsgLogIngestionScan.create(source_chat_id="11", scan_boundary=101, cursor=101).id == 106
            assert SlaveMessageDelivery.create(slave_origin_uid="slave-next", slave_message_id="next-message").id == 107
            provenance = manager.current_database.execute_sql('SELECT snapshot_identity FROM "sqliteimportprovenance"').fetchone()
            assert provenance is not None and len(provenance[0]) == 64
        assert not source_path.exists()
        archive = SqliteDatabase(migrated_path)
        archive.connect()
        try:
            assert archive.execute_sql("SELECT text FROM msglog WHERE master_msg_id = '10.1'").fetchone() == ("source text",)
        finally:
            archive.close()
    finally:
        if not source_db.is_closed():
            source_db.close()
        if manager is not None:
            manager.stop_worker()
        if database_name is not None:
            drop_database(admin_db, database_name)
        admin_db.close()

    assert not source_path.with_name("tgdata.db-wal").exists()


@pytest.mark.integration
def test_postgresql_import_canonicalizes_legacy_historic_identities(integration_postgres_config, tmp_path, monkeypatch):
    admin_db = PostgresqlDatabase(**database_kwargs(integration_postgres_config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    database_name = None
    manager = None
    source_path = tmp_path / "tgdata.db"
    migrated_path = source_path.with_suffix(".db.migrated")
    source_db = SqliteDatabase(source_path)
    models = (ChatAssoc, TopicAssoc, SlaveChatInfo, MsgLog, HistoryMigrationEntry, MsgLogIngestionScan, SlaveMessageDelivery)
    monkeypatch.setattr(database_initializer.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        source_db.connect()
        create_legacy_historic_identity_source(source_db)
        database_name, _target = new_database(admin_db, integration_postgres_config)
        config = {"database": {"type": "postgresql", "database": database_name, **{key: value for key, value in database_kwargs(integration_postgres_config).items() if key != "database"}}}

        manager = DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-import-canonical", config=SimpleNamespace(database=config["database"])))

        with manager.current_database.bind_ctx(DATABASE_MODELS):
            assert [(row.master_uid, row.slave_uid) for row in ChatAssoc.select()] == [("master-new", "slave-a")]
            assert [(row.topic_chat_id, row.message_thread_id, row.slave_uid) for row in TopicAssoc.select()] == [("101", "201", "slave-b")]
            assert [row.source_master_msg_id for row in HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.id)] == ["10.2", "10.4"]
            assert {
                DatabaseSchemaMigrator.CHAT_ASSOC_SLAVE_INDEX,
                DatabaseSchemaMigrator.TOPIC_ASSOC_SLAVE_INDEX,
                DatabaseSchemaMigrator.TOPIC_ASSOC_TOPIC_THREAD_INDEX,
                DatabaseSchemaMigrator.HISTORY_TARGET_POSITION_WITHOUT_THREAD_INDEX,
                DatabaseSchemaMigrator.HISTORY_TARGET_POSITION_WITH_THREAD_INDEX,
            }.issubset(
                {index.name for index in manager.current_database.get_indexes("chatassoc")}
                | {index.name for index in manager.current_database.get_indexes("topicassoc")}
                | {index.name for index in manager.current_database.get_indexes("historymigrationentry")}
            )
            provenance = manager.current_database.execute_sql('SELECT snapshot_identity FROM "sqliteimportprovenance"').fetchone()
            assert provenance is not None
        assert not source_path.exists()
        archive = SqliteDatabase(migrated_path)
        archive.connect()
        try:
            assert archive.execute_sql("SELECT COUNT(*) FROM chatassoc").fetchone() == (2,)
            assert archive.execute_sql("SELECT COUNT(*) FROM topicassoc").fetchone() == (3,)
            assert archive.execute_sql("SELECT COUNT(*) FROM historymigrationentry").fetchone() == (4,)
            with archive.bind_ctx(models):
                snapshot = SQLitePostgresqlImportCoordinator.sqlite_source_snapshot(archive, models)
            assert provenance == (snapshot.identity,)
        finally:
            archive.close()
    finally:
        if not source_db.is_closed():
            source_db.close()
        if manager is not None:
            manager.stop_worker()
        if database_name is not None:
            drop_database(admin_db, database_name)
        admin_db.close()


@pytest.mark.integration
def test_postgresql_retirement_advisory_lock_serializes_concurrent_startups(integration_postgres_config):
    admin_db = PostgresqlDatabase(**database_kwargs(integration_postgres_config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    database_name = None
    first_ready = threading.Event()
    release_first = threading.Event()
    errors = []
    original_validate = LegacyOutboundRetirement.validate_schema
    patch = pytest.MonkeyPatch()

    def wait_after_first_lock(cls, current_database, table_names):
        if not first_ready.is_set():
            first_ready.set()
            assert release_first.wait(10)
        return original_validate(current_database, table_names)

    def retire():
        connection = PostgresqlDatabase(database_name, **{key: value for key, value in database_kwargs(integration_postgres_config).items() if key != "database"})
        connection.connect()
        try:
            LegacyOutboundRetirement(connection).retire_tables()
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    try:
        database_name, legacy_db = new_database(admin_db, integration_postgres_config)
        legacy_db.connect()
        workflow, task = create_legacy_outbound_schema(legacy_db)
        legacy_db.close()
        patch.setattr(LegacyOutboundRetirement, "validate_schema", classmethod(wait_after_first_lock))
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
        check_db = PostgresqlDatabase(database_name, **{key: value for key, value in database_kwargs(integration_postgres_config).items() if key != "database"})
        check_db.connect()
        try:
            assert not set(LegacyOutboundRetirement.TABLES) & set(check_db.get_tables())
        finally:
            check_db.close()
    finally:
        patch.undo()
        if database_name is not None:
            drop_database(admin_db, database_name)
        admin_db.close()
