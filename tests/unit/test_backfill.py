import threading
from contextlib import ExitStack
from datetime import datetime, timedelta
from gettext import NullTranslations
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest
from ehforwarderbot.channel import MasterChannel
from ehforwarderbot.types import ChatID, ModuleID
from peewee import SqliteDatabase
from telegram import Update

from efb_telegram_master import TelegramChannel, utils
from efb_telegram_master.callback_sessions import CallbackSessionStore, ChatListStorage
from efb_telegram_master.channel_commands import TelegramCommandService
from efb_telegram_master.constants import Flags
from efb_telegram_master.history_migration_repository import HistoryMigrationRepository
from efb_telegram_master.history_replay import HistoryReplayShutdownTimeout, HistoryReplayWorker, history_location_text
from efb_telegram_master.link_completion import LinkCompletionService
from efb_telegram_master.models import HistoryMigrationEntry, MsgLog, database
from efb_telegram_master.mtproto import MTProtoConfig
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID


def _build_link_update(chat_id, *, is_forum=False, user_id=1):
    effective_chat = SimpleNamespace(id=chat_id, is_forum=is_forum, type="group")
    message = Mock()
    message.chat = effective_chat
    message.forward_from_chat = None
    message.reply_text = Mock()
    message.from_user = SimpleNamespace(id=user_id)
    return Update(update_id=1, message=message)


def _store_link_session(channel, chat, storage_key):
    storage = ChatListStorage([channel.chat_manager.update_chat_obj(chat)])
    channel.callback_sessions.store(storage_key, 1, storage)


def _cleanup_link_state(channel, chat, master_chat_id):
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(master_chat_id)))
    channel.chat_associations.remove_chat_assoc(master_uid=master_uid)
    channel.chat_associations.remove_topic_assoc(slave_uid=utils.chat_id_to_str(chat=chat))


def _sent_link_message(chat_id, message_id, sender_bot_id=None):
    sent_message = Mock()
    sent_message.chat.id = chat_id
    sent_message.message_id = message_id
    sent_message.reply_text = Mock()
    sent_message.sender_bot_id = sender_bot_id
    return sent_message


def _link_completion_service(storage_key, chat, multiple_slave_chats=lambda: False):
    bot = SimpleNamespace(
        send_message=Mock(return_value=_sent_link_message(-100500, 600)),
        edit_message_text=Mock(),
    )
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    handler = SimpleNamespace(_conversations={})
    callback_sessions.start(handler, storage_key, Flags.LINK_EXEC, 1, ChatListStorage([chat]))
    service = LinkCompletionService(
        bot,
        ModuleID("blueset.telegram"),
        multiple_slave_chats,
        SimpleNamespace(remove_topic_assoc=Mock(), get_chat_assoc=Mock(return_value=[])),
        callback_sessions,
        Mock(),
        Mock(),
        lambda message: message,
        lambda single, plural, count: single if count == 1 else plural,
        Mock(),
        handler,
    )
    return service


def test_link_completion_reads_multiple_slave_setting_at_completion_time():
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(458))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(
        module_id=ModuleID("tests.slave"),
        uid=ChatID("chat"),
        linked=[],
        full_name="Test chat",
        link=Mock(),
    )
    multiple_slave_chats = Mock(return_value=False)
    service = _link_completion_service(storage_key, chat, multiple_slave_chats)

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id"):
        service.complete(_build_link_update(-100500), [token])

    multiple_slave_chats.assert_called_once_with()
    assert chat.link.call_args.args[-1] is False


def test_link_completion_rejects_an_inactive_slave_without_consuming_its_session():
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(457))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(
        module_id=ModuleID("tests.inactive_slave"),
        uid=ChatID("chat"),
        linked=[],
        full_name="Inactive chat",
        link=Mock(),
    )
    service = _link_completion_service(storage_key, chat)

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id", side_effect=NameError):
        service.complete(_build_link_update(-100500), [token])

    service.bot.edit_message_text.assert_called_once_with(
        text="tests.inactive_slave is not activated in current profile. It cannot be linked.",
        chat_id=storage_key[0],
        message_id=storage_key[1],
    )
    service.bot.send_message.assert_not_called()
    chat.link.assert_not_called()
    service.chat_associations.remove_topic_assoc.assert_not_called()
    service.topic_sync.create_topic.assert_not_called()
    service.history_replay.start.assert_not_called()
    assert service.callback_sessions.lookup(storage_key) is not None
    assert service._conversation_handler._conversations[storage_key] == Flags.LINK_EXEC


def test_forged_start_token_does_not_consume_the_owner_session():
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(459))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(module_id=ModuleID("tests.slave"), uid=ChatID("chat"), linked=[], full_name="Test chat", link=Mock())
    service = _link_completion_service(storage_key, chat)

    service.complete(_build_link_update(-100500, user_id=2), [token])

    chat.link.assert_not_called()
    service.bot.send_message.assert_called_once_with(-100500, text="Session expired or unknown parameter. (SE02)", message_thread_id=ANY)
    assert service.callback_sessions.lookup(storage_key) is not None
    assert storage_key in service._conversation_handler._conversations

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id"):
        service.complete(_build_link_update(-100500), [token])
    chat.link.assert_called_once()
    assert service.callback_sessions.lookup(storage_key) is None


def test_start_with_missing_effective_user_does_not_consume_a_session():
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(460))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(module_id=ModuleID("tests.slave"), uid=ChatID("chat"), linked=[], full_name="Test chat", link=Mock())
    service = _link_completion_service(storage_key, chat)
    update = _build_link_update(-100500)
    update.effective_message.text = "/start " + token
    update.effective_message.from_user = None

    service.complete(update, [token])

    chat.link.assert_not_called()
    assert service.callback_sessions.lookup(storage_key) is not None


def test_link_chat_auto_mode_backfills_on_first_link(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(101))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    channel.callback_sessions.set_state(channel.link_handler, storage_key, Flags.LINK_EXEC)
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 500)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as migrate_chat_history,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token])

    migrate_chat_history.assert_called_once_with(utils.chat_id_to_str(chat=chat), bot_group, None, storage_key)
    send_history_link.assert_not_called()
    assert storage_key not in channel.link_handler._conversations
    _cleanup_link_state(channel, chat, bot_group)


def test_initial_link_backfills_to_the_chat_migrated_during_status_edit(channel, slave, bot_group):
    chat = slave.chat_with_alias
    migrated_chat_id = -100700
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(109))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    update = _build_link_update(bot_group)

    def migrate_status_edit(**kwargs):
        if kwargs["chat_id"] == bot_group:
            channel.topic_sync.migrate_chat_associations(bot_group, migrated_chat_id)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 509)),
        patch.object(channel.bot_manager, "get_chat_info", return_value=SimpleNamespace(is_forum=False)),
        patch.object(channel.bot_manager, "edit_message_text", side_effect=migrate_status_edit),
        patch.object(channel.history_replay, "start") as start,
    ):
        channel.link_completion.complete(update, [token])

    start.assert_called_once_with(utils.chat_id_to_str(chat=chat), migrated_chat_id, None, storage_key)
    _cleanup_link_state(channel, chat, migrated_chat_id)


def test_replacing_a_chat_association_discards_its_pending_history(channel, slave, bot_group):
    chat = slave.chat_with_alias
    slave_uid = utils.chat_id_to_str(chat=chat)
    old_master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    replacement_master_uid = utils.chat_id_to_str(channel.channel_id, ChatID("-100701"))
    channel.chat_associations.add_chat_assoc(old_master_uid, slave_uid)
    HistoryMigrationEntry.create(
        slave_chat_id=slave_uid,
        target_chat_id=str(bot_group),
        source_master_msg_id="10.20",
        formatted_text="pending",
        position=0,
    )

    channel.chat_associations.add_chat_assoc(replacement_master_uid, slave_uid)

    assert not HistoryMigrationEntry.select().where(HistoryMigrationEntry.slave_chat_id == slave_uid).exists()
    _cleanup_link_state(channel, chat, -100701)


def test_link_chat_preserves_session_when_link_fails(channel, slave, bot_group):
    chat = channel.chat_manager.update_chat_obj(slave.chat_with_alias)
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(106))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    channel.callback_sessions.set_state(channel.link_handler, storage_key, Flags.LINK_EXEC)
    update = _build_link_update(bot_group)

    with patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 506)), patch.object(chat, "link", side_effect=RuntimeError("link failed")):
        with pytest.raises(RuntimeError, match="link failed"):
            channel.link_completion.complete(update, [token])

    assert channel.callback_sessions.lookup(storage_key) is not None
    assert channel.link_handler._conversations[storage_key] == Flags.LINK_EXEC


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
        patch.object(channel.history_replay, "start"),
        patch.object(channel.link_completion, "send_history_link"),
    ):
        channel.link_completion.complete(update, [token])

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
    channel.chat_associations.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 501)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as migrate_chat_history,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token])

    migrate_chat_history.assert_not_called()
    send_history_link.assert_called_once()
    _cleanup_link_state(channel, chat, bot_group)


@pytest.mark.parametrize("backfill_flag", ["true", "yes", "on", "1"])
def test_link_chat_backfill_override_forces_replay(channel, slave, bot_group, backfill_flag):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(103))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.chat_associations.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)

    sent_message = _sent_link_message(bot_group, 502)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as migrate_chat_history,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token, backfill_flag])

    migrate_chat_history.assert_called_once_with(utils.chat_id_to_str(chat=chat), bot_group, None, storage_key)
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


@pytest.mark.parametrize("backfill_flag", ["false", "no", "off", "0"])
def test_link_chat_backfill_override_skips_replay(channel, slave, bot_group, backfill_flag):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(130))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.chat_associations.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 530)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as replay,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token, backfill_flag])

    replay.assert_not_called()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


def test_history_replay_resume_starts_a_worker_for_queued_entries():
    history_migrations = Mock(has_pending_entries=Mock(return_value=True))
    manager = HistoryReplayWorker(Mock(), Mock(), history_migrations, Mock(), Mock())

    with patch("efb_telegram_master.history_replay.threading.Thread") as thread:
        manager.resume()

    thread.assert_called_once_with(target=manager._run, name="HistoryMigrationReplay")
    thread.return_value.start.assert_called_once()


def test_history_replay_stop_rejects_starts_and_retries_after_a_blocked_call():
    started, release = threading.Event(), threading.Event()
    bot = SimpleNamespace(send_message=Mock(side_effect=lambda **_kwargs: (started.set(), release.wait())))
    worker = HistoryReplayWorker(bot, Mock(), Mock(), Mock(), Mock())
    worker.queue_entries = Mock(return_value=0)
    try:
        assert worker.start("tests.slave chat", 100, source_storage_key=(1, 2))
        assert started.wait(1)
        errors = worker.stop(0.01)
        assert len(errors) == 1
        assert isinstance(errors[0], HistoryReplayShutdownTimeout)
        assert worker.start("tests.slave chat", 100) is False
        release.set()
        assert worker.stop(1) == ()
        assert not any(thread.name == "HistoryMigrationReplay" and thread.is_alive() for thread in threading.enumerate())
    finally:
        release.set()
        worker.stop(1)


def test_history_replay_one_loop_drains_multiple_targets():
    first_started, release, second_done = threading.Event(), threading.Event(), threading.Event()
    worker = HistoryReplayWorker(Mock(), Mock(), SimpleNamespace(get_next_target=lambda: None), Mock(), Mock())
    queued: list[tuple[str, int]] = []

    def queue_entries(slave_chat_id, target_chat_id, _thread_id):
        queued.append((str(slave_chat_id), target_chat_id))
        if len(queued) == 1:
            first_started.set()
            release.wait(1)
        else:
            second_done.set()
        return 0

    worker.queue_entries = Mock(side_effect=queue_entries)
    try:
        assert worker.start("tests.slave first", 100)
        assert first_started.wait(1)
        assert worker.start("tests.slave second", 200)
        release.set()
        assert second_done.wait(1)
        assert worker.stop(1) == ()
        assert queued == [("tests.slave first", 100), ("tests.slave second", 200)]
    finally:
        release.set()
        worker.stop(1)


def test_history_replay_processes_request_enqueued_after_idle_queue_observation():
    observed_empty, second_done = threading.Event(), threading.Event()
    worker = HistoryReplayWorker(Mock(), Mock(), SimpleNamespace(get_next_target=lambda: None), Mock(), Mock())
    queued: list[int] = []
    original_wait = worker._condition.wait

    def observe_then_wait(*args, **kwargs):
        observed_empty.set()
        return original_wait(*args, **kwargs)

    worker._condition.wait = observe_then_wait
    worker.queue_entries = Mock(side_effect=lambda _slave, target, _thread: (queued.append(target), second_done.set() if target == 200 else None, 0)[2])
    try:
        assert worker.start("tests.slave first", 100)
        assert observed_empty.wait(1)
        assert worker.start("tests.slave second", 200)
        assert second_done.wait(1)
        assert queued == [100, 200]
    finally:
        worker.stop(1)


def test_empty_history_backfill_enqueues_one_location_notice_in_the_target_topic():
    bot = SimpleNamespace(send_message=Mock())
    worker = HistoryReplayWorker(bot, Mock(), Mock(), Mock(), Mock())
    worker.queue_entries = Mock(return_value=0)
    storage_key = (TelegramChatID(1044903212), TelegramMessageID(456))

    worker._queue_and_process("tests.slave chat", -100500, TelegramMessageID(789), storage_key)

    bot.send_message.assert_called_once_with(
        chat_id=-100500,
        text="This chat was previously linked. History messages are not migrated.",
        disable_notification=True,
        message_thread_id=TelegramMessageID(789),
    )


def test_private_callback_automatic_empty_backfill_omits_an_invalid_history_url():
    storage_key = (TelegramChatID(1044903212), TelegramMessageID(457))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(
        module_id=ModuleID("tests.slave"),
        uid=ChatID("chat"),
        linked=[],
        full_name="Test chat",
        link=Mock(),
    )
    service = _link_completion_service(storage_key, chat)

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id"):
        service.complete(_build_link_update(-100500), [token])

    service.history_replay.start.assert_called_once_with("tests.slave chat", -100500, None, storage_key)
    worker_bot = SimpleNamespace(send_message=Mock())
    worker = HistoryReplayWorker(worker_bot, Mock(), Mock(), Mock(), Mock())
    worker.queue_entries = Mock(return_value=0)
    worker._queue_and_process("tests.slave chat", -100500, None, storage_key)
    assert "https://t.me/" not in worker_bot.send_message.call_args.kwargs["text"]


def test_history_location_text_keeps_supergroup_history_urls():
    assert history_location_text(lambda message: message, (TelegramChatID(-1001234567890), TelegramMessageID(458))).endswith("https://t.me/c/1234567890/458")


@pytest.mark.parametrize("backfill_flag", ["true", "yes", "on", "1"], ids=str)
def test_requested_empty_backfill_sends_one_history_location_to_the_linked_topic(channel, slave, bot_group, backfill_flag):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(456))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.chat_associations.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group, is_forum=True)
    topic_id = 789
    channel.topic_sync.create_topic = Mock(return_value=topic_id)
    channel.msglogs.get_recent_messages = Mock(return_value=[])
    channel.history_migrations.replace_entries = Mock(return_value=0)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 531)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as start,
        patch.object(channel.topic_sync, "update_single_topic_info"),
    ):
        channel.link_completion.complete(update, [token, backfill_flag])
        start.assert_called_once_with(utils.chat_id_to_str(chat=chat), bot_group, topic_id, storage_key)
        channel.history_replay._queue_and_process(utils.chat_id_to_str(chat=chat), bot_group, topic_id, storage_key)

    history_calls = [call for call in channel.bot_manager.api.send_message.call_args_list if "previously linked" in call.kwargs.get("text", "")]
    assert len(history_calls) == 1
    assert history_calls[0].kwargs == {
        "chat_id": bot_group,
        "text": "This chat was previously linked. History messages are not migrated. You can view previous messages here: https://t.me/c/1234567890/456",
        "disable_notification": True,
        "message_thread_id": topic_id,
    }
    _cleanup_link_state(channel, chat, bot_group)


def test_automatic_empty_backfill_sends_the_original_history_location_once(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(457))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    channel.msglogs.get_recent_messages = Mock(return_value=[])
    channel.history_migrations.replace_entries = Mock(return_value=0)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 532)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as start,
    ):
        channel.link_completion.complete(_build_link_update(bot_group), [token])
        start.assert_called_once_with(utils.chat_id_to_str(chat=chat), bot_group, None, storage_key)
        channel.history_replay._queue_and_process(utils.chat_id_to_str(chat=chat), bot_group, None, storage_key)

    history_calls = [call for call in channel.bot_manager.api.send_message.call_args_list if "previously linked" in call.kwargs.get("text", "")]
    assert len(history_calls) == 1
    assert history_calls[0].kwargs["chat_id"] == bot_group
    assert history_calls[0].kwargs["text"].endswith("https://t.me/c/1234567890/457")
    _cleanup_link_state(channel, chat, bot_group)


def test_private_callback_relink_uses_the_previous_supergroup_history_location():
    storage_key = (TelegramChatID(1044903212), TelegramMessageID(458))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    previous_group_id = TelegramChatID(-1001234567890)
    chat = SimpleNamespace(
        module_id=ModuleID("tests.slave"),
        uid=ChatID("chat"),
        linked=[utils.chat_id_to_str(ModuleID("blueset.telegram"), ChatID(str(previous_group_id)))],
        full_name="Test chat",
        link=Mock(),
    )
    service = _link_completion_service(storage_key, chat)

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id"):
        service.complete(_build_link_update(-100500), [token])

    history_calls = [call for call in service.bot.send_message.call_args_list if "previously linked" in call.kwargs.get("text", "")]
    assert len(history_calls) == 1
    assert history_calls[0].kwargs["text"].endswith("https://t.me/c/1234567890/458")


def test_channel_composition_wires_sync_msglog_and_dynamic_locale():
    application = Mock()
    runtime = SimpleNamespace(application=application, as_async_callback=lambda callback: callback)
    api = SimpleNamespace(session_expired=lambda *_args, **_kwargs: None, send_message=Mock())
    bot_manager = SimpleNamespace(api=api, telegram_runtime=runtime, msglog_scan=Mock(), error=Mock())
    database_manager = SimpleNamespace(
        chat_associations=Mock(),
        slave_chat_info=Mock(),
        slave_message_deliveries=Mock(),
        msglogs=Mock(),
        history_migrations=Mock(),
        msglog_ingestion=Mock(),
        _base_path="/tmp",
    )
    flag_values = {"chats_per_page": 10, "multiple_slave_chats": False, "topic_group": 0, "auto_locale": True}
    flag = Mock(side_effect=lambda name: flag_values.get(name, False))
    history_replay = Mock()

    def load_config(_channel_id, _translate):
        return {"token": "token", "admins": [1]}, MTProtoConfig(enabled=False)

    dependencies = [
        "MTProtoClient",
        "ChatObjectCacheManager",
        "ChatDestinationCache",
        "TopicGroupService",
        "CommandsManager",
        "MasterMessageDelivery",
        "CallbackSessionStore",
        "RecipientSuggestionService",
        "LinkService",
        "LinkCompletionService",
        "ChatHeadService",
        "SlaveMessageRouter",
        "TextDelivery",
        "SlaveFileTransfer",
        "OversizedNoticeSender",
        "ImageDelivery",
        "SlaveMediaDelivery",
        "SlaveFileDelivery",
        "SlaveMessageService",
        "SlaveStatusService",
        "MasterMessageInbound",
        "MasterMessageMutations",
        "MasterMessageWorker",
    ]
    with ExitStack() as stack:
        stack.enter_context(patch.object(MasterChannel, "__init__", return_value=None))
        stack.enter_context(patch("efb_telegram_master.load_channel_config", new=load_config))
        stack.enter_context(patch("efb_telegram_master.ExperimentalFlagsManager", return_value=flag))
        stack.enter_context(patch("efb_telegram_master.DatabaseManager", return_value=database_manager))
        stack.enter_context(patch("efb_telegram_master.channel_composition.TelegramBotManager", return_value=bot_manager))
        stack.enter_context(patch("efb_telegram_master.channel_composition.HistoryReplayWorker", return_value=history_replay))
        for dependency in dependencies:
            stack.enter_context(patch(f"efb_telegram_master.channel_composition.{dependency}"))
        channel = TelegramChannel()

    assert channel.slave_message_deliveries is database_manager.slave_message_deliveries
    history_replay.resume.assert_called_once_with()
    channel.chat_associations.get_topic_slaves.return_value = [("tests.slave", 7)]
    channel.msglog_scan.schedule.return_value = "started"
    message = Mock(chat=Mock(id=100, is_forum=True), from_user=Mock(id=1), message_thread_id=None)
    channel.command_service.sync_msglog(Update(update_id=1, message=message), Mock())

    assert channel.command_service.admins == [1]
    channel.chat_associations.get_topic_slaves.assert_called_once_with(100)
    channel.msglog_scan.schedule.assert_called_once_with(100)
    api.send_message.assert_called_once_with(100, text="MsgLog sync started for this group.")

    class PrefixTranslations(NullTranslations):
        def gettext(self, message: str) -> str:
            return f"translated:{message}"

    locale_update = Update(update_id=2, message=Mock(chat=Mock(id=100), from_user=Mock(id=1, language_code="fr")))
    with patch("efb_telegram_master.channel_commands.translation", return_value=PrefixTranslations()):
        channel.locale_state.update(locale_update, channel.logger)

    assert channel.locale == "fr"
    assert channel._("locale") == "translated:locale"
    assert channel.command_service._("locale") == "translated:locale"
    assert channel.locale_state.gettext("locale") == "translated:locale"


def test_link_chat_raw_message_override_forces_behavior_when_args_are_truncated(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(104))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    channel.chat_associations.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))
    update = _build_link_update(bot_group)
    update.effective_message.text = f"/start {token} true"

    sent_message = _sent_link_message(bot_group, 503)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as migrate_chat_history,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


def test_resolve_command_args_falls_back_to_raw_message_text():
    args = TelegramCommandService.resolve_command_args("/start token true", ["token"])

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

    with patch.object(channel.link_completion, "complete") as link_chat:
        channel.command_service.start(update, context)

    link_chat.assert_called_once_with(update, ["token", "true"])


def test_history_migration_dispatches_persisted_entries_through_telegram_api():
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(), copy_message=Mock()), Mock(), HistoryMigrationRepository(), Mock(), Mock())
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

        assert manager.process_target(manager.history_migrations.get_next_target()) is True

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

        assert manager.process_target(failed_entry) is False
        assert HistoryMigrationEntry.select().where(HistoryMigrationEntry.id == failed_entry.id).exists()
    finally:
        test_database.close()
        database.initialize(original_database)


def test_pending_history_migrations_send_entries_in_position_order_and_delete_each_success():
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(), copy_message=Mock()), Mock(), HistoryMigrationRepository(), Mock(), Mock())
    manager.REPLAY_PAGE_SIZE = 2
    try:
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.insert_many(
            [
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.20", "formatted_text": "first\n", "position": 0},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.21", "formatted_text": "second\n", "position": 1},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.22", "formatted_text": None, "position": 2},
            ]
        ).execute()

        pages = []
        original_get_entries_page = manager.history_migrations.get_entries_page

        def get_entries_page(*args):
            page = original_get_entries_page(*args)
            pages.append(page)
            return page

        with patch.object(manager.history_migrations, "get_entries_page", side_effect=get_entries_page) as get_entries_page_mock:
            manager.process_pending()

        assert [call.kwargs["text"] for call in manager.bot.send_message.call_args_list] == ["first\n", "second\n"]
        manager.bot.copy_message.assert_called_once_with(chat_id=12345, from_chat_id=10, message_id=22, disable_notification=True)
        assert HistoryMigrationEntry.select().count() == 0
        assert [len(page) for page in pages] == [2, 1, 0]
        assert get_entries_page_mock.call_args_list == [
            (("tests.mocks.slave.chat", 12345, None, None, 2), {}),
            (("tests.mocks.slave.chat", 12345, None, (1, 2), 2), {}),
            (("tests.mocks.slave.chat", 12345, None, (2, 3), 2), {}),
        ]
    finally:
        test_database.close()
        database.initialize(original_database)


def test_pending_history_migrations_keep_the_failed_entry_and_remaining_boundary():
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(side_effect=[None, RuntimeError("Telegram failed")]), copy_message=Mock()), Mock(), HistoryMigrationRepository(), Mock(), Mock())
    try:
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.insert_many(
            [
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.20", "formatted_text": "first\n", "position": 0},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.21", "formatted_text": "second\n", "position": 1},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.22", "formatted_text": None, "position": 2},
            ]
        ).execute()

        manager.process_pending()

        assert [entry.source_master_msg_id for entry in HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.position)] == ["10.21", "10.22"]
        manager.bot.copy_message.assert_not_called()
    finally:
        test_database.close()
        database.initialize(original_database)


def test_queue_history_migration_entries_persists_pending_rows():
    manager = HistoryReplayWorker(Mock(), Mock(), Mock(), Mock(), Mock())
    base_time = datetime.now()
    text_log = Mock()
    text_log.master_msg_id = "10.20"
    text_log.text = "hello"
    text_log.media_type = "Text"
    text_log.time = base_time
    message_reconstructor = Mock()
    message_reconstructor.build.return_value = SimpleNamespace(author=SimpleNamespace(display_name="author"))
    media_log = Mock()
    media_log.master_msg_id = "10.21"
    media_log.text = ""
    media_log.media_type = "Photo"
    media_log.time = base_time + timedelta(seconds=1)
    manager.SOURCE_PAGE_SIZE = 1
    manager.msglogs.get_recent_message_page.side_effect = [[text_log], [media_log], []]
    manager.message_reconstructor = message_reconstructor

    def replace_entries(_slave_chat_id, _target_chat_id, _thread_id, entries):
        queued_entries.extend(entries)
        return len(queued_entries)

    queued_entries = []
    manager.history_migrations.replace_entries.side_effect = replace_entries

    queued_count = manager.queue_entries(
        "tests.mocks.slave.chat",
        12345,
    )

    assert queued_count == 2
    assert len(queued_entries) == 2
    assert queued_entries[0]["source_master_msg_id"] == "10.20"
    assert queued_entries[0]["formatted_text"] == f"*author* `{base_time.strftime('%Y-%m-%d %H:%M')}`\nhello\n\n"
    assert queued_entries[1]["source_master_msg_id"] == "10.21"
    assert queued_entries[1]["formatted_text"] is None
    assert manager.msglogs.get_recent_message_page.call_args_list == [
        (("tests.mocks.slave.chat", None, 1), {}),
        (("tests.mocks.slave.chat", (base_time, "10.20"), 1), {}),
        (("tests.mocks.slave.chat", (base_time + timedelta(seconds=1), "10.21"), 1), {}),
    ]
    manager.msglogs.get_recent_messages.assert_not_called()


def test_history_migration_deletes_zero_call_entry_without_queueing():
    manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(), copy_message=Mock()), Mock(), Mock(), Mock(), Mock())
    entry = SimpleNamespace(
        id=8,
        slave_chat_id="tests.mocks.slave.chat",
        target_chat_id="12345",
        message_thread_id=None,
        source_master_msg_id="",
        formatted_text="",
        position=0,
    )
    manager.history_migrations = SimpleNamespace(
        get_entries_page=Mock(side_effect=[[entry], []]),
        delete_entry=Mock(),
    )
    processed = manager.process_target(entry)

    assert processed is True
    manager.bot.send_message.assert_not_called()
    manager.bot.copy_message.assert_not_called()
    manager.history_migrations.delete_entry.assert_called_once_with(8)
    manager.logger.info.assert_any_call("History migration entry %d completed 0 calls", 8)


def test_history_migration_replacement_stages_source_before_acquiring_sqlite_writer_lock(tmp_path):
    original_database = database.obj
    database_path = tmp_path / "history.db"
    test_database = SqliteDatabase(database_path, pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    database.initialize(test_database)
    test_database.connect()
    repository = HistoryMigrationRepository()
    source_staged = threading.Event()
    continue_source = threading.Event()
    errors = []
    target = {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.20", "formatted_text": "old", "position": 0}
    replacement = {**target, "source_master_msg_id": "10.21", "formatted_text": "first"}
    try:
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.create(**target)

        def entries():
            yield replacement
            source_staged.set()
            assert continue_source.wait(5)
            yield {**replacement, "source_master_msg_id": "10.22", "formatted_text": "second", "position": 1}

        def replace() -> None:
            try:
                test_database.connect(reuse_if_open=True)
                repository.replace_entries("tests.mocks.slave.chat", 12345, None, entries())
            except BaseException as error:
                errors.append(error)
            finally:
                if not test_database.is_closed():
                    test_database.close()

        worker = threading.Thread(target=replace)
        worker.start()
        assert source_staged.wait(5)

        concurrent_database = SqliteDatabase(database_path, pragmas={"busy_timeout": 250})
        concurrent_database.connect()
        try:
            concurrent_database.execute_sql(
                "INSERT INTO historymigrationentry (slave_chat_id, target_chat_id, source_master_msg_id, formatted_text, position, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("tests.mocks.slave.chat", "12345", "10.23", "concurrent", 2, datetime.now()),
            )
        finally:
            concurrent_database.close()

        continue_source.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors
        assert [entry.source_master_msg_id for entry in HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.position)] == ["10.21", "10.22"]
    finally:
        continue_source.set()
        test_database.close()
        database.initialize(original_database)


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

    recent = channel.msglogs.get_recent_messages(slave_uid, limit=0)
    assert [row.slave_message_id for row in recent] == ["slave-0", "slave-1", "slave-2"]
    first_page = channel.msglogs.get_recent_message_page(slave_uid, None, 2)
    second_page = channel.msglogs.get_recent_message_page(slave_uid, (first_page[-1].time, first_page[-1].master_msg_id), 2)
    assert [row.slave_message_id for row in first_page] == ["slave-0", "slave-1"]
    assert [row.slave_message_id for row in second_page] == ["slave-2"]

    for row in MsgLog.select().where(MsgLog.slave_origin_uid == slave_uid):
        row.delete_instance()
