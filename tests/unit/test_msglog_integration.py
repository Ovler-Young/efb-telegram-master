from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock
from threading import Lock

from telegram import Update
from efb_telegram_master import TelegramChannel
from efb_telegram_master.chat_binding import ChatBindingManager


def test_sync_msglog_requires_admin_and_a_bound_forum_group():
    channel = object.__new__(TelegramChannel)
    channel.config = {"admins": [10]}
    channel.db = SimpleNamespace(get_topic_slaves=Mock(return_value=[("tests.slave", 7)]))
    channel.chat_binding = SimpleNamespace(schedule_msglog_ingestion=Mock(return_value="started"))
    channel.bot_manager = SimpleNamespace(send_message=Mock())
    channel.translator = SimpleNamespace(gettext=lambda text: text)
    message = Mock()
    message.chat = SimpleNamespace(id=100, is_forum=True)
    message.from_user = SimpleNamespace(id=10)
    update = Update(update_id=1, message=message)

    TelegramChannel.sync_msglog(channel, update, SimpleNamespace())

    channel.chat_binding.schedule_msglog_ingestion.assert_called_once_with(100)
    channel.bot_manager.send_message.assert_called_once()

    message.from_user.id = 11
    TelegramChannel.sync_msglog(channel, update, SimpleNamespace())
    assert channel.chat_binding.schedule_msglog_ingestion.call_count == 1


def test_resume_msglog_ingestions_schedules_each_bound_retryable_group():
    manager = object.__new__(ChatBindingManager)
    manager.db = SimpleNamespace(
        get_resumable_msglog_ingestion_scans=Mock(return_value=[
            SimpleNamespace(source_chat_id="100"), SimpleNamespace(source_chat_id="200"),
        ]),
        get_topic_slaves=Mock(side_effect=[[('a', 1)], [('b', 2)]]),
    )
    manager.schedule_msglog_ingestion = Mock()
    manager.logger = Mock()

    ChatBindingManager.resume_pending_msglog_ingestions(manager)

    assert manager.schedule_msglog_ingestion.call_args_list[0].args == (100,)
    assert manager.schedule_msglog_ingestion.call_args_list[1].args == (200,)


def test_schedule_msglog_ingestion_starts_one_thread_per_group(monkeypatch):
    manager = object.__new__(ChatBindingManager)
    manager.channel = SimpleNamespace(mtproto=SimpleNamespace(
        enabled=True, connected=True, config=SimpleNamespace(scan_ceiling=100),
    ))
    manager.db = SimpleNamespace(get_or_create_msglog_ingestion_scan=Mock(
        return_value=SimpleNamespace(status="pending", scanned_count=0),
    ))
    manager._msglog_ingestion_lock = Lock()
    manager._msglog_ingestion_threads = {}
    thread = SimpleNamespace(is_alive=Mock(return_value=True), start=Mock())
    monkeypatch.setattr("efb_telegram_master.chat_binding.threading.Thread", Mock(return_value=thread))

    assert ChatBindingManager.schedule_msglog_ingestion(manager, 100) == "started"
    assert ChatBindingManager.schedule_msglog_ingestion(manager, 100) == "already running"
    thread.start.assert_called_once()


def test_ingested_text_and_media_backfill_use_copy_message():
    manager = object.__new__(ChatBindingManager)
    manager.db = Mock()
    manager.chat_manager = Mock()
    manager.logger = Mock()
    text_log = SimpleNamespace(
        master_msg_id="100.10", text="text", media_type="Text", provenance="mtproto_ingested",
        time=datetime.now(),
    )
    media_log = SimpleNamespace(
        master_msg_id="100.11", text="caption", media_type="Photo", provenance="mtproto_ingested",
        time=datetime.now(),
    )
    manager.db.get_recent_messages.return_value = [text_log, media_log]
    manager.db.replace_history_migration_entries.return_value = 2

    ChatBindingManager._queue_history_migration_entries(manager, "tests.slave", 300)

    entries = manager.db.replace_history_migration_entries.call_args.args[3]
    assert [entry["formatted_text"] for entry in entries] == [None, None]
    assert [ChatBindingManager._prepare_history_migration_call(entry, 300, None)[0] for entry in
            map(SimpleNamespace, entries)] == ["copy_message", "copy_message"]
