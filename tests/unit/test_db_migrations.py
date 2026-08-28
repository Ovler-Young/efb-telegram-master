import os
import logging
import pickle
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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


def test_database_manager_uses_transactional_wal_sqlite(tmp_path, monkeypatch):
    from peewee import SqliteDatabase

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    channel = SimpleNamespace(channel_id="tests.sqlite", config={})
    manager = DatabaseManager(channel)
    try:
        assert isinstance(database.obj, SqliteDatabase)
        assert database.obj.pragma("journal_mode").lower() == "wal"
    finally:
        manager.stop_worker()
        database.initialize(original_database)


def test_startup_observes_raw_legacy_rows_without_mutating_them(tmp_path, monkeypatch, caplog):
    from peewee import SqliteDatabase

    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        raw_db.execute_sql(
            "CREATE TABLE outbound_workflow (id INTEGER PRIMARY KEY, state TEXT, marker TEXT)"
        )
        raw_db.execute_sql(
            "CREATE TABLE outbound_task (id INTEGER PRIMARY KEY, state TEXT, marker TEXT)"
        )
        raw_db.execute_sql(
            "INSERT INTO outbound_workflow (id, state, marker) VALUES (1, 'completed', 'workflow-marker')"
        )
        for index, state in enumerate(DatabaseManager._LEGACY_OUTBOUND_STATES, start=1):
            raw_db.execute_sql(
                "INSERT INTO outbound_task (id, state, marker) VALUES (?, ?, ?)",
                (index, state, f"marker-{index}"),
            )
        snapshot = {
            table: raw_db.execute_sql(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in DatabaseManager._LEGACY_OUTBOUND_TABLES
        }
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    channel = SimpleNamespace(channel_id="tests.legacy", config={})
    with caplog.at_level(logging.WARNING, logger="efb_telegram_master.db"):
        manager = DatabaseManager(channel)
    try:
        observed = {
            table: database.execute_sql(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in DatabaseManager._LEGACY_OUTBOUND_TABLES
        }
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert observed == snapshot
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "workflows=1 tasks=8" in warnings[0]
    for state in DatabaseManager._LEGACY_OUTBOUND_STATES:
        assert f"{state}=1" in warnings[0]


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


def test_reaction_alternate_updates_one_canonical_row_and_clears_retraction():
    from peewee import SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    manager = object.__new__(DatabaseManager)
    manager.logger = Mock()
    reactor = SimpleNamespace(module_id="tests.mocks.slave", uid="reactor")
    message = SimpleNamespace(
        uid=MessageID("reaction-message"),
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.mocks.slave", uid="author"),
        text="message",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
        file_id=None,
        file_unique_id=None,
        mime=None,
        is_system=False,
        attributes=None,
        commands=None,
        substitutions=None,
        target=None,
        sender_bot_id=None,
        reactions={},
    )

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=10))

        message.reactions = {"R0": [reactor]}
        manager.add_or_update_message_log(
            message,
            SimpleNamespace(chat_id=100, message_id=11),
            old_message_id=(TelegramChatID(100), TelegramMessageID(10)),
            sender_bot_id="777",
        )
        row = MsgLog.get()
        assert MsgLog.select().count() == 1
        assert (row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == ("100.10", "100.11", "777")
        assert pickle.loads(bytes(row.pickle))["reactions"] == {"R0": ("tests.mocks.slave reactor",)}

        message.reactions = {}
        manager.add_or_update_message_log(
            message,
            SimpleNamespace(chat_id=100, message_id=11),
            old_message_id=(TelegramChatID(100), TelegramMessageID(11)),
            sender_bot_id="777",
        )
        row = MsgLog.get()
        assert MsgLog.select().count() == 1
        assert (row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == ("100.10", "100.11", "777")
        assert row.pickle is None

        message.reactions = {"R1": [reactor]}
        manager.add_or_update_message_log(
            message,
            SimpleNamespace(chat_id=100, message_id=12),
            old_message_id=(TelegramChatID(100), TelegramMessageID(11)),
            sender_bot_id="888",
        )
        row = MsgLog.get()
        assert MsgLog.select().count() == 1
        assert (row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == ("100.10", "100.12", "888")
        assert pickle.loads(bytes(row.pickle))["reactions"] == {"R1": ("tests.mocks.slave reactor",)}


def test_reaction_alternate_db_failures_preserve_then_update_canonical_row():
    from peewee import SqliteDatabase

    test_db = SqliteDatabase(":memory:")
    manager = object.__new__(DatabaseManager)
    manager.logger = Mock()
    reactor = SimpleNamespace(module_id="tests.mocks.slave", uid="reactor")
    message = SimpleNamespace(
        uid=MessageID("reaction-message"), chat=SimpleNamespace(module_id="tests.mocks.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.mocks.slave", uid="author"), text="message", type=MsgType.Text,
        type_telegram=TGMsgType.Text, deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
        file_id=None, file_unique_id=None, mime=None, is_system=False, attributes=None, commands=None,
        substitutions=None, target=None, sender_bot_id=None, reactions={"NEW": [reactor]},
    )
    scenarios = (
        (None, 10, 11, 12),
        ("100.11", 11, 12, 13),
    )

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        for initial_alt, old_id, failed_id, success_id in scenarios:
            MsgLog.delete().execute()
            MsgLog.create(
                master_msg_id="100.10", master_msg_id_alt=initial_alt, slave_message_id="reaction-message",
                text="old", slave_origin_uid="tests.mocks.slave chat",
                slave_member_uid="tests.mocks.slave author", msg_type=MsgType.Text.name,
                sent_to="tests.mocks.slave", sender_bot_id="700",
                pickle=pickle.dumps({"reactions": {"OLD": ("tests.mocks.slave reactor",)}}),
            )

            with patch.object(MsgLog, "save", side_effect=RuntimeError("db failed")):
                with pytest.raises(RuntimeError, match="db failed"):
                    manager.add_or_update_message_log(
                        message, SimpleNamespace(chat_id=100, message_id=failed_id),
                        old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="800",
                    )

            row = MsgLog.get()
            assert (row.master_msg_id_alt, row.sender_bot_id) == (initial_alt, "700")
            assert pickle.loads(bytes(row.pickle))["reactions"] == {
                "OLD": ("tests.mocks.slave reactor",),
            }

            manager.add_or_update_message_log(
                message, SimpleNamespace(chat_id=100, message_id=success_id),
                old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="900",
            )
            row = MsgLog.get()
            assert MsgLog.select().count() == 1
            assert (row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == (
                "100.10", f"100.{success_id}", "900",
            )
            assert pickle.loads(bytes(row.pickle))["reactions"] == {
                "NEW": ("tests.mocks.slave reactor",),
            }

            message.reactions = {"FINAL": [reactor]}
            manager.add_or_update_message_log(
                message, SimpleNamespace(chat_id=100, message_id=success_id),
                old_message_id=(TelegramChatID(100), TelegramMessageID(success_id)), sender_bot_id="900",
            )
            row = MsgLog.get()
            assert MsgLog.select().count() == 1
            assert row.master_msg_id_alt == f"100.{success_id}"
            assert pickle.loads(bytes(row.pickle))["reactions"] == {
                "FINAL": ("tests.mocks.slave reactor",),
            }
            message.reactions = {"NEW": [reactor]}


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_HOST"),
    reason="PostgreSQL test environment is not configured",
)
def test_postgresql_startup_observes_raw_legacy_rows_without_mutating_them(
    tmp_path, monkeypatch, caplog
):
    from peewee import PostgresqlDatabase

    connection_kwargs = {
        "database": os.environ["TEST_POSTGRES_DB"],
        "host": os.environ["TEST_POSTGRES_HOST"],
        "port": int(os.environ["TEST_POSTGRES_PORT"]),
        "user": os.environ["TEST_POSTGRES_USER"],
        "password": os.environ["TEST_POSTGRES_PASSWORD"],
    }
    database_name = f"etm_legacy_{uuid.uuid4().hex}"
    admin_db = PostgresqlDatabase(**connection_kwargs)
    admin_db.connect()
    admin_db.connection().autocommit = True
    original_database = database.obj
    manager = None
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        admin_db.execute_sql(f'CREATE DATABASE "{database_name}"')
        test_db = PostgresqlDatabase(
            database_name,
            **{key: value for key, value in connection_kwargs.items() if key != "database"},
        )
        test_db.connect()
        with test_db.atomic():
            test_db.execute_sql("CREATE TABLE outbound_workflow (id BIGSERIAL PRIMARY KEY, marker TEXT)")
            test_db.execute_sql(
                "CREATE TABLE outbound_task (id BIGSERIAL PRIMARY KEY, state TEXT, marker TEXT)"
            )
            test_db.execute_sql("INSERT INTO outbound_workflow (marker) VALUES ('workflow-marker')")
            for index, state in enumerate(DatabaseManager._LEGACY_OUTBOUND_STATES, start=1):
                test_db.execute_sql(
                    "INSERT INTO outbound_task (state, marker) VALUES (%s, %s)",
                    (state, f"marker-{index}"),
                )
        snapshot = {
            table: test_db.execute_sql(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in DatabaseManager._LEGACY_OUTBOUND_TABLES
        }
        test_db.close()

        channel = SimpleNamespace(
            channel_id="tests.postgresql",
            config={
                "database": {
                    "type": "postgresql",
                    "database": database_name,
                    **{key: value for key, value in connection_kwargs.items() if key != "database"},
                }
            },
        )
        with caplog.at_level(logging.WARNING, logger="efb_telegram_master.db"):
            manager = DatabaseManager(channel)

        live_db = database.obj
        observed = {
            table: live_db.execute_sql(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in DatabaseManager._LEGACY_OUTBOUND_TABLES
        }
        assert observed == snapshot
        assert "Retained legacy outbound rows: workflows=1 tasks=8" in caplog.text
    finally:
        if manager is not None:
            manager.stop_worker()
        database.initialize(original_database)
        admin_db.execute_sql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database_name,),
        )
        admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_db.close()
