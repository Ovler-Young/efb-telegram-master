from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from telegram import Update

from ehforwarderbot.types import ChatID

from efb_telegram_master import utils
from efb_telegram_master.chat_binding import ChatListStorage
from efb_telegram_master.db import MsgLog
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID


def _build_link_update(chat_id, *, is_forum=False):
    effective_chat = SimpleNamespace(id=chat_id, is_forum=is_forum, type="group")
    message = Mock()
    message.chat = effective_chat
    message.forward_from_chat = None
    message.reply_text = Mock()
    return Update(update_id=1, message=message)


def _store_link_session(channel, chat, storage_key, backfill_mode=None):
    storage = ChatListStorage([channel.chat_manager.update_chat_obj(chat)])
    storage.backfill_mode = backfill_mode
    channel.chat_binding.msg_storage[storage_key] = storage


def _cleanup_link_state(channel, chat, master_chat_id):
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(master_chat_id)))
    channel.db.remove_chat_assoc(master_uid=master_uid)
    channel.db.remove_topic_assoc(slave_uid=utils.chat_id_to_str(chat=chat))


def test_link_chat_auto_mode_backfills_on_first_link(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(101))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
    update = _build_link_update(bot_group)

    sent_message = Mock()
    sent_message.chat.id = bot_group
    sent_message.message_id = 500
    sent_message.reply_text = Mock()

    with patch.object(channel.bot_manager, "send_message", return_value=sent_message), \
         patch.object(channel.bot_manager, "edit_message_text"), \
         patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history, \
         patch.object(channel.chat_binding, "send_history_link") as send_history_link:
        channel.chat_binding.link_chat(update, [token])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


def test_link_chat_auto_mode_sends_history_link_on_relink(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(102))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.db.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)

    sent_message = Mock()
    sent_message.chat.id = bot_group
    sent_message.message_id = 501
    sent_message.reply_text = Mock()

    with patch.object(channel.bot_manager, "send_message", return_value=sent_message), \
         patch.object(channel.bot_manager, "edit_message_text"), \
         patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history, \
         patch.object(channel.chat_binding, "send_history_link") as send_history_link:
        channel.chat_binding.link_chat(update, [token])

    migrate_chat_history.assert_not_called()
    send_history_link.assert_called_once()
    _cleanup_link_state(channel, chat, bot_group)


def test_link_chat_backfill_override_forces_behavior(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(103))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.db.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)

    sent_message = Mock()
    sent_message.chat.id = bot_group
    sent_message.message_id = 502
    sent_message.reply_text = Mock()

    with patch.object(channel.bot_manager, "send_message", return_value=sent_message), \
         patch.object(channel.bot_manager, "edit_message_text"), \
         patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history, \
         patch.object(channel.chat_binding, "send_history_link") as send_history_link:
        channel.chat_binding.link_chat(update, [token, "true"])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


def test_migrate_chat_history_batches_text_and_forwards_media(channel):
    msg_logs = []
    base_time = datetime.now()
    for idx in range(3):
        msg_log = Mock()
        msg_log.text = "x" * 2000
        msg_log.media_type = "Text"
        msg_log.time = base_time + timedelta(seconds=idx)
        etm_msg = SimpleNamespace(author=SimpleNamespace(display_name=f"author-{idx}"))
        msg_log.build_etm_msg.return_value = etm_msg
        msg_logs.append(msg_log)

    media_log = Mock()
    media_log.text = ""
    media_log.media_type = "Photo"
    media_log.master_msg_id = "1.2"
    media_log.time = base_time + timedelta(seconds=10)
    msg_logs.append(media_log)

    with patch.object(channel.db, "get_recent_messages", return_value=msg_logs), \
         patch.object(channel.chat_binding, "_migration_send_text") as send_text, \
         patch.object(channel.chat_binding, "_migration_forward_media") as forward_media:
        channel.chat_binding._migrate_chat_history_background("tests.mocks.slave.chat", 12345)

    assert send_text.call_count == 2
    forward_media.assert_called_once_with(media_log, 12345, None)


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
