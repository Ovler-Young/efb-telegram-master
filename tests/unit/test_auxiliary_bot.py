from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import telegram.error

from efb_telegram_master.auxiliary_bot import AuxiliaryBot


def test_initialize_sets_identity():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_cls, \
         patch("efb_telegram_master.auxiliary_bot._resolve_bot_result", side_effect=lambda result, runtime: result):
        bot = bot_cls.return_value
        bot.get_me = Mock(return_value=SimpleNamespace(id=123, username="auxbot"))

        aux_bot = AuxiliaryBot("123:token")
        assert aux_bot.initialize() is True
        assert aux_bot.bot_id == 123
        assert aux_bot.username == "auxbot"
        assert aux_bot.disabled is False


def test_initialize_uses_separate_validation_bot():
    primary_bot = Mock()
    validation_bot = Mock()
    primary_bot.get_me = Mock()
    validation_bot.get_me = Mock(return_value=SimpleNamespace(id=123, username="auxbot"))

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot", side_effect=[primary_bot, validation_bot]), \
         patch("efb_telegram_master.auxiliary_bot._resolve_bot_result", side_effect=lambda result, runtime: result):
        aux_bot = AuxiliaryBot("123:token")
        assert aux_bot.initialize() is True
        primary_bot.get_me.assert_not_called()
        validation_bot.get_me.assert_called_once_with()


def test_initialize_disables_bot_on_forbidden():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_cls, \
         patch("efb_telegram_master.auxiliary_bot._resolve_bot_result", side_effect=lambda result, runtime: result):
        bot_cls.return_value.get_me = Mock(side_effect=telegram.error.Forbidden("bad token"))
        aux_bot = AuxiliaryBot("123:token")
        assert aux_bot.initialize() is False
        assert aux_bot.disabled is True


def test_rate_limit_peek_and_reserve():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token", global_limit=3, global_window=10.0, chat_limit=3, chat_window=10.0)

    chat_id = 100
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        assert aux_bot.peek_delay(chat_id) == 0.0
        assert aux_bot.reserve_slot(chat_id) == 0.0
        assert aux_bot.reserve_slot(chat_id) > 0.0

    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        assert aux_bot.peek_delay(chat_id) > 0.0


def test_check_membership_tri_starts_probe_for_unknown():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    with patch.object(aux_bot, "_start_membership_probe") as start_probe:
        assert aux_bot.check_membership_tri(1000) is None

    start_probe.assert_called_once_with(1000)


def test_check_membership_tri_returns_stale_value_while_refreshing():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    with patch("efb_telegram_master.auxiliary_bot.time.time", return_value=1000.0):
        aux_bot.update_membership(2000, True)

    with patch("efb_telegram_master.auxiliary_bot.time.time", return_value=1000.0 + aux_bot.MEMBERSHIP_TTL_MEMBER + 1), \
         patch.object(aux_bot, "_start_membership_probe") as start_probe:
        assert aux_bot.check_membership_tri(2000) is True

    start_probe.assert_called_once_with(2000)


def test_check_membership_sync_waits_for_probe_completion():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    state = {"pending": True, "sleep_calls": 0}

    def fake_start(chat_id):
        with aux_bot._membership_lock:
            aux_bot._pending_probes.add(chat_id)

    def fake_sleep(_):
        state["sleep_calls"] += 1
        with aux_bot._membership_lock:
            aux_bot._pending_probes.clear()
            aux_bot._membership_cache[3000] = (True, 100.0)

    with patch.object(aux_bot, "_start_membership_probe", side_effect=fake_start), \
         patch("efb_telegram_master.auxiliary_bot.time.time", side_effect=[0.0, 0.1, 0.2, 0.3]), \
         patch("efb_telegram_master.auxiliary_bot.time.sleep", side_effect=fake_sleep):
        assert aux_bot.check_membership_sync(3000, timeout=1.0) is True

    assert state["sleep_calls"] == 1


def test_probe_membership_marks_non_member_on_bad_request():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_cls:
        bot = bot_cls.return_value
        bot.get_chat_member.side_effect = telegram.error.BadRequest("not found")
        aux_bot = AuxiliaryBot("123:token")
        aux_bot.bot_id = 123

    aux_bot._probe_membership(4000)
    assert aux_bot.check_membership(4000) is False


def test_mark_disabled_sets_reason():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    aux_bot.mark_disabled("rate limit")
    assert aux_bot.disabled is True
    assert aux_bot._disable_reason == "rate limit"
