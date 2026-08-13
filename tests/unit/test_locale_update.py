from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master import TelegramChannel


def test_locale_update_logs_event_with_normalized_locale(monkeypatch) -> None:
    channel = SimpleNamespace(
        flag=lambda name: name == "auto_locale",
        locale=None,
        logger=Mock(),
        translator=None,
    )
    translation = Mock()
    monkeypatch.setattr("efb_telegram_master.gettext.translation", Mock(return_value=translation))
    update = SimpleNamespace(effective_user=SimpleNamespace(language_code="en-US"))

    TelegramChannel.update_locale(channel, update, Mock())

    assert channel.locale == "en-US"
    assert channel.translator is translation
    channel.logger.info.assert_called_once_with(
        "Telegram locale updated",
        extra={"event": "telegram_channel.locale_updated", "locale": "en_US"},
    )
