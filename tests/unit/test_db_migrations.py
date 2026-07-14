import os
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ehforwarderbot import Message, MsgType
from ehforwarderbot.types import MessageID

from efb_telegram_master import db as db_module
from efb_telegram_master import utils
from efb_telegram_master.db import (
    ChatAssoc,
    DatabaseManager,
    HistoryMigrationEntry,
    MsgLog,
    OutboundTask,
    OutboundWorkflow,
    TopicAssoc,
    database,
)
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID, TelegramTopicID


def test_msglog_schema_has_sender_bot_id(channel):
    columns = {column.name for column in channel.db.database.get_columns("msglog")} if hasattr(channel.db, "database") else None
    if columns is None:
        from efb_telegram_master.db import database
        columns = {column.name for column in database.get_columns("msglog")}
    assert "sender_bot_id" in columns


def test_history_migration_entry_table_exists():
    from peewee import SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    with test_db.bind_ctx([HistoryMigrationEntry, MsgLog]):
        test_db.create_tables([HistoryMigrationEntry, MsgLog])
        history_columns = {column.name for column in test_db.get_columns("historymigrationentry")}
        msglog_columns = {column.name for column in test_db.get_columns("msglog")}

    assert {
        "slave_chat_id",
        "target_chat_id",
        "message_thread_id",
        "source_master_msg_id",
        "formatted_text",
        "position",
    }.issubset(history_columns)
    assert "source_master_msg_id" not in msglog_columns


def test_outbound_tables_and_history_workflow_fields_exist():
    from peewee import SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    models = [OutboundWorkflow, OutboundTask, HistoryMigrationEntry, MsgLog]
    with test_db.bind_ctx(models):
        test_db.create_tables(models)
        history_columns = {column.name for column in test_db.get_columns("historymigrationentry")}
        task_indexes = {index.name for index in test_db.get_indexes("outboundtask")}

    assert {"outbound_workflow_id", "state", "last_error"}.issubset(history_columns)
    assert any("source_key" in index_name for index_name in task_indexes)


def test_sqlite_legacy_migration_adds_durable_outbound_links_and_preserves_rows(tmp_path):
    from peewee import SqliteDatabase
    from playhouse.migrate import SqliteMigrator

    original_database = database.obj
    test_db = SqliteDatabase(tmp_path / "legacy.db")
    database.initialize(test_db)
    test_db.connect()
    try:
        test_db.execute_sql("CREATE TABLE msglog (master_msg_id TEXT PRIMARY KEY)")
        test_db.execute_sql(
            "CREATE TABLE historymigrationentry ("
            "id INTEGER PRIMARY KEY, source_master_msg_id TEXT, state_marker TEXT)"
        )
        test_db.execute_sql("INSERT INTO msglog (master_msg_id) VALUES ('100.1')")
        test_db.execute_sql(
            "INSERT INTO historymigrationentry (id, source_master_msg_id, state_marker) "
            "VALUES (1, '100.1', 'preserve')"
        )
        manager = object.__new__(DatabaseManager)
        manager._migrator_cls = SqliteMigrator

        manager._migrate(5)

        msglog_columns = {column.name for column in test_db.get_columns("msglog")}
        history_columns = {column.name for column in test_db.get_columns("historymigrationentry")}
        history_indexes = test_db.get_indexes("historymigrationentry")
        preserved = test_db.execute_sql(
            "SELECT source_master_msg_id, state_marker FROM historymigrationentry WHERE id = 1"
        ).fetchone()
    finally:
        test_db.close()
        database.initialize(original_database)

    assert "outbound_task_id" in msglog_columns
    assert {"outbound_workflow_id", "state", "last_error"}.issubset(history_columns)
    assert any(index.columns == ["outbound_workflow_id"] for index in history_indexes)
    assert preserved == ("100.1", "preserve")


def test_database_manager_uses_transactional_wal_sqlite_and_creates_durable_tables(tmp_path, monkeypatch):
    from peewee import SqliteDatabase

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    channel = SimpleNamespace(channel_id="tests.sqlite", config={})
    manager = DatabaseManager(channel)
    try:
        assert isinstance(database.obj, SqliteDatabase)
        assert database.obj.pragma("journal_mode").lower() == "wal"
        with database.atomic():
            workflow = OutboundWorkflow.create()
            OutboundTask.create(
                source_key="module chat",
                slave_id="module chat",
                priority=False,
                target_chat_id=100,
                operation="api_send_message",
                payload='{"version":1}',
                workflow_id=workflow.id,
            )
        assert OutboundTask.select().count() == 1
    finally:
        manager.stop_worker()
        database.initialize(original_database)


def test_topic_assoc_table_exists_and_round_trips(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(11111)
    thread_id = TelegramTopicID(22222)

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    assoc = channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    assert isinstance(assoc, TopicAssoc)
    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) == thread_id
    channel.db.remove_topic_assoc(slave_uid=slave_uid)


def test_create_missing_tables_preserves_existing_chat_assoc(channel):
    channel.db.add_chat_assoc("master-existing", "slave-existing", multiple_slave=True)
    TopicAssoc.drop_table(safe=True)
    HistoryMigrationEntry.drop_table(safe=True)

    channel.db._create_missing_tables()

    assert TopicAssoc.table_exists()
    assert HistoryMigrationEntry.table_exists()
    assert ChatAssoc.get_or_none(ChatAssoc.master_uid == "master-existing") is not None
    channel.db.remove_chat_assoc(master_uid="master-existing")


def test_topic_assoc_is_replaced_for_same_slave_and_thread(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(33333)

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, TelegramTopicID(44444), slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, TelegramTopicID(55555), slave_uid)

    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) == TelegramTopicID(55555)
    rows = list(TopicAssoc.select().where(TopicAssoc.slave_uid == slave_uid))
    assert len(rows) == 1
    channel.db.remove_topic_assoc(slave_uid=slave_uid)


def test_remove_chat_assoc_removes_topic_assoc(channel):
    channel.db.add_chat_assoc("master-topic-cleanup", "slave-topic-cleanup", multiple_slave=True)
    channel.db.add_topic_assoc(TelegramChatID(66666), TelegramTopicID(77777), "slave-topic-cleanup")

    channel.db.remove_chat_assoc(master_uid="master-topic-cleanup")

    assert channel.db.get_topic_thread_id("slave-topic-cleanup", TelegramChatID(66666)) is None


def test_add_or_update_message_log_persists_sender_bot_id(channel, slave):
    chat = slave.chat_with_alias
    author = chat.self
    etm_msg = ETMMsg(
        uid=MessageID("db-test-message"),
        chat=channel.chat_manager.update_chat_obj(chat),
        author=channel.chat_manager.get_or_enrol_member(chat, author),
        text="db test",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=channel,
    )

    master_message = SimpleNamespace(chat_id=123456, message_id=654321)
    channel.db.add_or_update_message_log(etm_msg, master_message, sender_bot_id="777")

    stored = channel.db.get_msg_log(master_msg_id=utils.message_id_to_str(TelegramChatID(123456), TelegramMessageID(654321)))
    assert stored is not None
    assert stored.sender_bot_id == "777"

    stored.delete_instance()


def test_build_etm_msg_restores_sender_bot_id(channel, slave):
    chat = slave.chat_with_alias
    etm_msg = ETMMsg(
        uid=MessageID("restored-message"),
        chat=channel.chat_manager.update_chat_obj(chat),
        author=channel.chat_manager.get_or_enrol_member(chat, chat.other),
        text="restored",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=channel,
    )
    master_message = SimpleNamespace(chat_id=4444, message_id=5555)
    channel.db.add_or_update_message_log(etm_msg, master_message, sender_bot_id="888")

    row = channel.db.get_msg_log(master_msg_id="4444.5555")
    assert row is not None
    restored = row.build_etm_msg(channel.chat_manager)
    assert restored.sender_bot_id == "888"

    row.delete_instance()


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_HOST"),
    reason="PostgreSQL test environment is not configured",
)
def test_postgresql_env_is_configured():
    required = [
        "TEST_POSTGRES_HOST",
        "TEST_POSTGRES_PORT",
        "TEST_POSTGRES_DB",
        "TEST_POSTGRES_USER",
        "TEST_POSTGRES_PASSWORD",
    ]
    for key in required:
        assert os.getenv(key)


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_HOST"),
    reason="PostgreSQL test environment is not configured",
)
def test_postgresql_durable_outbound_migration_and_schema_preserve_legacy_rows():
    from peewee import PostgresqlDatabase
    from playhouse.migrate import PostgresqlMigrator

    connection_kwargs = {
        "database": os.environ["TEST_POSTGRES_DB"],
        "host": os.environ["TEST_POSTGRES_HOST"],
        "port": int(os.environ["TEST_POSTGRES_PORT"]),
        "user": os.environ["TEST_POSTGRES_USER"],
        "password": os.environ["TEST_POSTGRES_PASSWORD"],
    }
    schema = f"etm_outbound_{uuid.uuid4().hex}"
    admin_db = PostgresqlDatabase(**connection_kwargs)
    admin_db.connect()
    admin_db.execute_sql(f'CREATE SCHEMA "{schema}"')
    admin_db.close()

    original_database = database.obj
    test_db = PostgresqlDatabase(
        **connection_kwargs,
        options=f'-c search_path="{schema}" -c timezone=UTC',
    )
    database.initialize(test_db)
    test_db.connect()
    try:
        test_db.execute_sql("CREATE TABLE msglog (master_msg_id TEXT PRIMARY KEY)")
        test_db.execute_sql(
            "CREATE TABLE historymigrationentry ("
            "id BIGSERIAL PRIMARY KEY, source_master_msg_id TEXT, state_marker TEXT)"
        )
        test_db.execute_sql("INSERT INTO msglog (master_msg_id) VALUES ('100.1')")
        test_db.execute_sql(
            "INSERT INTO historymigrationentry (source_master_msg_id, state_marker) "
            "VALUES ('100.1', 'preserve')"
        )
        manager = object.__new__(DatabaseManager)
        manager._migrator_cls = PostgresqlMigrator
        manager._migrate(5)
        test_db.create_tables([OutboundWorkflow, OutboundTask])

        assert "outbound_task_id" in {column.name for column in test_db.get_columns("msglog")}
        assert {"outbound_workflow_id", "state", "last_error"}.issubset(
            column.name for column in test_db.get_columns("historymigrationentry")
        )
        assert OutboundWorkflow.table_exists()
        assert OutboundTask.table_exists()
        preserved = test_db.execute_sql(
            "SELECT source_master_msg_id, state_marker FROM historymigrationentry"
        ).fetchone()
        assert preserved == ("100.1", "preserve")
    finally:
        test_db.close()
        database.initialize(original_database)
        admin_db = PostgresqlDatabase(**connection_kwargs)
        admin_db.connect()
        admin_db.execute_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin_db.close()
