import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ehforwarderbot.types import ChatID
from peewee import SqliteDatabase
from telegram import Update

from efb_telegram_master import TelegramChannel, utils
from efb_telegram_master.chat_binding import ChatBindingManager, ChatListStorage
from efb_telegram_master.constants import Flags
from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.models import HistoryMigrationEntry, MsgLog, database
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID


def _build_link_update(chat_id, *, is_forum=False):
    effective_chat = SimpleNamespace(id=chat_id, is_forum=is_forum, type="group")
    message = Mock()
    message.chat = effective_chat
    message.forward_from_chat = None
    message.reply_text = Mock()
    return Update(update_id=1, message=message)


def _store_link_session(channel, chat, storage_key):
    storage = ChatListStorage([channel.chat_manager.update_chat_obj(chat)])
    channel.chat_binding.msg_storage[storage_key] = storage


def _cleanup_link_state(channel, chat, master_chat_id):
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(master_chat_id)))
    channel.db.remove_chat_assoc(master_uid=master_uid)
    channel.db.remove_topic_assoc(slave_uid=utils.chat_id_to_str(chat=chat))


def _sent_link_message(chat_id, message_id, sender_bot_id=None):
    sent_message = Mock()
    sent_message.chat.id = chat_id
    sent_message.message_id = message_id
    sent_message.reply_text = Mock()
    sent_message.sender_bot_id = sender_bot_id
    return sent_message


def test_link_chat_auto_mode_backfills_on_first_link(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(101))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    ChatBindingManager._set_conversation_state(channel.chat_binding.link_handler, storage_key, Flags.LINK_EXEC)
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 500)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history,
        patch.object(channel.chat_binding, "send_history_link") as send_history_link,
    ):
        channel.chat_binding.link_chat(update, [token])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    assert storage_key not in channel.chat_binding.link_handler._conversations
    _cleanup_link_state(channel, chat, bot_group)


def test_link_chat_preserves_session_when_link_fails(channel, slave, bot_group):
    chat = channel.chat_manager.update_chat_obj(slave.chat_with_alias)
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(106))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    ChatBindingManager._set_conversation_state(channel.chat_binding.link_handler, storage_key, Flags.LINK_EXEC)
    update = _build_link_update(bot_group)

    with patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 506)), patch.object(chat, "link", side_effect=RuntimeError("link failed")):
        with pytest.raises(RuntimeError, match="link failed"):
            channel.chat_binding.link_chat(update, [token])

    assert storage_key in channel.chat_binding.msg_storage
    assert channel.chat_binding.link_handler._conversations[storage_key] == Flags.LINK_EXEC


def test_link_chat_edits_status_message_with_sender_bot(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(105))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 505, sender_bot_id="8465204282")

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text") as edit_message_text,
        patch.object(channel.chat_binding, "migrate_chat_history"),
        patch.object(channel.chat_binding, "send_history_link"),
    ):
        channel.chat_binding.link_chat(update, [token])

    target_status_edit = edit_message_text.call_args_list[0].kwargs
    assert target_status_edit["chat_id"] == bot_group
    assert target_status_edit["message_id"] == 505
    assert target_status_edit["_sender_bot_id"] == "8465204282"
    _cleanup_link_state(channel, chat, bot_group)


def test_link_chat_auto_mode_sends_history_link_on_relink(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(102))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.db.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 501)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history,
        patch.object(channel.chat_binding, "send_history_link") as send_history_link,
    ):
        channel.chat_binding.link_chat(update, [token])

    migrate_chat_history.assert_not_called()
    send_history_link.assert_called_once()
    _cleanup_link_state(channel, chat, bot_group)


@pytest.mark.parametrize("backfill_flag", ["true", "yes"])
def test_link_chat_backfill_override_forces_behavior(channel, slave, bot_group, backfill_flag):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(103))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.db.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 502)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history,
        patch.object(channel.chat_binding, "send_history_link") as send_history_link,
    ):
        channel.chat_binding.link_chat(update, [token, backfill_flag])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


def test_link_chat_raw_message_override_forces_behavior_when_args_are_truncated(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(104))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.db.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)
    update.effective_message.text = f"/start {token} true"

    sent_message = _sent_link_message(bot_group, 503)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history,
        patch.object(channel.chat_binding, "send_history_link") as send_history_link,
    ):
        channel.chat_binding.link_chat(update, [token])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


def test_resolve_command_args_falls_back_to_raw_message_text():
    args = TelegramChannel._resolve_command_args("/start token true", ["token"])

    assert args == ["token", "true"]


def test_start_uses_raw_message_args_for_link_chat(channel):
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "text": "/start token true",
                "chat": {"id": -1001, "type": "supergroup", "title": "Test Group"},
                "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
            },
        },
        channel.bot_manager._async_bot,
    )
    context = SimpleNamespace(args=["token"])

    with patch.object(channel.chat_binding, "link_chat") as link_chat:
        channel.start(update, context)

    link_chat.assert_called_once_with(update, ["token", "true"])


def test_history_migration_dispatches_persisted_entries_through_telegram_api():
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.db = object.__new__(DatabaseManager)
    manager.logger = Mock()
    manager.bot = SimpleNamespace(send_message=Mock(), copy_message=Mock())
    try:
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.create(
            slave_chat_id="tests.mocks.slave.chat",
            target_chat_id="12345",
            source_master_msg_id="10.20",
            formatted_text="first\n",
            position=0,
        )
        HistoryMigrationEntry.create(
            slave_chat_id="tests.mocks.slave.chat",
            target_chat_id="12345",
            source_master_msg_id="10.21",
            formatted_text=None,
            position=1,
        )

        assert manager._process_history_migration_target(manager.db.get_next_history_migration_target()) is True

        manager.bot.send_message.assert_called_once_with(chat_id=12345, text="first\n", parse_mode="Markdown", disable_notification=True)
        manager.bot.copy_message.assert_called_once_with(chat_id=12345, from_chat_id=10, message_id=21, disable_notification=True)
        assert HistoryMigrationEntry.select().count() == 0

        failed_entry = HistoryMigrationEntry.create(
            slave_chat_id="tests.mocks.slave.chat",
            target_chat_id="12345",
            source_master_msg_id="10.22",
            formatted_text="failed\n",
            position=0,
        )
        manager.bot.send_message.side_effect = RuntimeError("Telegram failed")

        assert manager._process_history_migration_target(failed_entry) is False
        assert HistoryMigrationEntry.select().where(HistoryMigrationEntry.id == failed_entry.id).exists()
    finally:
        test_database.close()
        database.initialize(original_database)


def test_pending_history_migrations_send_entries_in_position_order_and_delete_each_success():
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.db = object.__new__(DatabaseManager)
    manager.logger = Mock()
    manager._history_migration_lock = threading.Lock()
    manager.bot = SimpleNamespace(send_message=Mock(), copy_message=Mock())
    try:
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.insert_many(
            [
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.20", "formatted_text": "first\n", "position": 0},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.21", "formatted_text": "second\n", "position": 1},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.22", "formatted_text": None, "position": 2},
            ]
        ).execute()

        manager._process_pending_history_migrations()

        assert [call.kwargs["text"] for call in manager.bot.send_message.call_args_list] == ["first\n", "second\n"]
        manager.bot.copy_message.assert_called_once_with(chat_id=12345, from_chat_id=10, message_id=22, disable_notification=True)
        assert HistoryMigrationEntry.select().count() == 0
    finally:
        test_database.close()
        database.initialize(original_database)


def test_pending_history_migrations_keep_the_failed_entry_and_remaining_boundary():
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.db = object.__new__(DatabaseManager)
    manager.logger = Mock()
    manager._history_migration_lock = threading.Lock()
    manager.bot = SimpleNamespace(send_message=Mock(side_effect=[None, RuntimeError("Telegram failed")]), copy_message=Mock())
    try:
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.insert_many(
            [
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.20", "formatted_text": "first\n", "position": 0},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.21", "formatted_text": "second\n", "position": 1},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.22", "formatted_text": None, "position": 2},
            ]
        ).execute()

        manager._process_pending_history_migrations()

        assert [entry.source_master_msg_id for entry in HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.position)] == ["10.21", "10.22"]
        manager.bot.copy_message.assert_not_called()
    finally:
        test_database.close()
        database.initialize(original_database)


def test_queue_history_migration_entries_persists_pending_rows():
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.db = Mock()
    manager.chat_manager = Mock()
    manager.logger = Mock()
    base_time = datetime.now()
    text_log = Mock()
    text_log.master_msg_id = "10.20"
    text_log.text = "hello"
    text_log.media_type = "Text"
    text_log.time = base_time
    text_log.build_etm_msg.return_value = SimpleNamespace(author=SimpleNamespace(display_name="author"))
    media_log = Mock()
    media_log.master_msg_id = "10.21"
    media_log.text = ""
    media_log.media_type = "Photo"
    media_log.time = base_time + timedelta(seconds=1)
    manager.db.get_recent_messages.return_value = [text_log, media_log]
    manager.db.replace_history_migration_entries.return_value = 2

    queued_count = ChatBindingManager._queue_history_migration_entries(
        manager,
        "tests.mocks.slave.chat",
        12345,
    )

    entries = manager.db.replace_history_migration_entries.call_args.args[3]
    assert queued_count == 2
    assert len(entries) == 2
    assert entries[0]["source_master_msg_id"] == "10.20"
    assert entries[0]["formatted_text"] == f"*author* `{base_time.strftime('%Y-%m-%d %H:%M')}`\nhello\n\n"
    assert entries[1]["source_master_msg_id"] == "10.21"
    assert entries[1]["formatted_text"] is None


def test_history_migration_deletes_zero_call_entry_without_queueing():
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.logger = Mock()
    entry = SimpleNamespace(
        id=8,
        slave_chat_id="tests.mocks.slave.chat",
        target_chat_id="12345",
        message_thread_id=None,
        source_master_msg_id="",
        formatted_text="",
    )
    manager.db = SimpleNamespace(
        get_history_migration_entries=Mock(return_value=[entry]),
        delete_history_migration_entry=Mock(),
    )
    manager.bot = SimpleNamespace(send_message=Mock(), copy_message=Mock())

    processed = ChatBindingManager._process_history_migration_target(manager, entry)

    assert processed is True
    manager.bot.send_message.assert_not_called()
    manager.bot.copy_message.assert_not_called()
    manager.db.delete_history_migration_entry.assert_called_once_with(8)
    manager.logger.info.assert_any_call("History migration entry %d completed 0 calls", 8)


def test_get_recent_messages_returns_oldest_first(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    existing = list(MsgLog.select().where(MsgLog.slave_origin_uid == slave_uid))
    for row in existing:
        row.delete_instance()

    base_time = datetime.now()
    for idx in range(3):
        MsgLog.create(
            master_msg_id=f"9000.{idx}",
            master_msg_id_alt=None,
            slave_message_id=f"slave-{idx}",
            text=f"text-{idx}",
            slave_origin_uid=slave_uid,
            slave_member_uid=slave_uid,
            media_type="Text",
            mime=None,
            file_id=None,
            file_unique_id=None,
            msg_type="Text",
            sent_to=channel.channel_id,
            sender_bot_id=None,
            time=base_time + timedelta(seconds=idx),
        )

    recent = channel.db.get_recent_messages(slave_uid, limit=0)
    assert [row.slave_message_id for row in recent] == ["slave-0", "slave-1", "slave-2"]

    for row in MsgLog.select().where(MsgLog.slave_origin_uid == slave_uid):
        row.delete_instance()
