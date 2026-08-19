import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest
from ehforwarderbot.channel import MasterChannel
from telegram import Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.config.runtime import RuntimeConfiguration
from efb_telegram_master.master_message import MasterMessageWorker
from efb_telegram_master.runtime.bot_manager import TelegramBotManager
from efb_telegram_master.transport.telegram_api import TelegramAPI


def test_bot_manager_constructor_failure_preserves_dispatcher_error_and_reports_cleanup_errors(caplog) -> None:
    channel = SimpleNamespace(config=RuntimeConfiguration.from_mapping({"token": "token", "admins": [1], "outbound": {"max_pending": 17}}), db=Mock())
    async_runtime = Mock()

    def consume_runtime_call(coroutine, **_kwargs):
        coroutine.close()

    async_runtime.call.side_effect = consume_runtime_call
    runtime = Mock(async_runtime=async_runtime, bot=Mock())
    dispatcher_error = RuntimeError("dispatcher registration failed")
    delivery_error = RuntimeError("delivery shutdown failed")
    runtime_error = RuntimeError("runtime shutdown failed")
    runtime.add_base_dispatchers.side_effect = dispatcher_error
    runtime.stop.side_effect = runtime_error

    with (
        patch("efb_telegram_master.runtime.bot_manager.build_telegram_polling_runtime", return_value=runtime),
        patch("efb_telegram_master.runtime.bot_manager.build_bot_pool", return_value=None),
        patch("efb_telegram_master.runtime.bot_manager.OutboundQueue") as queue_type,
        patch("efb_telegram_master.runtime.bot_manager.configure_runtime_metrics", return_value=(None, None)),
        patch.object(TelegramAPI, "begin_delivery_shutdown", return_value=(delivery_error,)) as begin_delivery,
        patch.object(TelegramAPI, "finish_delivery_shutdown", return_value=()) as finish_delivery,
    ):
        with pytest.raises(RuntimeError, match="dispatcher registration failed"):
            TelegramBotManager(
                channel,
                Mock(),
                Mock(),
                Mock(),
                TelegramChannel.channel_id,
                lambda: 0,
                lambda: False,
                lambda text: text,
                lambda singular, _plural, _count: singular,
                Mock(),
            )

    queue_type.return_value.start.assert_called_once_with()
    assert queue_type.call_args.kwargs["max_pending"] == 17
    begin_delivery.assert_called_once_with(ANY)
    finish_delivery.assert_called_once_with(ANY)
    runtime.stop.assert_called_once_with(ANY)
    assert "Telegram delivery resource did not stop after initialization failed: delivery shutdown failed" in caplog.text
    assert "Failed to stop the Telegram runtime after initialization failed." in caplog.text


def test_channel_constructor_stops_database_when_bot_manager_creation_fails() -> None:
    database_manager = SimpleNamespace(
        chat_associations=Mock(),
        slave_chat_info=Mock(),
        slave_message_deliveries=Mock(),
        msglogs=Mock(),
        history_migrations=Mock(),
        msglog_ingestion=Mock(),
        _base_path="/tmp",
        stop_worker=Mock(),
    )

    with (
        patch.object(MasterChannel, "__init__", return_value=None),
        patch("efb_telegram_master.load_channel_config", return_value=RuntimeConfiguration.from_mapping({"token": "token", "admins": [1]})),
        patch("efb_telegram_master.ExperimentalFlagsManager", return_value=Mock()),
        patch("efb_telegram_master.DatabaseManager", return_value=database_manager),
        patch("efb_telegram_master.runtime.channel_composition.TelegramBotManager", side_effect=RuntimeError("bot setup failed")),
        patch("efb_telegram_master.runtime.channel_composition.MTProtoClient", return_value=Mock()),
        patch("efb_telegram_master.runtime.channel_composition.ChatObjectCacheManager", return_value=Mock()),
        patch("efb_telegram_master.runtime.channel_composition.ChatDestinationCache", return_value=Mock()),
        patch("efb_telegram_master.runtime.channel_composition.TelegramChatID", return_value=Mock()),
    ):
        with pytest.raises(RuntimeError, match="bot setup failed"):
            TelegramChannel()

    database_manager.stop_worker.assert_called_once_with()


def test_channel_constructor_stops_started_history_replay_when_handler_registration_fails() -> None:
    application = Mock()
    runtime = SimpleNamespace(application=application, as_async_callback=lambda callback: callback)
    api = SimpleNamespace(session_expired=lambda *_args, **_kwargs: None, send_message=Mock())
    bot_manager = SimpleNamespace(api=api, telegram_runtime=runtime, msglog_scan=Mock(), error=Mock(), stop_channel_resources=Mock())
    history_migrations = SimpleNamespace(has_pending_entries=lambda: True, get_next_target=lambda: None)
    database_manager = SimpleNamespace(
        chat_associations=Mock(),
        slave_chat_info=Mock(),
        slave_message_deliveries=Mock(),
        msglogs=Mock(),
        history_migrations=history_migrations,
        msglog_ingestion=Mock(),
        _base_path="/tmp",
        stop_worker=Mock(),
    )
    flag = Mock(side_effect=lambda name: {"chats_per_page": 10, "multiple_slave_chats": False, "topic_group": 0}.get(name, False))
    dependencies = (
        "MTProtoClient",
        "ChatObjectCacheManager",
        "ChatDestinationCache",
        "TopicGroupService",
        "CommandsManager",
        "MasterMessageDelivery",
        "CallbackSessionStore",
        "RecipientSuggestionService",
        "LinkActionService",
        "LinkService",
        "LinkCompletionService",
        "ChatHeadService",
    )
    history_replay = Mock(stop=Mock(return_value=()))
    application.add_handler.side_effect = [None] * 6 + [RuntimeError("handler registration failed")]
    with ExitStack() as stack:
        stack.enter_context(patch.object(MasterChannel, "__init__", return_value=None))
        stack.enter_context(patch("efb_telegram_master.load_channel_config", return_value=RuntimeConfiguration.from_mapping({"token": "token", "admins": [1]})))
        stack.enter_context(patch("efb_telegram_master.ExperimentalFlagsManager", return_value=flag))
        stack.enter_context(patch("efb_telegram_master.DatabaseManager", return_value=database_manager))
        stack.enter_context(patch("efb_telegram_master.runtime.channel_composition.TelegramBotManager", return_value=bot_manager))
        stack.enter_context(patch("efb_telegram_master.runtime.channel_composition.HistoryReplayWorker", return_value=history_replay))
        for dependency in dependencies:
            stack.enter_context(patch(f"efb_telegram_master.runtime.channel_composition.{dependency}"))

        with pytest.raises(RuntimeError, match="handler registration failed"):
            TelegramChannel()

    history_replay.resume.assert_called_once_with()
    history_replay.stop.assert_called_once_with(ANY)
    bot_manager.stop_channel_resources.assert_called_once_with(ANY)
    database_manager.stop_worker.assert_called_once_with()


def test_channel_rpc_bind_failure_stops_before_component_startup() -> None:
    database_manager = SimpleNamespace(
        chat_associations=Mock(),
        slave_chat_info=Mock(),
        slave_message_deliveries=Mock(),
        msglogs=Mock(),
        history_migrations=Mock(),
        msglog_ingestion=Mock(),
        _base_path="/tmp",
        stop_worker=Mock(),
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(MasterChannel, "__init__", return_value=None))
        stack.enter_context(
            patch("efb_telegram_master.load_channel_config", return_value=RuntimeConfiguration.from_mapping({"token": "token", "admins": [1], "rpc": {"server": "127.0.0.1", "port": 0}}))
        )
        stack.enter_context(patch("efb_telegram_master.runtime.rpc_utils._ThreadedXMLRPCServer", side_effect=OSError("bind failed")))
        stack.enter_context(patch("efb_telegram_master.DatabaseManager", return_value=database_manager))
        bot_manager = stack.enter_context(patch("efb_telegram_master.runtime.channel_composition.TelegramBotManager"))

        with pytest.raises(OSError, match="bind failed"):
            TelegramChannel()

    bot_manager.assert_not_called()
    database_manager.stop_worker.assert_called_once_with()


def test_master_message_worker_shutdown_suppresses_an_in_flight_delivery_failure() -> None:
    runtime = SimpleNamespace(application=SimpleNamespace(add_handler=Mock()), as_async_callback=lambda callback: callback)
    bot = Mock()
    update = SimpleNamespace(effective_message=SimpleNamespace(chat=SimpleNamespace(id=1)))
    inbound = Mock()
    started = threading.Event()
    release = threading.Event()

    def fail_after_shutdown(*_args) -> None:
        started.set()
        assert release.wait(1)
        raise RuntimeError("cancelled delivery")

    inbound.msg.side_effect = fail_after_shutdown
    worker = MasterMessageWorker(runtime, bot, inbound, Mock(), lambda text: text, Mock())
    worker.message_queue.put((update, Mock()))
    assert started.wait(1)
    stopped = threading.Thread(target=worker.stop_worker)

    try:
        stopped.start()
        deadline = time.monotonic() + 1
        while not worker._stopping.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker._stopping.is_set()
        release.set()
        stopped.join(1)
        assert not stopped.is_alive()
        assert not worker.message_worker_thread.is_alive()
        bot.send_message.assert_not_called()
        worker.enqueue_message(Update(1), Mock())
        assert worker.message_queue.empty()
        bot.send_message.assert_not_called()
    finally:
        release.set()
        stopped.join(1)
        worker.stop_worker()
