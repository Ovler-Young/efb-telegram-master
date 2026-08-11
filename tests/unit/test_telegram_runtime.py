import asyncio
import inspect
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from telegram import Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.auxiliary_bot import AuxiliaryBot, MembershipProbeShutdownTimeout
from efb_telegram_master.bot_manager import TelegramBotManager, TelegramResourceShutdownError
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.master_message import MasterMessageWorker
from efb_telegram_master.metrics_runtime import MetricsServer, parse_metrics_config
from efb_telegram_master.outbound_types import OutboundShutdownTimeout, SchedulerStoppedError
from efb_telegram_master.telegram_api import TelegramAPI
from efb_telegram_master.telegram_runtime import TelegramPollingRuntime, build_telegram_polling_runtime
from efb_telegram_master.telegram_sync_bridge import AsyncTelegramRuntime, SyncBotFacade


def _runtime(*, application: object | None = None, async_runtime: AsyncTelegramRuntime | None = None) -> TelegramPollingRuntime:
    return TelegramPollingRuntime(
        Mock(),
        application,
        Mock(),
        async_runtime or AsyncTelegramRuntime(Mock()),
        AsyncMock(),
        AsyncMock(),
    )


def test_sync_bot_facade_preserves_telegram_method_signature() -> None:
    async def send_message(chat_id: int, text: str) -> tuple[int, str]:
        return chat_id, text

    facade = SyncBotFacade(SimpleNamespace(send_message=send_message), Mock())

    assert inspect.signature(facade.send_message).bind(42, "message").arguments == {"chat_id": 42, "text": "message"}


def test_async_runtime_call_uses_bound_loop_without_starting_background_loop() -> None:
    runtime = AsyncTelegramRuntime(Mock())
    runtime._ready = Mock()
    runtime._ready.wait.return_value = True
    runtime._loop = object()
    runtime._loop_thread_id = -1
    runtime._ensure_background_loop = Mock()
    future = Mock()
    future.result.return_value = "ok"

    async def coroutine_function() -> None:
        return None

    coroutine = coroutine_function()

    try:
        with patch("efb_telegram_master.telegram_sync_bridge.asyncio.run_coroutine_threadsafe", return_value=future) as runner:
            assert runtime.call(coroutine, timeout=7) == "ok"
    finally:
        coroutine.close()

    runtime._ready.wait.assert_called_once_with(timeout=2.0)
    runtime._ensure_background_loop.assert_not_called()
    runner.assert_called_once_with(coroutine, runtime._loop)
    future.result.assert_called_once_with(7)


def test_async_runtime_call_starts_background_loop_when_no_loop_is_ready() -> None:
    runtime = AsyncTelegramRuntime(Mock())
    runtime._ready = Mock()
    runtime._ready.wait.return_value = False
    background_loop = object()
    runtime._loop = None
    runtime._loop_thread_id = None

    def ensure_background_loop() -> None:
        runtime._loop = background_loop
        runtime._loop_thread_id = -1

    runtime._ensure_background_loop = Mock(side_effect=ensure_background_loop)
    future = Mock()
    future.result.return_value = "ok"

    async def coroutine_function() -> None:
        return None

    coroutine = coroutine_function()

    try:
        with patch("efb_telegram_master.telegram_sync_bridge.asyncio.run_coroutine_threadsafe", return_value=future) as runner:
            assert runtime.call(coroutine) == "ok"
    finally:
        coroutine.close()

    runtime._ready.wait.assert_called_once_with(timeout=2.0)
    runtime._ensure_background_loop.assert_called_once_with()
    runner.assert_called_once_with(coroutine, background_loop)
    future.result.assert_called_once_with(None)


def test_async_runtime_delivery_shutdown_cancels_active_calls_and_rejects_new_calls() -> None:
    runtime = AsyncTelegramRuntime(Mock())
    future = Mock()
    runtime._active_calls.add(future)

    runtime.begin_delivery_shutdown()

    future.cancel.assert_called_once_with()

    async def coroutine_function() -> None:
        return None

    coroutine = coroutine_function()
    with pytest.raises(RuntimeError, match="runtime is stopping"):
        runtime.call(coroutine)


def test_async_runtime_stale_loop_clear_does_not_remove_rebound_loop() -> None:
    runtime = AsyncTelegramRuntime(Mock())
    current_loop = object()
    runtime._loop = current_loop
    runtime._loop_thread_id = -1
    runtime._loop_thread = Mock()
    runtime._owns_loop_thread = False
    runtime._ready.set()

    runtime.clear_loop(object())

    assert runtime._loop is current_loop
    assert runtime._loop_thread_id == -1
    assert runtime._ready.is_set()


@pytest.mark.parametrize(
    ("environment", "expected"),
    [(None, 16), ("3", 24), ("0.5", 4), ("invalid", 16), ("0", 16)],
)
def test_default_connection_pool_size_uses_worker_count_multiplier(monkeypatch: pytest.MonkeyPatch, environment: str | None, expected: int) -> None:
    if environment is None:
        monkeypatch.delenv("ETM_HTTPX_POOL_MULTIPLIER", raising=False)
    else:
        monkeypatch.setenv("ETM_HTTPX_POOL_MULTIPLIER", environment)

    assert TelegramPollingRuntime._default_connection_pool_size({}) == expected


def test_build_runtime_passes_local_mode_and_independent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = [Mock(name="request"), Mock(name="get_updates_request")]
    application = Mock()
    builder = Mock()
    builder.bot.return_value = builder
    builder.job_queue.return_value = builder
    builder.post_init.return_value = builder
    builder.post_shutdown.return_value = builder
    builder.build.return_value = application
    channel = Mock()
    channel.flag.side_effect = {"local_tdlib_api": True, "api_base_url": "http://localhost:8081/bot", "api_base_file_url": "file:///var/lib/telegram-bot-api"}.get

    with patch("efb_telegram_master.telegram_runtime.build_request", side_effect=requests) as build_request:
        with patch("efb_telegram_master.telegram_runtime.telegram.Bot") as bot_cls:
            with patch("efb_telegram_master.telegram_runtime.Application.builder", return_value=builder):
                runtime = build_telegram_polling_runtime({"token": "123:token"}, channel, Mock(), AsyncMock(), AsyncMock())

    bot_cls.assert_called_once_with(
        token="123:token",
        local_mode=True,
        base_url="http://localhost:8081/bot",
        base_file_url="file:///var/lib/telegram-bot-api",
        request=requests[0],
        get_updates_request=requests[1],
    )
    assert build_request.call_count == 2
    assert [call.args[0] for call in build_request.call_args_list] == [
        {"read_timeout": 15.0, "connection_pool_size": 16},
        {"read_timeout": 15.0, "connection_pool_size": 16},
    ]
    assert runtime.application is application


@pytest.mark.asyncio
async def test_post_lifecycle_callbacks_bind_and_clear_the_runtime_loop() -> None:
    application = Mock()
    async_runtime = Mock()
    async_bot = Mock()
    async_bot.get_me = AsyncMock(return_value=SimpleNamespace(id=1))
    on_started = AsyncMock()
    on_stopped = AsyncMock()
    runtime = TelegramPollingRuntime(Mock(), application, async_bot, async_runtime, on_started, on_stopped)

    await runtime._post_init(application)
    await runtime._post_shutdown(application)

    async_runtime.bind_loop.assert_called_once_with(asyncio.get_running_loop())
    on_started.assert_awaited_once_with(runtime)
    on_stopped.assert_awaited_once_with(runtime)
    async_runtime.clear_loop.assert_called_once_with()
    assert runtime._shutdown_complete.is_set()


@pytest.mark.asyncio
async def test_polling_lifecycle_starts_polling_then_stops_every_ptb_component() -> None:
    observed: list[str] = []
    runtime: TelegramPollingRuntime

    class Updater:
        running = False

        async def start_polling(self, **kwargs: object) -> None:
            observed.append("start_polling")
            assert kwargs["timeout"] == 1
            assert runtime._stop_event is not None
            self.running = True
            runtime._stop_event.set()

        async def stop(self) -> None:
            observed.append("updater_stop")
            self.running = False

    async def record(name: str) -> None:
        observed.append(name)

    updater = Updater()
    application = SimpleNamespace(
        initialize=lambda: record("initialize"),
        post_init=lambda _application: record("post_init"),
        updater=updater,
        start=lambda: record("start"),
        running=True,
        stop=lambda: record("stop"),
        post_stop=lambda _application: record("post_stop"),
        shutdown=lambda: record("shutdown"),
        post_shutdown=lambda _application: record("post_shutdown"),
        create_task=Mock(),
        process_error=Mock(),
    )
    runtime = _runtime(application=application)

    await runtime._run_application_lifecycle(drop_pending_updates=False, timeout=1)

    assert observed == ["initialize", "post_init", "start_polling", "start", "updater_stop", "stop", "post_stop", "shutdown", "post_shutdown"]
    assert runtime._stop_event is None


def test_poll_forwards_custom_timeout_to_manual_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(application=Mock())
    observed: dict[str, object] = {}

    async def lifecycle(*, drop_pending_updates: bool, timeout: int) -> None:
        observed.update(drop_pending_updates=drop_pending_updates, timeout=timeout)

    monkeypatch.setattr(runtime, "_run_application_lifecycle", lifecycle)

    runtime.poll(drop_pending_updates=True, timeout=1)

    assert observed == {"drop_pending_updates": True, "timeout": 1}
    assert runtime._shutdown_complete.is_set()


def test_stop_signals_active_polling_event_then_shuts_down_background_runtime() -> None:
    async_runtime = Mock()
    stop_event = Mock()
    runtime = _runtime(application=Mock(), async_runtime=async_runtime)
    runtime._stop_event = stop_event
    runtime._shutdown_complete.set()

    runtime.stop()

    async_runtime.call_soon.assert_called_once_with(stop_event.set)
    async_runtime.shutdown.assert_called_once_with()


def test_stop_falls_back_to_ptb_stop_running_and_is_idempotent() -> None:
    async_runtime = Mock()
    async_runtime.call_soon.return_value = False
    application = Mock()
    runtime = _runtime(application=application, async_runtime=async_runtime)

    runtime.stop()
    runtime.stop()

    async_runtime.call_soon.assert_called_once_with(application.stop_running)
    application.stop_running.assert_called_once_with()
    async_runtime.shutdown.assert_called_once_with()


def test_api_resource_shutdown_stops_metrics_server_under_its_current_owner() -> None:
    bot_pool = Mock()
    bot_pool.wait_for_shutdown.return_value = ()
    api = TelegramAPI(SimpleNamespace(), Mock(), Mock(), bot_pool)
    metrics_server = Mock()
    api.bind_metrics_server(metrics_server)

    assert api.stop_delivery_resources(2.5) == ()

    api._outbound_queue.stop.assert_called_once_with()
    metrics_server.stop.assert_called_once_with(2.5)
    bot_pool.begin_shutdown.assert_called_once_with()
    bot_pool.wait_for_shutdown.assert_called_once()
    assert api._metrics_server is None


def test_api_timeout_still_stops_other_delivery_resources() -> None:
    queue = Mock()
    queue.stop.side_effect = OutboundShutdownTimeout("blocked send")
    bot_pool = Mock()
    bot_pool.wait_for_shutdown.return_value = ()
    api = TelegramAPI(SimpleNamespace(), Mock(), queue, bot_pool)
    metrics_server = Mock()
    api.bind_metrics_server(metrics_server)

    errors = api.stop_delivery_resources(2.5)

    assert isinstance(errors[0], OutboundShutdownTimeout)
    metrics_server.stop.assert_called_once_with(2.5)
    bot_pool.begin_shutdown.assert_called_once_with()
    bot_pool.wait_for_shutdown.assert_called_once()


def test_channel_shutdown_error_still_stops_channel_owned_workers() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.logger = Mock()
    channel.rpc_utilities = Mock()
    channel.bot_manager = Mock()
    channel.bot_manager.stop_channel_resources.side_effect = TelegramResourceShutdownError((OutboundShutdownTimeout("blocked send"),))
    channel.telegram_runtime = Mock()
    channel.master_message_worker = Mock()
    channel.db = Mock()

    with pytest.raises(TelegramResourceShutdownError, match="blocked send"):
        channel.stop_polling()

    assert channel._stop_polling_called
    channel.master_message_worker.stop_worker.assert_called_once_with()
    channel.db.stop_worker.assert_called_once_with()


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
        channel.rpc_utilities.shutdown.assert_called_once_with()
        channel.db.stop_worker.assert_called_once_with()
    finally:
        worker.stop_worker()


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


def _manager(api: Mock, runtime: Mock) -> TelegramBotManager:
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager._stopping = Mock()
    manager.logger = Mock()
    manager.api = api
    manager.telegram_runtime = runtime
    return manager


def test_manager_does_not_notify_through_a_stopped_outbound_queue() -> None:
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager.logger = Mock()
    manager._stopping = threading.Event()
    manager._stopping.set()

    manager._handle_error(object(), SchedulerStoppedError("Outbound queue stopped."))

    manager.logger.info.assert_called_once_with(
        "Ignoring outbound delivery cancellation during Telegram shutdown.",
        extra={"event": "telegram_channel.outbound_cancelled_during_shutdown"},
    )


def test_manager_reports_scheduler_stopped_error_while_running() -> None:
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager.logger = Mock()
    manager._stopping = threading.Event()
    manager._notify_unhandled_error = Mock()
    update = object()
    error = SchedulerStoppedError("Outbound queue stopped.")

    manager._handle_error(update, error)

    manager._notify_unhandled_error.assert_called_once_with(update, error)
    manager.logger.info.assert_not_called()


def test_manager_retries_membership_join_after_stopping_runtime() -> None:
    api, runtime = Mock(), Mock()
    membership_error = MembershipProbeShutdownTimeout("bot 10")
    api.begin_delivery_shutdown.return_value = ()
    api.finish_delivery_shutdown.side_effect = ((membership_error,), ())
    manager = _manager(api, runtime)

    manager.stop_channel_resources()

    runtime.stop.assert_called_once_with()
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

        def stop(self) -> None:
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
        assert not any(thread.is_alive() for thread in auxiliary._membership_probe_executor._threads)
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

    with pytest.raises(TelegramResourceShutdownError) as raised:
        manager.stop_channel_resources()

    assert raised.value.errors == (outbound_error, membership_error)
    runtime.stop.assert_called_once_with()


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


def test_channel_dispatch_stops_when_the_runtime_stop_signal_is_set() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.bot_manager = SimpleNamespace(_stopping=Mock(is_set=Mock(return_value=True)))
    channel.message_service = Mock()
    message = Mock()

    assert channel.send_message(message) is message
    channel.message_service.send_message.assert_not_called()


def test_channel_dispatches_messages_while_the_runtime_is_running() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.bot_manager = SimpleNamespace(_stopping=Mock(is_set=Mock(return_value=False)))
    channel.message_service = Mock()
    message = Mock()
    channel.message_service.send_message.return_value = message

    assert channel.send_message(message) is message
    channel.message_service.send_message.assert_called_once_with(message)
