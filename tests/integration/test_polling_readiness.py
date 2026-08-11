import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def poll_bot():
    """Keep polling-readiness fixture tests independent of Telegram."""


class PollingStartupError(RuntimeError):
    pass


class ReadyThenFail(threading.Event):
    def __init__(self, release_failure: threading.Event):
        super().__init__()
        self.release_failure = release_failure

    def wait(self, timeout: float) -> bool:
        ready = super().wait(timeout)
        if ready:
            self.release_failure.set()
        return ready


class FakeTelegramRuntime:
    def __init__(self, ready, *, fail_startup: bool):
        self.async_runtime = SimpleNamespace(_ready=ready)
        self.application = SimpleNamespace(
            updater=SimpleNamespace(running=False),
            running=False,
        )
        self.fail_startup = fail_startup
        self.stop_requested = threading.Event()

    def poll(self, **kwargs) -> None:
        if self.fail_startup:
            self.async_runtime._ready.set()
            self.async_runtime._ready.release_failure.wait()
            raise PollingStartupError("polling startup failed")

        self.application.updater.running = True
        self.application.running = True
        self.async_runtime._ready.set()
        self.stop_requested.wait()

    def stop(self) -> None:
        self.stop_requested.set()


class NeverReadyTelegramRuntime(FakeTelegramRuntime):
    def __init__(self) -> None:
        super().__init__(threading.Event(), fail_startup=False)

    def poll(self, **kwargs) -> None:
        self.stop_requested.wait()


def test_poll_bot_factory_rejects_runtime_ready_when_polling_startup_fails(
    poll_bot_factory,
):
    release_failure = threading.Event()
    runtime = FakeTelegramRuntime(ReadyThenFail(release_failure), fail_startup=True)
    channel = SimpleNamespace(channel_id="startup-failure", telegram_runtime=runtime, stop_polling=runtime.stop)

    with pytest.raises(PollingStartupError, match="polling startup failed"):
        poll_bot_factory.start(channel)


def test_poll_bot_factory_accepts_fully_running_application(poll_bot_factory):
    runtime = FakeTelegramRuntime(threading.Event(), fail_startup=False)
    channel = SimpleNamespace(channel_id="ready", telegram_runtime=runtime, stop_polling=runtime.stop)
    thread_name = f"pytest-poll-bot-{channel.channel_id}"

    try:
        poll_bot_factory.start(channel)
        assert runtime.async_runtime._ready.is_set()
        assert runtime.application.updater.running
        assert runtime.application.running
    finally:
        poll_bot_factory.stop(channel)

    assert not any(thread.is_alive() and thread.name == thread_name for thread in threading.enumerate())
    assert not any(thread.is_alive() and not thread.daemon and thread.name == thread_name for thread in threading.enumerate())


def test_poll_bot_factory_cleans_up_when_readiness_times_out(poll_bot_factory, monkeypatch):
    monkeypatch.setattr("tests.integration.conftest.POLLING_START_TIMEOUT", 0.05)
    runtime = NeverReadyTelegramRuntime()
    stop_calls = 0

    def stop_polling() -> None:
        nonlocal stop_calls
        stop_calls += 1
        runtime.stop()

    channel = SimpleNamespace(channel_id="never-ready", telegram_runtime=runtime, stop_polling=stop_polling)
    thread_name = "pytest-poll-bot-never-ready"
    started_at = time.monotonic()

    with pytest.raises(RuntimeError, match="did not become ready"):
        poll_bot_factory.start(channel)

    assert time.monotonic() - started_at < 1
    assert stop_calls == 1
    assert not any(thread.is_alive() and thread.name == thread_name for thread in threading.enumerate())
    poll_bot_factory.stop(channel)
    assert stop_calls == 1
