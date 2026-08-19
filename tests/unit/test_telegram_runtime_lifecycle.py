import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest

from efb_telegram_master.telegram_runtime import TelegramPollingRuntime, TelegramRuntimeShutdownTimeout
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


@pytest.mark.asyncio
async def test_post_lifecycle_callbacks_bind_and_clear_the_runtime_loop() -> None:
    application = Mock()
    async_runtime = Mock()
    async_bot = Mock()
    async_bot.get_me = AsyncMock(return_value=SimpleNamespace(id=1))
    on_started = AsyncMock()
    on_stopped = AsyncMock()
    runtime = TelegramPollingRuntime(Mock(), application, async_bot, async_runtime, on_started, on_stopped)

    await runtime._application_lifecycle.post_init(application)
    await runtime._application_lifecycle.post_shutdown(application)

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

    await runtime._application_lifecycle.run(drop_pending_updates=False, timeout=1, stop_requested=lambda: False)

    assert observed == ["initialize", "post_init", "start_polling", "start", "updater_stop", "stop", "post_stop", "shutdown", "post_shutdown"]
    assert runtime._stop_event is None


def test_poll_forwards_custom_timeout_to_manual_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(application=Mock())
    observed: dict[str, object] = {}

    async def lifecycle(*, drop_pending_updates: bool, timeout: int, stop_requested: object) -> None:
        observed.update(drop_pending_updates=drop_pending_updates, timeout=timeout)

    monkeypatch.setattr(runtime._application_lifecycle, "run", lifecycle)

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
