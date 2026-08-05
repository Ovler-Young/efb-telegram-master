from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from efb_telegram_master.etm_metrics import parse_metrics_config
from efb_telegram_master.telegram_runtime import TelegramPollingRuntime


def test_constructor_does_not_validate_bot_identity() -> None:
    bot = Mock()
    bot.get_me.side_effect = AssertionError("constructor must not call get_me")
    builder = Mock()
    builder.bot.return_value = builder
    builder.job_queue.return_value = builder
    builder.post_init.return_value = builder
    builder.post_shutdown.return_value = builder
    builder.build.return_value = SimpleNamespace()
    channel = SimpleNamespace(flag=lambda name: False if name == "local_tdlib_api" else None)

    with patch.object(TelegramPollingRuntime, "_build_bot", return_value=bot), \
         patch("efb_telegram_master.telegram_runtime.Application.builder", return_value=builder):
        TelegramPollingRuntime({"token": "token"}, channel, Mock(), Mock(), Mock())

    bot.get_me.assert_not_called()


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
        "initialize", "bind_loop", "identity", "started", "updater.start", "application.start",
        "updater.stop", "application.stop", "post_stop", "shutdown", "post_shutdown",
    ]

    async def on_stopped(_runtime: TelegramPollingRuntime) -> None:
        return None

    runtime.async_runtime.clear_loop = Mock()
    runtime._on_stopped = on_stopped
    await runtime._post_shutdown(runtime.application)
    assert runtime.logger.info.call_args_list == [
        call("Telegram polling runtime started", extra={"event": "telegram_runtime.start"}),
        call("Telegram polling runtime stopped", extra={"event": "telegram_runtime.stop"}),
    ]


def test_metrics_config_parser_returns_validated_endpoint() -> None:
    logger = Mock()

    assert parse_metrics_config({"top_n": "3", "host": "127.0.0.2", "port": "9200"}, logger) == (
        3,
        ("127.0.0.2", 9200),
    )
