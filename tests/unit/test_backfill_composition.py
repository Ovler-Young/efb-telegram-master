from contextlib import ExitStack
from gettext import NullTranslations
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot.channel import MasterChannel
from telegram import Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.mtproto import MTProtoConfig


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
