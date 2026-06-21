import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ehforwarderbot import Message, MsgType
from ehforwarderbot.types import MessageID

from efb_telegram_master import utils
from efb_telegram_master.db import MsgAlias, MsgLog, TopicAssoc
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID, TelegramTopicID


def test_msglog_schema_has_sender_bot_id(channel):
    columns = {column.name for column in channel.db.database.get_columns("msglog")} if hasattr(channel.db, "database") else None
    if columns is None:
        from efb_telegram_master.db import database
        columns = {column.name for column in database.get_columns("msglog")}
    assert "sender_bot_id" in columns


def test_msgalias_table_exists(channel):
    tables = channel.db.database.get_tables() if hasattr(channel.db, "database") else None
    if tables is None:
        from efb_telegram_master.db import database
        tables = database.get_tables()
    assert "msgalias" in tables


def test_topic_assoc_table_exists_and_round_trips(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(11111)
    thread_id = TelegramTopicID(22222)

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    assoc = channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    assert isinstance(assoc, TopicAssoc)
    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) == thread_id
    channel.db.remove_topic_assoc(slave_uid=slave_uid)


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


def test_msg_alias_resolves_recent_slave_uid(channel, slave):
    chat = slave.chat_with_alias
    author = chat.self
    etm_msg = ETMMsg(
        uid=MessageID("canonical-message"),
        chat=channel.chat_manager.update_chat_obj(chat),
        author=channel.chat_manager.get_or_enrol_member(chat, author),
        text="canonical text",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=channel,
    )
    master_message = SimpleNamespace(chat_id=3333, message_id=4444)
    channel.db.add_or_update_message_log(etm_msg, master_message)

    slave_uid = utils.chat_id_to_str(chat=chat)
    channel.db.add_msg_alias(slave_uid, MessageID("alias-message"), "3333.4444")

    stored = channel.db.get_msg_log(slave_msg_id=MessageID("alias-message"), slave_origin_uid=slave_uid)
    assert stored is not None
    assert stored.master_msg_id == "3333.4444"

    MsgAlias.delete().where(MsgAlias.slave_message_id == "alias-message").execute()
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
