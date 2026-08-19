from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master import TelegramChannel


def test_channel_stopping_gate_drops_messages_and_statuses() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.message_service = Mock()
    channel.status_service = Mock()
    channel.bot_manager = SimpleNamespace(_stopping=False)
    channel._stop_polling_called = True

    message = SimpleNamespace(uid="late")
    assert TelegramChannel.send_message(channel, message) is message
    assert TelegramChannel.send_status(channel, SimpleNamespace()) is None
    channel.message_service.send_message.assert_not_called()
    channel.status_service.send_status.assert_not_called()


def test_channel_manager_stopping_gate_drops_message() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.message_service = Mock()
    channel.bot_manager = SimpleNamespace(_stopping=True)
    channel._stop_polling_called = False

    message = SimpleNamespace(uid="late")
    assert TelegramChannel.send_message(channel, message) is message
    channel.message_service.send_message.assert_not_called()
