"""Telegram channel lifecycle wiring."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from .bot_pool import build_bot_pool
from .metrics_runtime import configure_runtime_metrics
from .outbound import OutboundQueue
from .rate_limiter import SlidingWindowRateLimiter
from .telegram_api import TelegramAPI
from .telegram_runtime import build_telegram_polling_runtime

if TYPE_CHECKING:
    from . import TelegramChannel


class TelegramBotManager:
    """Construct and stop the Telegram runtime and its delivery collaborator."""

    logger = logging.getLogger(__name__)
    DEFAULT_SEND_WORKER_COUNT = 8
    BLOCKING_SEND_TIMEOUT = 300.0
    SHUTDOWN_DRAIN_TIMEOUT = 5.0
    SHUTDOWN_JOIN_GRACE = 1.0

    def __init__(self, channel: "TelegramChannel") -> None:
        self._stopping = threading.Event()
        config = channel.config
        self.telegram_runtime = build_telegram_polling_runtime(
            config,
            channel,
            self.logger,
            channel._telegram_runtime_started,
            channel._telegram_runtime_stopped,
        )
        bot_pool = build_bot_pool(config.get("auxiliary_bots", []), config, channel, self.telegram_runtime.async_runtime, self.logger)
        outbound_queue = OutboundQueue(
            self.telegram_runtime.bot,
            bot_pool,
            SlidingWindowRateLimiter(),
            worker_count=self.DEFAULT_SEND_WORKER_COUNT,
            blocking_timeout=self.BLOCKING_SEND_TIMEOUT,
            shutdown_drain_timeout=self.SHUTDOWN_DRAIN_TIMEOUT,
            shutdown_join_grace=self.SHUTDOWN_JOIN_GRACE,
            cancel_active_calls=self.telegram_runtime.async_runtime.begin_delivery_shutdown,
        )
        self.api = TelegramAPI(channel, self.telegram_runtime.bot, outbound_queue, bot_pool)
        _metrics, metrics_server = configure_runtime_metrics(config, channel.db, bot_pool, outbound_queue, self.logger)
        self.api.bind_metrics_server(metrics_server)
        outbound_queue.start()
        self.telegram_runtime.add_base_dispatchers(config["admins"], channel.update_locale)

    def stop_channel_resources(self) -> None:
        """Stop delivery resources before the polling runtime is stopped."""
        self._stopping.set()
        self.logger.info("Stopping Telegram delivery resources", extra={"event": "telegram_bot.stop_started"})
        self.api.stop_delivery_resources(self.SHUTDOWN_JOIN_GRACE)
        self.logger.info("Stopped Telegram delivery resources", extra={"event": "telegram_bot.stop_completed"})
