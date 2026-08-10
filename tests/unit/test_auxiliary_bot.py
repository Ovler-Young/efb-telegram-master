import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import telegram.error

from efb_telegram_master.auxiliary_bot import AuxiliaryBot


def test_initialize_sets_identity():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_cls:
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

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot", side_effect=[primary_bot, validation_bot]):
        aux_bot = AuxiliaryBot("123:token")
        assert aux_bot.initialize() is True
        primary_bot.get_me.assert_not_called()
        validation_bot.get_me.assert_called_once_with()


def test_local_mode_is_passed_to_primary_and_validation_bots():
    primary_bot = Mock()
    validation_bot = Mock()
    validation_bot.get_me = Mock(return_value=SimpleNamespace(id=123, username="auxbot"))

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot", side_effect=[primary_bot, validation_bot]) as bot_cls:
        aux_bot = AuxiliaryBot(
            "123:token",
            base_url="http://localhost:8081/bot",
            base_file_url="file:///var/lib/telegram-bot-api",
            local_mode=True,
        )
        assert aux_bot.initialize() is True

    assert bot_cls.call_args_list[0].kwargs["local_mode"] is True
    assert bot_cls.call_args_list[1].kwargs["local_mode"] is True


def test_initialize_disables_bot_on_forbidden():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_cls:
        bot_cls.return_value.get_me = Mock(side_effect=telegram.error.Forbidden("bad token"))
        aux_bot = AuxiliaryBot("123:token")
        assert aux_bot.initialize() is False
        assert aux_bot.disabled is True


def test_rate_limit_peek_and_acquire_uses_auxiliary_limiter() -> None:
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    limiter = Mock()
    limiter.peek_delay.return_value = 1.5
    limiter.try_acquire.return_value = False
    aux_bot._rate_limiter = limiter

    assert aux_bot.peek_delay(100) == 1.5
    assert aux_bot.try_acquire_limits(100) is False
    limiter.peek_delay.assert_called_once_with(100)
    limiter.try_acquire.assert_called_once_with(100)


def test_check_membership_tri_starts_probe_for_unknown():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    with patch.object(aux_bot, "_start_membership_probe") as start_probe:
        assert aux_bot.check_membership_tri(1000) is None

    start_probe.assert_called_once_with(1000)


def test_check_membership_tri_returns_unknown_while_refreshing_stale_entry():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    with patch("efb_telegram_master.auxiliary_bot.time.monotonic", return_value=1000.0):
        aux_bot.update_membership(2000, True)

    with patch("efb_telegram_master.auxiliary_bot.time.monotonic", return_value=1000.0 + aux_bot.MEMBERSHIP_TTL_MEMBER + 1), patch.object(aux_bot, "_start_membership_probe") as start_probe:
        assert aux_bot.check_membership_tri(2000) is None

    start_probe.assert_called_once_with(2000)


def test_probe_membership_marks_non_member_on_bad_request():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_cls:
        bot = bot_cls.return_value
        bot.get_chat_member.side_effect = telegram.error.BadRequest("not found")
        aux_bot = AuxiliaryBot("123:token")
        aux_bot.bot_id = 123

    aux_bot._probe_membership(4000)
    assert aux_bot.check_membership_tri(4000) is False


def test_membership_cache_snapshot_counts_cached_and_pending_states():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    aux_bot.update_membership(100, True)
    aux_bot.update_membership(200, False)
    with aux_bot._membership_lock:
        aux_bot._pending_probes.add(300)

    assert aux_bot.get_membership_cache_snapshot() == {
        "member": 1,
        "not_member": 1,
        "unknown_probe_pending": 1,
    }


def test_probe_membership_records_bad_request_metric():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_cls:
        bot = bot_cls.return_value
        bot.get_chat_member.side_effect = telegram.error.BadRequest("not found")
        aux_bot = AuxiliaryBot("123:token")
        aux_bot.bot_id = 123
        aux_bot.username = "botA"
        metrics = Mock()
        aux_bot.bind_metrics(metrics)

    aux_bot._probe_membership(4000)

    metrics.membership_probe.assert_called_once_with(123, "botA", "bad_request")


def test_probe_membership_forbidden_marks_non_member_without_disabling():
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_cls:
        bot = bot_cls.return_value
        bot.get_chat_member.side_effect = telegram.error.Forbidden("bot was kicked")
        aux_bot = AuxiliaryBot("123:token")
        aux_bot.bot_id = 123

    aux_bot._probe_membership(4000)

    assert aux_bot.check_membership_tri(4000) is False
    assert aux_bot.disabled is False


def test_membership_cache_expiry_uses_monotonic_time() -> None:
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    with patch("efb_telegram_master.auxiliary_bot.time.monotonic", return_value=100.0):
        aux_bot.update_membership(100, True)

    with patch("efb_telegram_master.auxiliary_bot.time.monotonic", return_value=100.0 + aux_bot.MEMBERSHIP_TTL_MEMBER + 1), patch.object(aux_bot, "_start_membership_probe") as start_probe:
        assert aux_bot.check_membership_tri(100) is None

    start_probe.assert_called_once_with(100)


def test_membership_probes_are_bounded_for_many_unknown_chats() -> None:
    started = threading.Event()
    release = threading.Event()

    def get_chat_member(_chat_id: int, _bot_id: int) -> SimpleNamespace:
        started.set()
        assert release.wait(1)
        return SimpleNamespace(status="member")

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"), patch.object(AuxiliaryBot, "MAX_PENDING_MEMBERSHIP_PROBES", 2):
        aux_bot = AuxiliaryBot("123:token")
    aux_bot.bot_id = 123
    aux_bot.async_bot.get_chat_member.side_effect = get_chat_member
    for chat_id in range(10):
        assert aux_bot.check_membership_tri(chat_id) is None

    assert started.wait(1)
    with aux_bot._membership_lock:
        assert len(aux_bot._pending_probes) == 2
    release.set()
    aux_bot.shutdown(time.monotonic() + 1)


def test_shutdown_rejects_new_membership_probes() -> None:
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        aux_bot = AuxiliaryBot("123:token")

    aux_bot.begin_membership_shutdown()
    assert aux_bot.check_membership_tri(100) is None
    assert aux_bot.has_pending_probes() is False


def test_network_exception_is_not_cached_as_non_membership() -> None:
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_cls:
        bot_cls.return_value.get_chat_member.side_effect = OSError("network unavailable")
        aux_bot = AuxiliaryBot("123:token")
        aux_bot.bot_id = 123

    aux_bot._probe_membership(4000)

    with aux_bot._membership_lock:
        assert 4000 not in aux_bot._membership_cache


def test_membership_cache_evicts_the_least_recently_used_entry() -> None:
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"), patch.object(AuxiliaryBot, "MAX_MEMBERSHIP_CACHE_ENTRIES", 2):
        aux_bot = AuxiliaryBot("123:token")

    aux_bot.update_membership(1, True)
    aux_bot.update_membership(2, True)
    assert aux_bot.check_membership_tri(1) is True
    aux_bot.update_membership(3, True)

    with aux_bot._membership_lock:
        assert list(aux_bot._membership_cache) == [1, 3]


def test_shutdown_cancels_queued_membership_probes() -> None:
    started = threading.Event()
    release = threading.Event()

    def get_chat_member(_chat_id: int, _bot_id: int) -> SimpleNamespace:
        started.set()
        assert release.wait(1)
        return SimpleNamespace(status="member")

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"), patch.object(AuxiliaryBot, "MEMBERSHIP_PROBE_WORKERS", 1), patch.object(AuxiliaryBot, "MAX_PENDING_MEMBERSHIP_PROBES", 2):
        aux_bot = AuxiliaryBot("123:token")
    aux_bot.bot_id = 123
    aux_bot.async_bot.get_chat_member.side_effect = get_chat_member

    aux_bot.check_membership_tri(1)
    assert started.wait(1)
    aux_bot.check_membership_tri(2)
    aux_bot.begin_membership_shutdown()

    with aux_bot._membership_lock:
        assert aux_bot._pending_probes == {1}
    release.set()
    aux_bot.wait_for_membership_shutdown(time.monotonic() + 1)
    assert aux_bot.has_pending_probes() is False
