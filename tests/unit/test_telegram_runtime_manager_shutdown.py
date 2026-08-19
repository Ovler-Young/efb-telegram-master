import threading
import time
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest

from efb_telegram_master.auxiliary_bot import AuxiliaryBot, MembershipProbeShutdownTimeout
from efb_telegram_master.bot_manager import TelegramBotManager, TelegramResourceShutdownError
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.msglog_scan import MsgLogScanShutdownTimeout
from efb_telegram_master.outbound_types import OutboundShutdownTimeout
from efb_telegram_master.transport.telegram_api import TelegramAPI


def _manager(api: Mock, runtime: Mock) -> TelegramBotManager:
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager._stopping = Mock()
    manager.logger = Mock()
    manager.api = api
    manager.telegram_runtime = runtime
    return manager


def test_manager_retries_membership_join_after_stopping_runtime() -> None:
    api, runtime = Mock(), Mock()
    membership_error = MembershipProbeShutdownTimeout("bot 10")
    api.begin_delivery_shutdown.return_value = ()
    api.finish_delivery_shutdown.side_effect = ((membership_error,), ())
    manager = _manager(api, runtime)

    with pytest.raises(TelegramResourceShutdownError, match="bot 10"):
        manager.stop_channel_resources()
    manager.stop_channel_resources()

    assert runtime.stop.call_count == 2
    assert api.finish_delivery_shutdown.call_count == 2


def test_manager_runtime_stop_releases_a_real_membership_worker() -> None:
    started = threading.Event()
    released = threading.Event()

    class Runtime:
        def call(self, coroutine, *, timeout):
            coroutine.close()
            started.set()
            released.wait()
            return SimpleNamespace(status="member")

        def stop(self, _deadline: float | None = None) -> None:
            released.set()

    async def get_chat_member(*_args):
        return SimpleNamespace(status="member")

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        auxiliary = AuxiliaryBot("123:token")
    auxiliary.bot_id = 10
    auxiliary._runtime = Runtime()
    auxiliary.async_bot.get_chat_member.side_effect = get_chat_member
    assert auxiliary.check_membership_tri(4000) is None
    assert started.wait(1)

    api = TelegramAPI(SimpleNamespace(), Mock(), Mock(), BotPool([auxiliary]))
    manager = _manager(api, auxiliary._runtime)
    manager.SHUTDOWN_JOIN_GRACE = 0.05
    manager.SHUTDOWN_DRAIN_TIMEOUT = 1.0

    try:
        started_at = time.monotonic()
        manager.stop_channel_resources()
        assert time.monotonic() - started_at < 1.0
        assert not any(thread.is_alive() for thread in auxiliary._membership_probe_workers)
    finally:
        released.set()
        auxiliary.wait_for_membership_shutdown(time.monotonic() + 1)


def test_manager_aggregates_outbound_and_persistent_membership_failures() -> None:
    api, runtime = Mock(), Mock()
    outbound_error = OutboundShutdownTimeout("outbound")
    membership_error = MembershipProbeShutdownTimeout("bot 10")
    api.begin_delivery_shutdown.return_value = (outbound_error,)
    api.finish_delivery_shutdown.side_effect = ((membership_error,), (membership_error,))
    manager = _manager(api, runtime)

    deadline = time.monotonic() + 1
    with pytest.raises(TelegramResourceShutdownError) as raised:
        manager.stop_channel_resources(deadline)

    assert raised.value.errors == (outbound_error, membership_error)
    api.begin_delivery_shutdown.assert_called_once_with(deadline)
    runtime.stop.assert_called_once_with(deadline)
    api.finish_delivery_shutdown.assert_called_once_with(deadline)


def test_manager_stops_runtime_after_scheduler_shutdown_error() -> None:
    api, runtime = Mock(), Mock()
    scheduler_error = MsgLogScanShutdownTimeout("blocked scan")
    runtime_error = RuntimeError("runtime shutdown failed")
    api.begin_delivery_shutdown.return_value = ()
    api.finish_delivery_shutdown.return_value = ()
    runtime.stop.side_effect = runtime_error
    manager = _manager(api, runtime)
    manager.msglog_scan = Mock(stop=Mock(return_value=(scheduler_error,)))

    deadline = time.monotonic() + 1
    with pytest.raises(TelegramResourceShutdownError) as raised:
        manager.stop_channel_resources(deadline)

    assert raised.value.errors == (scheduler_error, runtime_error)
    manager.msglog_scan.stop.assert_called_once_with(ANY)
    runtime.stop.assert_called_once_with(deadline)
    api.finish_delivery_shutdown.assert_called_once_with(deadline)
