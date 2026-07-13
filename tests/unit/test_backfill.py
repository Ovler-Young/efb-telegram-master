import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import telegram
from telegram import Update

from efb_telegram_master import TelegramChannel
from ehforwarderbot.types import ChatID

from efb_telegram_master import utils
from efb_telegram_master.chat_binding import ChatBindingManager, ChatListStorage
from efb_telegram_master.db import HistoryMigrationEntry, MsgLog
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
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 500)

    with patch.object(channel.bot_manager, "send_message", return_value=sent_message), \
         patch.object(channel.bot_manager, "edit_message_text"), \
         patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history, \
         patch.object(channel.chat_binding, "send_history_link") as send_history_link:
        channel.chat_binding.link_chat(update, [token])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


def test_link_chat_edits_status_message_with_sender_bot(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(105))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 505, sender_bot_id="8465204282")

    with patch.object(channel.bot_manager, "send_message", return_value=sent_message), \
         patch.object(channel.bot_manager, "edit_message_text") as edit_message_text, \
         patch.object(channel.chat_binding, "migrate_chat_history"), \
         patch.object(channel.chat_binding, "send_history_link"):
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
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.db.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 501)

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

    sent_message = _sent_link_message(bot_group, 502)

    with patch.object(channel.bot_manager, "send_message", return_value=sent_message), \
         patch.object(channel.bot_manager, "edit_message_text"), \
         patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history, \
         patch.object(channel.chat_binding, "send_history_link") as send_history_link:
        channel.chat_binding.link_chat(update, [token, "true"])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


def test_link_chat_raw_message_override_forces_behavior_when_args_are_truncated(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(104))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.db.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)
    update.effective_message.text = f"/start {token} true"

    sent_message = _sent_link_message(bot_group, 503)

    with patch.object(channel.bot_manager, "send_message", return_value=sent_message), \
         patch.object(channel.bot_manager, "edit_message_text"), \
         patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history, \
         patch.object(channel.chat_binding, "send_history_link") as send_history_link:
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


def test_migrate_chat_history_enqueues_first_durable_batch_without_deleting_rows(channel):
    HistoryMigrationEntry.delete().execute()
    msg_logs = []
    base_time = datetime.now()
    for idx in range(3):
        msg_log = Mock()
        msg_log.master_msg_id = f"1.{idx}"
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
         patch.object(channel.bot_manager, "enqueue_history_operation", return_value=1) as enqueue:
        channel.chat_binding._migrate_chat_history_background("tests.mocks.slave.chat", 12345)

    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["operation"] == "send_message"
    assert HistoryMigrationEntry.select().count() == 4


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


def test_process_pending_history_migrations_enqueues_one_workflow_without_msglog_scan():
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager._history_migration_lock = threading.Lock()
    manager.logger = Mock()
    pending_entries = [
        SimpleNamespace(
            id=1,
            slave_chat_id="tests.mocks.slave.chat",
            target_chat_id="12345",
            message_thread_id=None,
            source_master_msg_id="10.20",
            formatted_text="first\n",
            media_type="Text",
            source_time=datetime.now(),
            position=0,
            outbound_workflow_id=None,
            state="queued",
        ),
        SimpleNamespace(
            id=2,
            slave_chat_id="tests.mocks.slave.chat",
            target_chat_id="12345",
            message_thread_id=None,
            source_master_msg_id="10.21",
            formatted_text="second\n",
            media_type="Text",
            source_time=datetime.now() + timedelta(seconds=1),
            position=1,
            outbound_workflow_id=None,
            state="queued",
        ),
        SimpleNamespace(
            id=3,
            slave_chat_id="tests.mocks.slave.chat",
            target_chat_id="12345",
            message_thread_id=None,
            source_master_msg_id="10.22",
            formatted_text=None,
            media_type="Photo",
            source_time=datetime.now() + timedelta(seconds=2),
            position=2,
            outbound_workflow_id=None,
            state="queued",
        ),
    ]

    def get_next_history_migration_target():
        return pending_entries[0] if pending_entries else None

    def get_history_migration_entries(_slave_chat_id, _tg_chat_id, _thread_id):
        return list(pending_entries)

    manager.db = SimpleNamespace(
        get_next_history_migration_target=Mock(side_effect=get_next_history_migration_target),
        get_history_migration_entries=Mock(side_effect=get_history_migration_entries),
        reconcile_history_migration_workflow=Mock(),
        get_recent_messages=Mock(),
    )
    manager.bot = SimpleNamespace(enqueue_history_operation=Mock(return_value=99))

    ChatBindingManager._process_pending_history_migrations(manager)

    manager.db.get_recent_messages.assert_not_called()
    manager.bot.enqueue_history_operation.assert_called_once_with(
        source_key="tests.mocks.slave.chat",
        target_chat_id=12345,
        operation="send_message",
        args=(),
        kwargs={
            "chat_id": 12345,
            "text": "first\nsecond\n",
            "parse_mode": "Markdown",
            "disable_notification": True,
        },
        history_entry_ids=[1, 2],
    )
    assert [entry.id for entry in pending_entries] == [1, 2, 3]


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
