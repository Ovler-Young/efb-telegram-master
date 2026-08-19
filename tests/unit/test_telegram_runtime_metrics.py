from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master import TelegramChannel
from efb_telegram_master.runtime.metrics_runtime import MetricsServer, parse_metrics_config


def test_metrics_server_stop_closes_an_unstarted_server_without_shutdown_or_join() -> None:
    thread = Mock()
    thread.is_alive.return_value = False
    server = Mock()

    MetricsServer(server, thread).stop(1.0)

    server.shutdown.assert_not_called()
    server.server_close.assert_called_once_with()
    thread.join.assert_not_called()


def test_metrics_configuration_defaults_and_disables_invalid_endpoint_options() -> None:
    logger = Mock()

    assert parse_metrics_config({"top_n": None, "host": "0.0.0.0", "port": "9102"}, logger) == (20, ("0.0.0.0", 9102))
    assert parse_metrics_config({"top_n": "3", "host": "127.0.0.1", "port": object()}, logger) == (3, None)
    assert logger.warning.call_count == 2


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("top_n", True, (20, ("127.0.0.1", 9101))),
        ("top_n", False, (20, ("127.0.0.1", 9101))),
        ("port", True, (20, None)),
        ("port", False, (20, None)),
    ],
)
def test_metrics_configuration_rejects_boolean_numeric_values(field, value, expected) -> None:
    logger = Mock()

    assert parse_metrics_config({field: value}, logger) == expected
    logger.warning.assert_called_once()


@pytest.mark.parametrize(
    ("stopping", "dispatches"),
    [(True, False), (False, True)],
    ids=["stop_signal", "running"],
)
def test_channel_dispatch_respects_runtime_state(stopping: bool, dispatches: bool) -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.bot_manager = SimpleNamespace(_stopping=Mock(is_set=Mock(return_value=stopping)))
    channel.message_service = Mock()
    message = Mock()
    channel.message_service.send_message.return_value = message

    assert channel.send_message(message) is message
    if dispatches:
        channel.message_service.send_message.assert_called_once_with(message)
    else:
        channel.message_service.send_message.assert_not_called()
