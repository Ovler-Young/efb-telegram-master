import threading
from concurrent.futures import Future
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ehforwarderbot.types import ChatID
from telegram import Update

from efb_telegram_master import TelegramChannel, utils
from efb_telegram_master.chat_binding import ChatBindingManager, ChatListStorage
from efb_telegram_master.constants import Flags
from efb_telegram_master.db import HistoryMigrationEntry, MsgLog
from efb_telegram_master.outbound import QueueRequest
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
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
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
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
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
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
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


def test_link_chat_backfill_override_forces_behavior(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(103))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
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
        channel.chat_binding.link_chat(update, [token, "true"])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


@pytest.mark.parametrize(("override", "expected_backfill"), [("yes", True), ("no", False)])
def test_link_chat_accepts_yes_no_backfill_aliases(channel, slave, bot_group, override, expected_backfill):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(130))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key, backfill_mode=None)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.db.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 530)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history,
        patch.object(channel.chat_binding, "send_history_link") as send_history_link,
    ):
        channel.chat_binding.link_chat(update, [token, override])

    assert migrate_chat_history.called is expected_backfill
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


def test_migrate_chat_history_waits_for_each_call_before_deleting_entries(channel):
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

    waiters = []
    for _ in range(4):
        waiter = Future()
        waiter.set_result(None)
        waiters.append(waiter)
    with patch.object(channel.db, "get_recent_messages", return_value=msg_logs), patch.object(channel.bot_manager.outbound_queue, "enqueue", side_effect=waiters) as enqueue:
        channel.chat_binding._migrate_chat_history_background("tests.mocks.slave.chat", 12345)

    assert enqueue.call_count == 4
    assert [call.args[0].operation for call in enqueue.call_args_list] == [
        "send_message",
        "send_message",
        "send_message",
        "copy_message",
    ]
    assert HistoryMigrationEntry.select().count() == 0


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


def test_process_pending_history_migrations_waits_before_next_enqueue_and_deletes_successes():
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
        ),
    ]

    def get_next_history_migration_target():
        return pending_entries[0] if pending_entries else None

    def get_history_migration_entries(_slave_chat_id, _tg_chat_id, _thread_id):
        return list(pending_entries)

    manager.db = SimpleNamespace(
        get_next_history_migration_target=Mock(side_effect=get_next_history_migration_target),
        get_history_migration_entries=Mock(side_effect=get_history_migration_entries),
        get_recent_messages=Mock(),
        delete_history_migration_entry=Mock(side_effect=lambda entry_id: pending_entries.remove(next(entry for entry in pending_entries if entry.id == entry_id))),
    )
    events = []

    class Waiter:
        def __init__(self, entry_id):
            self.entry_id = entry_id

        def result(self):
            events.append(("wait", self.entry_id))

    def enqueue(request):
        entry_id = len(events) // 2 + 1
        events.append(("enqueue", entry_id))
        return Waiter(entry_id)

    manager.bot = SimpleNamespace(outbound_queue=SimpleNamespace(enqueue=Mock(side_effect=enqueue)))

    ChatBindingManager._process_pending_history_migrations(manager)

    manager.db.get_recent_messages.assert_not_called()
    assert [call.args[0] for call in manager.bot.outbound_queue.enqueue.call_args_list] == [
        QueueRequest("send_message", (), {"chat_id": 12345, "text": "first\n", "parse_mode": "Markdown", "disable_notification": True}, 12345),
        QueueRequest("send_message", (), {"chat_id": 12345, "text": "second\n", "parse_mode": "Markdown", "disable_notification": True}, 12345),
        QueueRequest("copy_message", (), {"chat_id": 12345, "from_chat_id": 10, "message_id": 22, "disable_notification": True}, 12345),
    ]
    assert events == [
        ("enqueue", 1),
        ("wait", 1),
        ("enqueue", 2),
        ("wait", 2),
        ("enqueue", 3),
        ("wait", 3),
    ]
    assert pending_entries == []


def test_history_migration_retains_entry_and_logs_completed_count_on_waiter_failure():
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.logger = Mock()
    entry = SimpleNamespace(
        id=7,
        slave_chat_id="tests.mocks.slave.chat",
        target_chat_id="12345",
        message_thread_id=None,
        source_master_msg_id="10.20",
        formatted_text="first\n",
    )
    manager.db = SimpleNamespace(
        get_history_migration_entries=Mock(return_value=[entry]),
        delete_history_migration_entry=Mock(),
    )
    failed_waiter = Future()
    failed_waiter.set_exception(RuntimeError("Telegram failed"))
    manager.bot = SimpleNamespace(outbound_queue=SimpleNamespace(enqueue=Mock(return_value=failed_waiter)))

    processed = ChatBindingManager._process_history_migration_target(manager, entry)

    assert processed is False
    manager.db.delete_history_migration_entry.assert_not_called()
    manager.logger.warning.assert_called_once_with(
        "History migration entry %d retained after %d completed calls (%s).",
        7,
        0,
        "RuntimeError",
    )


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
    manager.bot = SimpleNamespace(outbound_queue=SimpleNamespace(enqueue=Mock()))

    processed = ChatBindingManager._process_history_migration_target(manager, entry)

    assert processed is True
    manager.bot.outbound_queue.enqueue.assert_not_called()
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
