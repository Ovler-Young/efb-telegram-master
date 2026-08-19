import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from types import SimpleNamespace
from unittest.mock import Mock, patch

import telegram.error

from efb_telegram_master.auxiliary_bot import AuxiliaryBot
from efb_telegram_master.membership_lifecycle import MembershipLifecycle


def _wait_for_probe(auxiliary: AuxiliaryBot) -> None:
    deadline = time.monotonic() + 1
    while auxiliary.has_pending_probes() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not auxiliary.has_pending_probes()


def test_initialize_sets_identity() -> None:
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_class:
        bot_class.return_value.get_me = Mock(return_value=SimpleNamespace(id=123, username="auxbot"))
        auxiliary = AuxiliaryBot("123:token")

        assert auxiliary.initialize()

    assert (auxiliary.bot_id, auxiliary.username, auxiliary.disabled) == (123, "auxbot", False)


def test_initialize_disables_forbidden_bot() -> None:
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot") as bot_class:
        bot_class.return_value.get_me = Mock(side_effect=telegram.error.Forbidden("bad token"))
        auxiliary = AuxiliaryBot("123:token")

        assert not auxiliary.initialize()

    assert auxiliary.disabled


def test_rate_limit_delegation_uses_auxiliary_limiter() -> None:
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        auxiliary = AuxiliaryBot("123:token")
    limiter = Mock(peek_delay=Mock(return_value=1.5), try_acquire=Mock(return_value=False))
    auxiliary._rate_limiter = limiter

    assert auxiliary.peek_delay(100) == 1.5
    assert not auxiliary.try_acquire_limits(100)
    limiter.peek_delay.assert_called_once_with(100)
    limiter.try_acquire.assert_called_once_with(100)


def test_auxiliary_delegates_membership_lifecycle_to_collaborator() -> None:
    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        auxiliary = AuxiliaryBot("123:token")

    assert isinstance(auxiliary._membership_lifecycle, MembershipLifecycle)
    auxiliary.update_membership(100, True)

    assert auxiliary.check_membership_tri(100) is True
    assert auxiliary.get_membership_cache_snapshot() == {"member": 1, "not_member": 0, "unknown_probe_pending": 0}


def test_unknown_membership_admits_one_probe_and_records_result() -> None:
    started = threading.Event()
    release = threading.Event()

    def get_chat_member(_chat_id: int, _bot_id: int) -> SimpleNamespace:
        started.set()
        assert release.wait(1)
        return SimpleNamespace(status="member")

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        auxiliary = AuxiliaryBot("123:token")
    auxiliary.bot_id = 123
    auxiliary.async_bot.get_chat_member.side_effect = get_chat_member
    try:
        assert auxiliary.check_membership_tri(1000) is None
        assert started.wait(1)
        assert auxiliary.check_membership_tri(1000) is None
        assert auxiliary.async_bot.get_chat_member.call_count == 1
        release.set()
        _wait_for_probe(auxiliary)
        assert auxiliary.check_membership_tri(1000) is True
    finally:
        release.set()
        auxiliary.wait_for_membership_shutdown(time.monotonic() + 1)


def test_membership_probe_timeout_keeps_membership_unknown() -> None:
    class Runtime:
        def call(self, coroutine, *, timeout=None):
            coroutine.close()
            assert timeout == AuxiliaryBot.MEMBERSHIP_PROBE_TIMEOUT
            raise FutureTimeoutError()

    async def get_chat_member(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(status="member")

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        auxiliary = AuxiliaryBot("123:token")
    auxiliary.bot_id = 123
    auxiliary._runtime = Runtime()
    auxiliary.async_bot.get_chat_member.side_effect = get_chat_member

    assert auxiliary.check_membership_tri(4000) is None
    _wait_for_probe(auxiliary)
    assert auxiliary.get_membership_cache_snapshot() == {"member": 0, "not_member": 0, "unknown_probe_pending": 0}
    auxiliary.wait_for_membership_shutdown(time.monotonic() + 1)


def test_shutdown_cancels_queued_work_and_waits_for_running_worker() -> None:
    started = threading.Event()
    release = threading.Event()

    def get_chat_member(_chat_id: int, _bot_id: int) -> SimpleNamespace:
        started.set()
        assert release.wait(1)
        return SimpleNamespace(status="member")

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"), patch.object(AuxiliaryBot, "MEMBERSHIP_PROBE_WORKERS", 1), patch.object(AuxiliaryBot, "MAX_PENDING_MEMBERSHIP_PROBES", 2):
        auxiliary = AuxiliaryBot("123:token")
    auxiliary.bot_id = 123
    auxiliary.async_bot.get_chat_member.side_effect = get_chat_member
    try:
        assert auxiliary.check_membership_tri(1) is None
        assert started.wait(1)
        assert auxiliary.check_membership_tri(2) is None
        auxiliary.begin_membership_shutdown()
        assert auxiliary.get_membership_cache_snapshot()["unknown_probe_pending"] == 1
        assert not auxiliary.wait_for_membership_shutdown(time.monotonic() + 0.05)
        release.set()
        assert auxiliary.wait_for_membership_shutdown(time.monotonic() + 1)
    finally:
        release.set()
        auxiliary.wait_for_membership_shutdown(time.monotonic() + 1)
