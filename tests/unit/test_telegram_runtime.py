from types import SimpleNamespace
from unittest.mock import ANY, Mock, call

import pytest

from efb_telegram_master import telegram_runtime
from efb_telegram_master.etm_metrics import parse_metrics_config
from efb_telegram_master.telegram_runtime import TelegramPollingRuntime, build_telegram_polling_runtime


def test_constructor_does_not_validate_bot_identity() -> None:
    bot = Mock()
    bot.get_me.side_effect = AssertionError("constructor must not call get_me")
    runtime = TelegramPollingRuntime(
        Mock(),
        SimpleNamespace(),
        bot,
        Mock(),
        Mock(),
        Mock(),
    )

    bot.get_me.assert_not_called()
    assert runtime.async_bot is bot


def test_builder_assembles_injected_ptb_dependencies_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    built: dict[str, object] = {}

    class Builder:
        def bot(self, bot: object) -> "Builder":
            built["bot"] = bot
            return self

        def job_queue(self, queue: object) -> "Builder":
            built["job_queue"] = queue
            return self

        def post_init(self, callback: object) -> "Builder":
            built["post_init"] = callback
            return self

        def post_shutdown(self, callback: object) -> "Builder":
            built["post_shutdown"] = callback
            return self

        def build(self) -> object:
            return SimpleNamespace()

    async_bot = Mock()
    async_runtime = Mock()
    monkeypatch.setattr(telegram_runtime.telegram, "Bot", Mock(return_value=async_bot))
    monkeypatch.setattr(telegram_runtime, "AsyncTelegramRuntime", Mock(return_value=async_runtime))
    monkeypatch.setattr(telegram_runtime.Application, "builder", staticmethod(Builder))
    channel = SimpleNamespace(flag=lambda name: {"local_tdlib_api": False, "api_base_url": None, "api_base_file_url": None}[name])

    runtime = build_telegram_polling_runtime({"token": "token"}, channel, Mock(), Mock(), Mock())

    assert runtime.async_bot is async_bot
    assert runtime.async_runtime is async_runtime
    assert runtime.application is not None
    telegram_runtime.telegram.Bot.assert_called_once_with(token="token", local_mode=False, request=ANY, get_updates_request=ANY)
    assert built["bot"] is async_bot
    assert built["job_queue"] is None
    assert callable(built["post_init"])
    assert callable(built["post_shutdown"])


@pytest.mark.asyncio
async def test_polling_lifecycle_keeps_ptb_startup_and_shutdown_order():
    order: list[str] = []
    runtime = object.__new__(TelegramPollingRuntime)
    runtime.logger = Mock()
    runtime._stop_event = None
    runtime._shutdown_complete = Mock()
    runtime.me = None

    class Updater:
        running = True

        async def start_polling(self, **kwargs: object) -> None:
            order.append("updater.start")

        async def stop(self) -> None:
            order.append("updater.stop")

    async def initialize() -> None:
        order.append("initialize")

    async def post_init(_application: object) -> None:
        await runtime._post_init(_application)

    async def get_me() -> SimpleNamespace:
        order.append("identity")
        return SimpleNamespace(id=123)

    async def on_started(_runtime: TelegramPollingRuntime) -> None:
        order.append("started")

    runtime.async_bot = SimpleNamespace(get_me=get_me)
    runtime.async_runtime = SimpleNamespace(bind_loop=lambda _loop: order.append("bind_loop"))
    runtime._on_started = on_started

    async def start() -> None:
        order.append("application.start")
        runtime._stop_event.set()

    async def stop() -> None:
        order.append("application.stop")

    async def post_stop(_application: object) -> None:
        order.append("post_stop")

    async def shutdown() -> None:
        order.append("shutdown")

    async def post_shutdown(_application: object) -> None:
        order.append("post_shutdown")

    runtime.application = SimpleNamespace(
        updater=Updater(),
        running=True,
        initialize=initialize,
        post_init=post_init,
        start=start,
        stop=stop,
        post_stop=post_stop,
        shutdown=shutdown,
        post_shutdown=post_shutdown,
    )

    await runtime._run_application_lifecycle(drop_pending_updates=False, timeout=1)

    assert order == [
        "initialize",
        "bind_loop",
        "identity",
        "started",
        "updater.start",
        "application.start",
        "updater.stop",
        "application.stop",
        "post_stop",
        "shutdown",
        "post_shutdown",
    ]

    async def on_stopped(_runtime: TelegramPollingRuntime) -> None:
        return None

    runtime.async_runtime.clear_loop = Mock()
    runtime._on_stopped = on_stopped
    await runtime._post_shutdown(runtime.application)
    assert runtime.logger.info.call_args_list == [
        call(
            "Telegram polling runtime started",
            extra={"event": "telegram_runtime.start"},
        ),
        call("Telegram polling runtime stopped", extra={"event": "telegram_runtime.stop"}),
    ]


@pytest.mark.asyncio
async def test_runtime_logs_lifecycle_callback_failure_with_error_type() -> None:
    runtime = object.__new__(TelegramPollingRuntime)
    runtime.logger = Mock()
    runtime.async_runtime = SimpleNamespace(bind_loop=Mock(), clear_loop=Mock())
    runtime._shutdown_complete = Mock()

    async def get_me() -> SimpleNamespace:
        return SimpleNamespace(id=123)

    runtime.async_bot = SimpleNamespace(get_me=get_me)

    async def started(_runtime: TelegramPollingRuntime) -> None:
        raise RuntimeError("unavailable")

    runtime._on_started = started
    with pytest.raises(RuntimeError, match="unavailable"):
        await runtime._post_init(Mock())

    runtime.logger.exception.assert_called_once_with(
        "Telegram runtime start callback failed",
        extra={"event": "telegram_runtime.start_callback_failed", "error_type": "RuntimeError"},
    )

    async def stopped(_runtime: TelegramPollingRuntime) -> None:
        raise ValueError("stopped")

    runtime._on_stopped = stopped
    with pytest.raises(ValueError, match="stopped"):
        await runtime._post_shutdown(Mock())

    assert runtime.logger.exception.call_args_list[-1] == call(
        "Telegram runtime stop callback failed",
        extra={"event": "telegram_runtime.stop_callback_failed", "error_type": "ValueError"},
    )


@pytest.mark.asyncio
async def test_shutdown_failures_log_events_and_error_types() -> None:
    runtime = object.__new__(TelegramPollingRuntime)
    runtime.logger = Mock()

    async def fail(*_args: object) -> None:
        raise RuntimeError("failed")

    runtime.application = SimpleNamespace(
        updater=SimpleNamespace(running=True, stop=fail),
        running=True,
        stop=fail,
        post_stop=fail,
        shutdown=fail,
        post_shutdown=fail,
    )

    await runtime._shutdown_application()

    assert [entry.kwargs["extra"] for entry in runtime.logger.exception.call_args_list] == [
        {"event": event, "error_type": "RuntimeError"}
        for event in (
            "telegram_runtime.updater_stop_failed",
            "telegram_runtime.application_stop_failed",
            "telegram_runtime.post_stop_failed",
            "telegram_runtime.shutdown_failed",
            "telegram_runtime.post_shutdown_failed",
        )
    ]


def test_webhook_poll_logs_start_and_stop_events() -> None:
    runtime = object.__new__(TelegramPollingRuntime)
    runtime.logger = Mock()
    runtime._webhook = {"start_webhook": {"listen": "127.0.0.1", "port": 8080}}
    runtime._shutdown_complete = Mock()
    runtime.application = SimpleNamespace(run_webhook=Mock())

    runtime.poll()

    assert runtime.logger.info.call_args_list == [
        call("Telegram webhook runtime starting", extra={"event": "telegram_runtime.webhook_start"}),
        call("Telegram webhook runtime stopped", extra={"event": "telegram_runtime.webhook_stop"}),
    ]


def test_polling_failure_logs_error_type(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = object.__new__(TelegramPollingRuntime)
    runtime.logger = Mock()
    runtime._webhook = None
    runtime._shutdown_complete = Mock()

    def fail_run(coroutine: object) -> None:
        coroutine.close()
        raise RuntimeError("failed")

    monkeypatch.setattr(telegram_runtime.asyncio, "run", fail_run)

    with pytest.raises(RuntimeError, match="failed"):
        runtime.poll()

    runtime.logger.exception.assert_called_once_with(
        "Telegram polling lifecycle failed",
        extra={"event": "telegram_runtime.polling_failed", "error_type": "RuntimeError"},
    )


def test_metrics_config_parser_returns_validated_endpoint() -> None:
    logger = Mock()

    assert parse_metrics_config({"top_n": "3", "host": "127.0.0.2", "port": "9200"}, logger) == (
        3,
        ("127.0.0.2", 9200),
    )
