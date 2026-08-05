from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master.etm_metrics import parse_metrics_config
from efb_telegram_master.telegram_runtime import TelegramPollingRuntime


@pytest.mark.asyncio
async def test_polling_lifecycle_keeps_ptb_startup_and_shutdown_order():
    order: list[str] = []
    runtime = object.__new__(TelegramPollingRuntime)
    runtime.logger = Mock()
    runtime._stop_event = None

    class Updater:
        running = True

        async def start_polling(self, **kwargs: object) -> None:
            order.append("updater.start")

        async def stop(self) -> None:
            order.append("updater.stop")

    async def initialize() -> None:
        order.append("initialize")

    async def post_init(_application: object) -> None:
        order.append("post_init")

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
        "initialize", "post_init", "updater.start", "application.start",
        "updater.stop", "application.stop", "post_stop", "shutdown", "post_shutdown",
    ]


def test_metrics_config_parser_returns_validated_endpoint() -> None:
    logger = Mock()

    assert parse_metrics_config({"top_n": "3", "host": "127.0.0.2", "port": "9200"}, logger) == (
        3,
        ("127.0.0.2", 9200),
    )
