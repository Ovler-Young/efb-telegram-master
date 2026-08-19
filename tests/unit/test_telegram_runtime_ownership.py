import threading
from types import SimpleNamespace
from unittest.mock import ANY, Mock

import pytest

from efb_telegram_master import TelegramChannel
from efb_telegram_master.delivery.master_message import MasterMessageWorker, MasterMessageWorkerShutdownTimeout
from efb_telegram_master.outbound.outbound import OutboundQueue
from efb_telegram_master.outbound.outbound_types import QueueRequest
from efb_telegram_master.runtime.bot_manager import TelegramBotManager, TelegramResourceShutdownError
from efb_telegram_master.runtime.rate_limiter import SlidingWindowRateLimiter
from efb_telegram_master.transport.telegram_api import TelegramAPI


def test_channel_owner_stops_the_real_master_message_worker() -> None:
    runtime = SimpleNamespace(application=SimpleNamespace(add_handler=Mock()), as_async_callback=lambda callback: callback)
    worker = MasterMessageWorker(runtime, Mock(), Mock(), Mock(), lambda text: text, Mock())
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.logger = Mock()
    channel.rpc_utilities = Mock()
    channel.bot_manager = Mock()
    channel.master_message_worker = worker
    channel.db = Mock()

    try:
        channel.bot_manager.stop_channel_resources()
        assert worker.message_worker_thread.is_alive()

        channel.stop_polling()

        assert not worker.message_worker_thread.is_alive()
        channel.rpc_utilities.stop.assert_called_once_with(ANY)
        channel.db.stop_worker.assert_called_once_with()
    finally:
        worker.stop_worker()


def test_channel_withholds_downstream_resources_until_a_blocked_master_message_exits() -> None:
    runtime = SimpleNamespace(application=SimpleNamespace(add_handler=Mock()), as_async_callback=lambda callback: callback)
    started, release = threading.Event(), threading.Event()
    inbound = Mock()

    def block_message(*_args: object) -> None:
        started.set()
        assert release.wait(1)

    inbound.msg.side_effect = block_message
    worker = MasterMessageWorker(runtime, Mock(), inbound, Mock(), lambda text: text, Mock())
    worker.DEFAULT_STOP_TIMEOUT = 0.02
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.SHUTDOWN_TIMEOUT = 0.02
    channel._stop_polling_called = False
    channel.logger = Mock()
    channel.rpc_utilities = Mock()
    channel.bot_manager = Mock()
    channel.master_message_worker = worker
    channel.db = Mock()
    worker.message_queue.put((SimpleNamespace(effective_message=None), Mock()))
    assert started.wait(1)

    try:
        with pytest.raises(TelegramResourceShutdownError) as raised:
            channel.stop_polling()

        assert isinstance(raised.value.errors[0], MasterMessageWorkerShutdownTimeout)
        channel.bot_manager.stop_channel_resources.assert_not_called()
        channel.db.stop_worker.assert_not_called()

        release.set()
        worker.message_worker_thread.join(1)
        channel.stop_polling()
        channel.db.stop_worker.assert_called_once_with()
    finally:
        release.set()
        worker.stop_worker()


def test_channel_withholds_database_until_a_real_outbound_send_exits() -> None:
    started, release = threading.Event(), threading.Event()

    class Sender:
        def send_message(self, *, chat_id: int, text: str) -> str:
            started.set()
            assert release.wait(1)
            return text

    queue = OutboundQueue(Sender(), None, SlidingWindowRateLimiter(), worker_count=1, blocking_timeout=1, shutdown_drain_timeout=0.01, shutdown_join_grace=0.01)
    queue.start()
    queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "blocked"}, 1))
    assert started.wait(1)
    api = TelegramAPI(SimpleNamespace(), Mock(), queue, None)
    manager = _manager(api, Mock())
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.SHUTDOWN_TIMEOUT = 0.02
    channel._stop_polling_called = False
    channel.logger = Mock()
    channel.rpc_utilities = Mock()
    channel.history_replay = Mock(stop=Mock(return_value=()))
    channel.master_message_worker = Mock(stop_worker=Mock(return_value=()))
    channel.bot_manager = manager
    channel.db = Mock()

    try:
        with pytest.raises(TelegramResourceShutdownError, match="shutdown deadline"):
            channel.stop_polling()
        channel.db.stop_worker.assert_not_called()

        release.set()
        assert queue._finalized.wait(1)
        channel.stop_polling()
        channel.db.stop_worker.assert_called_once_with()
    finally:
        release.set()
        queue.stop()


def _manager(api: Mock, runtime: Mock) -> TelegramBotManager:
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager._stopping = Mock()
    manager.logger = Mock()
    manager.api = api
    manager.telegram_runtime = runtime
    return manager
