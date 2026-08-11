from contextlib import ExitStack
from datetime import datetime, timedelta
from gettext import NullTranslations
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ehforwarderbot.channel import MasterChannel
from ehforwarderbot.types import ChatID
from peewee import SqliteDatabase
from telegram import Update

from efb_telegram_master import TelegramChannel, utils
from efb_telegram_master.callback_sessions import ChatListStorage
from efb_telegram_master.channel_commands import TelegramCommandService
from efb_telegram_master.constants import Flags
from efb_telegram_master.history_migration_repository import HistoryMigrationRepository
from efb_telegram_master.history_replay import HistoryReplayWorker
from efb_telegram_master.models import HistoryMigrationEntry, MsgLog, database
from efb_telegram_master.mtproto import MTProtoConfig
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
    channel.callback_sessions.store(storage_key, storage)


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

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    assert storage_key not in channel.link_handler._conversations
    _cleanup_link_state(channel, chat, bot_group)


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

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


@pytest.mark.parametrize("backfill_flag", ["false", "no", "off", "0"])
def test_link_chat_backfill_override_skips_replay(channel, slave, bot_group, backfill_flag):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(130))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
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

    thread.assert_called_once_with(target=manager.process_pending, daemon=True, name="HistoryMigrationResume")
    thread.return_value.start.assert_called_once()


def test_channel_composition_wires_sync_msglog_and_dynamic_locale():
    application = Mock()
    runtime = SimpleNamespace(application=application, as_async_callback=lambda callback: callback)
    api = SimpleNamespace(session_expired=lambda *_args, **_kwargs: None, send_message=Mock())
    bot_manager = SimpleNamespace(api=api, telegram_runtime=runtime, msglog_scan=Mock(), error=Mock())
    database_manager = SimpleNamespace(
        chat_associations=Mock(),
        slave_chat_info=Mock(),
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
        "RPCUtilities",
    ]
    with ExitStack() as stack:
        stack.enter_context(patch.object(MasterChannel, "__init__", return_value=None))
        stack.enter_context(patch("efb_telegram_master.load_channel_config", new=load_config))
        stack.enter_context(patch("efb_telegram_master.ExperimentalFlagsManager", return_value=flag))
        stack.enter_context(patch("efb_telegram_master.DatabaseManager", return_value=database_manager))
        stack.enter_context(patch("efb_telegram_master.TelegramBotManager", return_value=bot_manager))
        stack.enter_context(patch("efb_telegram_master.HistoryReplayWorker", return_value=history_replay))
        for dependency in dependencies:
            stack.enter_context(patch(f"efb_telegram_master.{dependency}"))
        channel = TelegramChannel()

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
    text_log.build_etm_msg.return_value = SimpleNamespace(author=SimpleNamespace(display_name="author"))
    media_log = Mock()
    media_log.master_msg_id = "10.21"
    media_log.text = ""
    media_log.media_type = "Photo"
    media_log.time = base_time + timedelta(seconds=1)
    manager.msglogs.get_recent_messages.return_value = [text_log, media_log]
    manager.history_migrations.replace_entries.return_value = 2

    queued_count = manager.queue_entries(
        "tests.mocks.slave.chat",
        12345,
    )

    entries = manager.history_migrations.replace_entries.call_args.args[3]
    assert queued_count == 2
    assert len(entries) == 2
    assert entries[0]["source_master_msg_id"] == "10.20"
    assert entries[0]["formatted_text"] == f"*author* `{base_time.strftime('%Y-%m-%d %H:%M')}`\nhello\n\n"
    assert entries[1]["source_master_msg_id"] == "10.21"
    assert entries[1]["formatted_text"] is None


def test_history_migration_deletes_zero_call_entry_without_queueing():
    manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(), copy_message=Mock()), Mock(), Mock(), Mock(), Mock())
    entry = SimpleNamespace(
        id=8,
        slave_chat_id="tests.mocks.slave.chat",
        target_chat_id="12345",
        message_thread_id=None,
        source_master_msg_id="",
        formatted_text="",
    )
    manager.history_migrations = SimpleNamespace(
        get_entries=Mock(return_value=[entry]),
        delete_entry=Mock(),
    )
    processed = manager.process_target(entry)

    assert processed is True
    manager.bot.send_message.assert_not_called()
    manager.bot.copy_message.assert_not_called()
    manager.history_migrations.delete_entry.assert_called_once_with(8)
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

    recent = channel.msglogs.get_recent_messages(slave_uid, limit=0)
    assert [row.slave_message_id for row in recent] == ["slave-0", "slave-1", "slave-2"]

    for row in MsgLog.select().where(MsgLog.slave_origin_uid == slave_uid):
        row.delete_instance()
