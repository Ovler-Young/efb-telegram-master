import time
from types import SimpleNamespace
from unittest.mock import ANY, Mock

import pytest

from efb_telegram_master import TelegramChannel
from efb_telegram_master.history_replay import HistoryReplayShutdownTimeout
from efb_telegram_master.outbound_types import OutboundShutdownTimeout
from efb_telegram_master.runtime.bot_manager import TelegramResourceShutdownError
from efb_telegram_master.transport.telegram_api import TelegramAPI


def test_api_resource_shutdown_stops_metrics_server_under_its_current_owner() -> None:
    bot_pool = Mock()
    bot_pool.begin_shutdown.return_value = ()
    bot_pool.wait_for_shutdown.return_value = ()
    api = TelegramAPI(SimpleNamespace(), Mock(), Mock(), bot_pool)
    metrics_server = Mock(thread=Mock(is_alive=Mock(return_value=False)))
    api.bind_metrics_server(metrics_server)

    deadline = time.monotonic() + 2.5
    assert api.stop_delivery_resources(deadline) == ()

    api._outbound_queue.stop.assert_called_once_with(deadline)
    bot_pool.begin_shutdown.assert_called_once_with()
    bot_pool.wait_for_shutdown.assert_called_once()
    assert api._metrics_server is None


def test_api_timeout_still_stops_other_delivery_resources() -> None:
    queue = Mock()
    queue.stop.side_effect = OutboundShutdownTimeout("blocked send")
    bot_pool = Mock()
    bot_pool.begin_shutdown.return_value = ()
    bot_pool.wait_for_shutdown.return_value = ()
    api = TelegramAPI(SimpleNamespace(), Mock(), queue, bot_pool)
    metrics_server = Mock(thread=Mock(is_alive=Mock(return_value=False)))
    api.bind_metrics_server(metrics_server)

    errors = api.stop_delivery_resources(time.monotonic() + 2.5)

    assert isinstance(errors[0], OutboundShutdownTimeout)
    metrics_server.stop.assert_called_once()
    bot_pool.begin_shutdown.assert_called_once_with()
    bot_pool.wait_for_shutdown.assert_called_once()


def test_channel_shutdown_error_stops_owned_workers() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.logger = Mock()
    channel.rpc_utilities = Mock()
    channel.bot_manager = Mock()
    channel.bot_manager.stop_channel_resources.side_effect = TelegramResourceShutdownError((OutboundShutdownTimeout("blocked send"),))
    channel.master_message_worker = Mock(stop_worker=Mock(return_value=()))
    channel.db = Mock()

    with pytest.raises(TelegramResourceShutdownError, match="blocked send"):
        channel.stop_polling()

    assert channel._stop_polling_called
    channel.master_message_worker.stop_worker.assert_called_once_with(deadline=ANY)
    channel.db.stop_worker.assert_not_called()


def test_channel_stops_master_messages_before_outbound_delivery() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.logger = Mock()
    channel.rpc_utilities = Mock()
    channel.bot_manager = Mock()
    channel.master_message_worker = Mock(stop_worker=Mock(return_value=()))
    channel.db = Mock()

    events: list[str] = []
    channel.master_message_worker.stop_worker.side_effect = lambda **_kwargs: events.append("master") or ()
    channel.bot_manager.stop_channel_resources.side_effect = lambda *_args: events.append("outbound")

    channel.stop_polling()

    assert events == ["master", "outbound"]


def test_channel_does_not_close_database_while_history_worker_is_blocked() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.logger = Mock()
    channel.rpc_utilities = Mock()
    channel.history_replay = Mock(stop=Mock(return_value=(HistoryReplayShutdownTimeout("target 100"),)))
    channel.bot_manager = Mock()
    channel.master_message_worker = Mock(stop_worker=Mock(return_value=()))
    channel.db = Mock()

    with pytest.raises(TelegramResourceShutdownError, match="target 100"):
        channel.stop_polling()

    channel.master_message_worker.stop_worker.assert_called_once_with(deadline=ANY)
    channel.db.stop_worker.assert_not_called()


def test_channel_retries_blocked_history_shutdown_then_closes_database_once() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.logger = Mock()
    channel.rpc_utilities = Mock()
    timeout = HistoryReplayShutdownTimeout("target 100")
    channel.history_replay = Mock(stop=Mock(side_effect=[(timeout,), (timeout,), (), ()]))
    channel.bot_manager = Mock()
    channel.master_message_worker = Mock(stop_worker=Mock(return_value=()))
    channel.db = Mock()

    with pytest.raises(TelegramResourceShutdownError, match="target 100"):
        channel.stop_polling()
    channel.db.stop_worker.assert_not_called()

    channel.stop_polling()
    channel.stop_polling()

    channel.db.stop_worker.assert_called_once_with()
    channel.bot_manager.stop_channel_resources.assert_called_once_with(ANY)
    assert channel.master_message_worker.stop_worker.call_count == 2


def test_channel_retains_history_shutdown_errors_from_both_failed_attempts() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    first_error = HistoryReplayShutdownTimeout("first attempt")
    retry_error = HistoryReplayShutdownTimeout("retry attempt")
    channel.logger = Mock()
    channel.history_replay = Mock(stop=Mock(side_effect=[(first_error,), (retry_error,)]))
    channel.rpc_utilities = Mock()
    channel.bot_manager = Mock()
    channel.db = Mock()

    errors = channel._stop_non_master_resources(time.monotonic() + 1)

    assert errors == (first_error, retry_error)
    channel.db.stop_worker.assert_not_called()


def test_channel_discards_transient_history_shutdown_error_after_successful_retry() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    first_error = HistoryReplayShutdownTimeout("first attempt")
    channel.logger = Mock()
    channel.history_replay = Mock(stop=Mock(side_effect=[(first_error,), ()]))
    channel.rpc_utilities = Mock()
    channel.bot_manager = Mock()
    channel.db = Mock()

    assert channel._stop_non_master_resources(time.monotonic() + 1) == ()
    channel.db.stop_worker.assert_called_once_with()
