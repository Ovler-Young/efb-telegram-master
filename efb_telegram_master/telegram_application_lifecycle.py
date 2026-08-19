"""PTB application hooks, polling execution, and ordered teardown."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import telegram
import telegram.error
from telegram.ext import Application

from .telegram_sync_bridge import AsyncTelegramRuntime

if TYPE_CHECKING:
    from .telegram_runtime import TelegramPollingRuntime

LifecycleCallback = Callable[["TelegramPollingRuntime"], Awaitable[None]]


class TelegramApplicationLifecycle:
    """Own PTB application hooks and the ordered polling teardown sequence."""

    def __init__(
        self,
        owner: TelegramPollingRuntime,
        logger: logging.Logger,
        async_runtime: AsyncTelegramRuntime,
        on_started: LifecycleCallback,
        on_stopped: LifecycleCallback,
    ) -> None:
        self._owner = owner
        self._logger = logger
        self._async_runtime = async_runtime
        self._on_started = on_started
        self._on_stopped = on_stopped
        self.stop_event: asyncio.Event | None = None

    async def post_init(self, _application: Application) -> None:
        self._async_runtime.bind_loop(asyncio.get_running_loop())
        self._owner._shutdown_complete.clear()
        self._owner.me = await self._owner.async_bot.get_me()
        assert self._owner.me, "Invalid bot credential provided."
        try:
            await self._on_started(self._owner)
        except Exception as error:
            self._logger.exception(
                "Telegram runtime start callback failed",
                extra={"event": "telegram_runtime.start_callback_failed", "error_type": type(error).__name__},
            )
            raise
        self._logger.info("Telegram polling runtime started", extra={"event": "telegram_runtime.start"})

    async def post_shutdown(self, _application: Application) -> None:
        try:
            await self._on_stopped(self._owner)
        except Exception as error:
            self._logger.exception(
                "Telegram runtime stop callback failed",
                extra={"event": "telegram_runtime.stop_callback_failed", "error_type": type(error).__name__},
            )
            raise
        finally:
            self._async_runtime.clear_loop()
            self._owner._shutdown_complete.set()
            self._logger.info("Telegram polling runtime stopped", extra={"event": "telegram_runtime.stop"})

    async def run(self, *, drop_pending_updates: bool, timeout: int, stop_requested: Callable[[], bool]) -> None:
        stop_event = asyncio.Event()
        try:
            self.stop_event = stop_event
            if stop_requested():
                return
            application = self._owner.application
            await application.initialize()
            if application.post_init:
                await application.post_init(application)
            if stop_requested():
                return
            updater = application.updater
            if updater is None:
                raise RuntimeError("Application.run_polling requires an Updater.")
            await updater.start_polling(
                poll_interval=0.0,
                timeout=timeout,
                bootstrap_retries=0,
                allowed_updates=None,
                drop_pending_updates=drop_pending_updates,
                error_callback=self.handle_polling_error,
            )
            await application.start()
            await stop_event.wait()
        finally:
            self.stop_event = None
            await self.shutdown()

    def handle_polling_error(self, error: telegram.error.TelegramError) -> None:
        application = self._owner.application
        application.create_task(application.process_error(error=error, update=None))

    async def shutdown(self) -> None:
        application = self._owner.application
        try:
            updater = application.updater
            if updater is not None and updater.running:
                await updater.stop()
        except Exception as error:
            self._logger.exception("Telegram updater stop failed", extra={"event": "telegram_runtime.updater_stop_failed", "error_type": type(error).__name__})
        try:
            if application.running:
                await application.stop()
        except Exception as error:
            self._logger.exception("Telegram application stop failed", extra={"event": "telegram_runtime.application_stop_failed", "error_type": type(error).__name__})
        try:
            if application.post_stop:
                await application.post_stop(application)
        except Exception as error:
            self._logger.exception("Telegram post-stop hook failed", extra={"event": "telegram_runtime.post_stop_failed", "error_type": type(error).__name__})
        try:
            await application.shutdown()
        except Exception as error:
            self._logger.exception("Telegram application shutdown failed", extra={"event": "telegram_runtime.shutdown_failed", "error_type": type(error).__name__})
        try:
            if application.post_shutdown:
                await application.post_shutdown(application)
        except Exception as error:
            self._logger.exception("Telegram post-shutdown hook failed", extra={"event": "telegram_runtime.post_shutdown_failed", "error_type": type(error).__name__})
