import asyncio
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest
from ehforwarderbot.channel import MasterChannel
from telegram import Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.auxiliary_bot import AuxiliaryBot, MembershipProbeShutdownTimeout
from efb_telegram_master.bot_manager import TelegramBotManager, TelegramResourceShutdownError
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.history_replay import HistoryReplayShutdownTimeout
from efb_telegram_master.master_message import MasterMessageWorker, MasterMessageWorkerShutdownTimeout
from efb_telegram_master.metrics_runtime import MetricsServer, parse_metrics_config
from efb_telegram_master.msglog_scan import MsgLogScanShutdownTimeout
from efb_telegram_master.outbound import OutboundQueue
from efb_telegram_master.outbound_types import OutboundShutdownTimeout, QueueRequest, SchedulerStoppedError
from efb_telegram_master.rate_limiter import SlidingWindowRateLimiter
from efb_telegram_master.telegram_api import TelegramAPI
from efb_telegram_master.telegram_runtime import TelegramPollingRuntime, TelegramRuntimeShutdownTimeout, build_telegram_polling_runtime
from efb_telegram_master.telegram_sync_bridge import AsyncTelegramRuntime


def _runtime(
    *,
    application: object | None = None,
    async_runtime: AsyncTelegramRuntime | None = None,
    webhook: dict[str, object] | None = None,
) -> TelegramPollingRuntime:
    return TelegramPollingRuntime(
        Mock(),
        application,
        Mock(),
        async_runtime or AsyncTelegramRuntime(Mock()),
        AsyncMock(),
        AsyncMock(),
        webhook,
    )


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


@pytest.mark.parametrize("webhook", [False, [], "invalid"])
def test_build_runtime_rejects_non_mapping_webhook_config(webhook: object) -> None:
    with pytest.raises(ValueError, match="webhook must be a mapping"):
        build_telegram_polling_runtime({"token": "123:token", "webhook": webhook}, Mock(), Mock(), AsyncMock(), AsyncMock())


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


def test_stop_during_poll_startup_prevents_application_initialization() -> None:
    async_runtime = Mock()
    async_runtime.call_soon.return_value = False
    stop_requested = threading.Event()
    application = SimpleNamespace(
        initialize=AsyncMock(),
        post_init=None,
        updater=None,
        running=False,
        stop=AsyncMock(),
        post_stop=None,
        shutdown=AsyncMock(),
        post_shutdown=None,
        create_task=Mock(),
        process_error=Mock(),
        stop_running=Mock(side_effect=stop_requested.set),
    )
    runtime = _runtime(application=application, async_runtime=async_runtime)
    poll_entered, release_poll = threading.Event(), threading.Event()
    original_run = asyncio.run

    def delayed_run(coroutine: object) -> object:
        poll_entered.set()
        assert release_poll.wait(1)
        return original_run(coroutine)

    errors: list[BaseException] = []

    def poll() -> None:
        try:
            runtime.poll()
        except BaseException as error:
            errors.append(error)

    with patch("efb_telegram_master.telegram_runtime.asyncio.run", side_effect=delayed_run):
        poll_thread = threading.Thread(target=poll)
        poll_thread.start()
        assert poll_entered.wait(1)

        stop_thread = threading.Thread(target=lambda: runtime.stop(time.monotonic() + 1))
        stop_thread.start()
        assert stop_requested.wait(1)
        release_poll.set()
        poll_thread.join(timeout=1)
        stop_thread.join(timeout=1)

    assert not poll_thread.is_alive()
    assert not stop_thread.is_alive()
    assert not errors
    application.initialize.assert_not_awaited()
    application.shutdown.assert_awaited_once_with()
    async_runtime.shutdown.assert_called_once_with(ANY)


def test_concurrent_poll_calls_admit_one_application_lifecycle() -> None:
    poll_started = threading.Event()
    initialized_loops: list[asyncio.AbstractEventLoop] = []
    async_runtime = Mock()

    class Updater:
        running = False

        async def start_polling(self, **_kwargs: object) -> None:
            self.running = True
            poll_started.set()

        async def stop(self) -> None:
            self.running = False

    updater = Updater()

    async def initialize() -> None:
        initialized_loops.append(asyncio.get_running_loop())

    async def no_op(*_args: object) -> None:
        return None

    application = SimpleNamespace(
        initialize=initialize,
        post_init=None,
        updater=updater,
        start=no_op,
        running=False,
        stop=no_op,
        post_stop=None,
        shutdown=no_op,
        post_shutdown=None,
        create_task=Mock(),
        process_error=Mock(),
        stop_running=Mock(),
    )
    runtime = _runtime(application=application, async_runtime=async_runtime)

    def signal_on_poll_loop(callback: object) -> bool:
        initialized_loops[0].call_soon_threadsafe(callback)
        return True

    async_runtime.call_soon.side_effect = signal_on_poll_loop
    errors: list[BaseException] = []

    def poll() -> None:
        try:
            runtime.poll()
        except BaseException as error:
            errors.append(error)

    first_poll = threading.Thread(target=poll)
    first_poll.start()
    assert poll_started.wait(1)

    second_poll = threading.Thread(target=poll)
    second_poll.start()
    second_poll.join(timeout=1)
    assert not second_poll.is_alive()

    runtime.stop(time.monotonic() + 1)
    first_poll.join(timeout=1)

    assert not first_poll.is_alive()
    assert len(initialized_loops) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "already active" in str(errors[0])


def test_poll_is_rejected_after_terminal_stop() -> None:
    async_runtime = Mock()
    runtime = _runtime(application=Mock(), async_runtime=async_runtime)

    runtime.stop()

    with pytest.raises(RuntimeError, match="has been stopped"):
        runtime.poll()

    async_runtime.shutdown.assert_called_once_with(None)


def test_stop_signals_active_polling_event_then_shuts_down_background_runtime() -> None:
    async_runtime = Mock()
    stop_event = Mock()
    runtime = _runtime(application=Mock(), async_runtime=async_runtime)
    runtime._stop_event = stop_event
    runtime._shutdown_complete.set()

    runtime.stop()

    async_runtime.call_soon.assert_called_once_with(stop_event.set)
    async_runtime.shutdown.assert_called_once_with(None)


def test_stop_falls_back_to_ptb_stop_running_and_is_idempotent() -> None:
    async_runtime = Mock()
    async_runtime.call_soon.return_value = False
    application = Mock()
    runtime = _runtime(application=application, async_runtime=async_runtime)

    runtime.stop()
    runtime.stop()

    async_runtime.call_soon.assert_called_once_with(application.stop_running)
    application.stop_running.assert_called_once_with()
    async_runtime.shutdown.assert_called_once_with(None)


def test_runtime_stop_timeout_can_be_retried() -> None:
    async_runtime = Mock()
    runtime = _runtime(application=Mock(), async_runtime=async_runtime)
    stop_event = Mock()
    runtime._stop_event = stop_event
    async_runtime.call_soon.side_effect = lambda callback: callback() or True

    with pytest.raises(TelegramRuntimeShutdownTimeout):
        runtime.stop(time.monotonic() + 0.01)

    stop_event.set.assert_called_once_with()
    async_runtime.shutdown.assert_not_called()
    runtime._shutdown_complete.set()
    runtime.stop(time.monotonic() + 0.1)
    async_runtime.shutdown.assert_called_once_with(ANY)


def test_webhook_stop_waits_for_blocked_ptb_teardown_before_shutting_down_runtime() -> None:
    async_runtime = Mock()
    teardown_started = threading.Event()
    release_teardown = threading.Event()
    application = Mock()

    def run_webhook(**_kwargs: object) -> None:
        teardown_started.set()
        release_teardown.wait()

    application.run_webhook.side_effect = run_webhook
    runtime = _runtime(application=application, async_runtime=async_runtime, webhook={"start_webhook": {}})
    poll_thread = threading.Thread(target=runtime.poll)
    poll_thread.start()
    try:
        assert teardown_started.wait(timeout=1)

        with pytest.raises(TelegramRuntimeShutdownTimeout):
            runtime.stop(time.monotonic() + 0.01)

        async_runtime.shutdown.assert_not_called()
    finally:
        release_teardown.set()
        poll_thread.join(timeout=1)
    assert not poll_thread.is_alive()

    runtime.stop(time.monotonic() + 0.1)

    async_runtime.shutdown.assert_called_once_with(ANY)


def test_webhook_runtime_forwards_typed_start_arguments() -> None:
    application = Mock()
    runtime = _runtime(
        application=application,
        webhook={"start_webhook": {"listen": "0.0.0.0", "port": 8443, "allowed_updates": ["message"], "secret_token": "secret"}},
    )

    runtime.poll(drop_pending_updates=True)

    application.run_webhook.assert_called_once_with(
        listen="0.0.0.0",
        port=8443,
        allowed_updates=["message"],
        secret_token="secret",
        drop_pending_updates=True,
        close_loop=True,
        stop_signals=None,
    )


@pytest.mark.parametrize("argument", ["port", "bootstrap_retries", "max_connections"])
def test_webhook_runtime_rejects_boolean_numeric_start_arguments(argument: str) -> None:
    application = Mock()
    runtime = _runtime(application=application, webhook={"start_webhook": {argument: True}})

    with pytest.raises(ValueError, match="must be an integer"):
        runtime.poll()

    application.run_webhook.assert_not_called()


def test_invalid_webhook_configuration_does_not_leave_lifecycle_active() -> None:
    async_runtime = Mock()
    runtime = _runtime(application=Mock(), async_runtime=async_runtime, webhook={"start_webhook": None})

    with pytest.raises(ValueError, match="webhook.start_webhook must be a mapping"):
        runtime.poll()

    runtime.stop(time.monotonic() + 0.01)

    async_runtime.shutdown.assert_called_once_with(ANY)


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


def test_bot_manager_constructor_failure_preserves_dispatcher_error_and_reports_cleanup_errors(caplog) -> None:
    channel = SimpleNamespace(config={"admins": [1], "outbound": {"max_pending": 17}}, db=Mock())
    async_runtime = Mock()

    def consume_runtime_call(coroutine, **_kwargs):
        coroutine.close()

    async_runtime.call.side_effect = consume_runtime_call
    runtime = Mock(async_runtime=async_runtime, bot=Mock())
    dispatcher_error = RuntimeError("dispatcher registration failed")
    delivery_error = RuntimeError("delivery shutdown failed")
    runtime_error = RuntimeError("runtime shutdown failed")
    runtime.add_base_dispatchers.side_effect = dispatcher_error
    runtime.stop.side_effect = runtime_error

    with (
        patch("efb_telegram_master.bot_manager.build_telegram_polling_runtime", return_value=runtime),
        patch("efb_telegram_master.bot_manager.build_bot_pool", return_value=None),
        patch("efb_telegram_master.bot_manager.OutboundQueue") as queue_type,
        patch("efb_telegram_master.bot_manager.configure_runtime_metrics", return_value=(None, None)),
        patch.object(TelegramAPI, "begin_delivery_shutdown", return_value=(delivery_error,)) as begin_delivery,
        patch.object(TelegramAPI, "finish_delivery_shutdown", return_value=()) as finish_delivery,
    ):
        with pytest.raises(RuntimeError, match="dispatcher registration failed"):
            TelegramBotManager(
                channel,
                Mock(),
                Mock(),
                Mock(),
                TelegramChannel.channel_id,
                lambda: 0,
                lambda: False,
                lambda text: text,
                lambda singular, _plural, _count: singular,
                Mock(),
            )

    queue_type.return_value.start.assert_called_once_with()
    assert queue_type.call_args.kwargs["max_pending"] == 17
    begin_delivery.assert_called_once_with(ANY)
    finish_delivery.assert_called_once_with(ANY)
    runtime.stop.assert_called_once_with(ANY)
    assert "Telegram delivery resource did not stop after initialization failed: delivery shutdown failed" in caplog.text
    assert "Failed to stop the Telegram runtime after initialization failed." in caplog.text


def test_channel_constructor_stops_database_when_bot_manager_creation_fails() -> None:
    database_manager = SimpleNamespace(
        chat_associations=Mock(),
        slave_chat_info=Mock(),
        slave_message_deliveries=Mock(),
        msglogs=Mock(),
        history_migrations=Mock(),
        msglog_ingestion=Mock(),
        _base_path="/tmp",
        stop_worker=Mock(),
    )

    with (
        patch.object(MasterChannel, "__init__", return_value=None),
        patch("efb_telegram_master.load_channel_config", return_value=({"token": "token", "admins": [1]}, Mock())),
        patch("efb_telegram_master.ExperimentalFlagsManager", return_value=Mock()),
        patch("efb_telegram_master.DatabaseManager", return_value=database_manager),
        patch("efb_telegram_master.channel_composition.TelegramBotManager", side_effect=RuntimeError("bot setup failed")),
        patch("efb_telegram_master.channel_composition.MTProtoClient", return_value=Mock()),
        patch("efb_telegram_master.channel_composition.ChatObjectCacheManager", return_value=Mock()),
        patch("efb_telegram_master.channel_composition.ChatDestinationCache", return_value=Mock()),
        patch("efb_telegram_master.channel_composition.TelegramChatID", return_value=Mock()),
    ):
        with pytest.raises(RuntimeError, match="bot setup failed"):
            TelegramChannel()

    database_manager.stop_worker.assert_called_once_with()


def test_channel_constructor_stops_started_history_replay_when_handler_registration_fails() -> None:
    application = Mock()
    runtime = SimpleNamespace(application=application, as_async_callback=lambda callback: callback)
    api = SimpleNamespace(session_expired=lambda *_args, **_kwargs: None, send_message=Mock())
    bot_manager = SimpleNamespace(api=api, telegram_runtime=runtime, msglog_scan=Mock(), error=Mock(), stop_channel_resources=Mock())
    history_migrations = SimpleNamespace(has_pending_entries=lambda: True, get_next_target=lambda: None)
    database_manager = SimpleNamespace(
        chat_associations=Mock(),
        slave_chat_info=Mock(),
        slave_message_deliveries=Mock(),
        msglogs=Mock(),
        history_migrations=history_migrations,
        msglog_ingestion=Mock(),
        _base_path="/tmp",
        stop_worker=Mock(),
    )
    flag = Mock(side_effect=lambda name: {"chats_per_page": 10, "multiple_slave_chats": False, "topic_group": 0}.get(name, False))
    dependencies = (
        "MTProtoClient",
        "ChatObjectCacheManager",
        "ChatDestinationCache",
        "TopicGroupService",
        "CommandsManager",
        "MasterMessageDelivery",
        "CallbackSessionStore",
        "RecipientSuggestionService",
        "LinkService",
        "LinkCompletionService",
        "ChatHeadService",
    )
    history_replay = Mock(stop=Mock(return_value=()))
    application.add_handler.side_effect = [None] * 6 + [RuntimeError("handler registration failed")]
    with ExitStack() as stack:
        stack.enter_context(patch.object(MasterChannel, "__init__", return_value=None))
        stack.enter_context(patch("efb_telegram_master.load_channel_config", return_value=({"token": "token", "admins": [1]}, Mock())))
        stack.enter_context(patch("efb_telegram_master.ExperimentalFlagsManager", return_value=flag))
        stack.enter_context(patch("efb_telegram_master.DatabaseManager", return_value=database_manager))
        stack.enter_context(patch("efb_telegram_master.channel_composition.TelegramBotManager", return_value=bot_manager))
        stack.enter_context(patch("efb_telegram_master.channel_composition.HistoryReplayWorker", return_value=history_replay))
        for dependency in dependencies:
            stack.enter_context(patch(f"efb_telegram_master.channel_composition.{dependency}"))

        with pytest.raises(RuntimeError, match="handler registration failed"):
            TelegramChannel()

    history_replay.resume.assert_called_once_with()
    history_replay.stop.assert_called_once_with(ANY)
    bot_manager.stop_channel_resources.assert_called_once_with(ANY)
    database_manager.stop_worker.assert_called_once_with()


def test_channel_rpc_bind_failure_stops_before_component_startup() -> None:
    database_manager = SimpleNamespace(
        chat_associations=Mock(),
        slave_chat_info=Mock(),
        slave_message_deliveries=Mock(),
        msglogs=Mock(),
        history_migrations=Mock(),
        msglog_ingestion=Mock(),
        _base_path="/tmp",
        stop_worker=Mock(),
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(MasterChannel, "__init__", return_value=None))
        stack.enter_context(patch("efb_telegram_master.load_channel_config", return_value=({"token": "token", "admins": [1], "rpc": {"server": "127.0.0.1", "port": 0}}, Mock())))
        stack.enter_context(patch("efb_telegram_master.rpc_utils._ThreadedXMLRPCServer", side_effect=OSError("bind failed")))
        stack.enter_context(patch("efb_telegram_master.DatabaseManager", return_value=database_manager))
        bot_manager = stack.enter_context(patch("efb_telegram_master.channel_composition.TelegramBotManager"))

        with pytest.raises(OSError, match="bind failed"):
            TelegramChannel()

    bot_manager.assert_not_called()
    database_manager.stop_worker.assert_called_once_with()


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
