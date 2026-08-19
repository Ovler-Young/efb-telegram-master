"""Telegram channel lifecycle wiring."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from ehforwarderbot.types import ModuleID
from telegram import Update
from telegram.ext import CallbackContext

from ..history.msglog_scan import MsgLogScanScheduler
from ..outbound.outbound import OutboundQueue
from ..persistence.chat_association_repository import ChatAssociationRepository
from ..persistence.msglog_ingestion_repository import MsgLogIngestionRepository
from ..transport.telegram_api import TelegramAPI
from ..transport.telegram_error_router import TelegramErrorRouter
from ..transport.telegram_runtime import TelegramPollingRuntime, build_telegram_polling_runtime
from .bot_pool import build_bot_pool
from .metrics_runtime import configure_runtime_metrics
from .mtproto import MTProtoClient, MTProtoRetryableError, MTProtoSessionOwnershipError
from .rate_limiter import SlidingWindowRateLimiter

if TYPE_CHECKING:
    from .. import TelegramChannel


class TelegramResourceShutdownError(RuntimeError):
    """One or more owned Telegram resources did not stop."""

    def __init__(self, errors: tuple[BaseException, ...]) -> None:
        self.errors = errors
        details = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
        super().__init__(f"Telegram resource shutdown failed: {details}")


class TelegramBotManagerInitializationCleanup:
    """Retain delivery shutdown ownership after manager construction fails."""

    def __init__(self, manager: "TelegramBotManager") -> None:
        self.manager = manager

    def retry(self) -> tuple[BaseException, ...]:
        return self.manager._stop_after_initialization_failure()


class TelegramBotManager:
    """Construct and stop the Telegram runtime and its delivery collaborator."""

    logger = logging.getLogger(__name__)
    DEFAULT_SEND_WORKER_COUNT = 8
    BLOCKING_SEND_TIMEOUT = 300.0
    SHUTDOWN_DRAIN_TIMEOUT = 5.0
    SHUTDOWN_JOIN_GRACE = 1.0

    def __init__(
        self,
        channel: "TelegramChannel",
        mtproto: MTProtoClient,
        msglog_ingestion: MsgLogIngestionRepository,
        chat_associations: ChatAssociationRepository,
        channel_id: ModuleID,
        network_error_prompt_interval: Callable[[], int],
        auto_locale: Callable[[], bool],
        translate: Callable[[str], str],
        ngettext: Callable[[str, str, int], str],
        locale_update: Callable[[Update, logging.Logger], None],
    ) -> None:
        self._stopping = threading.Event()
        self.mtproto = mtproto
        self.chat_associations, self.channel_id = chat_associations, channel_id
        self.network_error_prompt_interval = network_error_prompt_interval
        self.auto_locale = auto_locale
        self._translate, self._ngettext = translate, ngettext
        self._locale_update = locale_update
        config = channel.config
        self.admins: Sequence[int] = config.admins
        self.telegram_runtime = build_telegram_polling_runtime(
            config,
            channel,
            self.logger,
            self.runtime_started,
            self.runtime_stopped,
        )
        self.msglog_scan = MsgLogScanScheduler(self.telegram_runtime, mtproto, msglog_ingestion, chat_associations, self.logger)
        bot_pool = build_bot_pool(config.auxiliary_bots, config.request, channel, self.telegram_runtime.async_runtime, self.logger)
        outbound_queue = OutboundQueue(
            self.telegram_runtime.bot,
            bot_pool,
            SlidingWindowRateLimiter(),
            worker_count=self.DEFAULT_SEND_WORKER_COUNT,
            blocking_timeout=self.BLOCKING_SEND_TIMEOUT,
            shutdown_drain_timeout=self.SHUTDOWN_DRAIN_TIMEOUT,
            shutdown_join_grace=self.SHUTDOWN_JOIN_GRACE,
            cancel_active_calls=self.telegram_runtime.async_runtime.begin_delivery_shutdown,
            max_pending=config.outbound.max_pending,
        )
        self.api = TelegramAPI(channel, self.telegram_runtime.bot, outbound_queue, bot_pool)
        self.error_router = TelegramErrorRouter(
            self.api,
            self.admins,
            self.chat_associations,
            self.channel_id,
            self.network_error_prompt_interval,
            self._translate,
            self._ngettext,
            self._stopping,
            self.logger,
        )
        self.error = self.error_router.handle
        _metrics, metrics_server = configure_runtime_metrics(config, channel.db, bot_pool, outbound_queue, self.logger)
        self.api.bind_metrics_server(metrics_server)
        outbound_queue.start()
        try:
            self.telegram_runtime.add_base_dispatchers(config.admins, self.update_locale)
        except BaseException as error:
            cleanup = TelegramBotManagerInitializationCleanup(self)
            cleanup_errors = cleanup.retry()
            if cleanup_errors:
                setattr(error, "telegram_bot_manager_cleanup", cleanup)
            raise

    def _stop_after_initialization_failure(self) -> tuple[BaseException, ...]:
        deadline = time.monotonic() + self.SHUTDOWN_DRAIN_TIMEOUT + self.SHUTDOWN_JOIN_GRACE
        scheduler = getattr(self, "msglog_scan", None)
        cleanup_errors: list[BaseException] = []
        scan_errors: list[BaseException] = []
        if scheduler is not None:
            try:
                scan_errors.extend(scheduler.stop(max(0.0, deadline - time.monotonic())))
            except BaseException as error:
                scan_errors.append(error)
                self.logger.exception("Failed to stop MsgLog ingestion after initialization failed.")
        cleanup_errors.extend(scan_errors)
        try:
            cleanup_errors.extend(self.api.begin_delivery_shutdown(deadline))
        except BaseException as error:
            cleanup_errors.append(error)
            self.logger.exception("Failed to begin Telegram delivery shutdown after initialization failed.")
        if not scan_errors:
            try:
                self.telegram_runtime.stop(deadline)
            except BaseException as error:
                cleanup_errors.append(error)
                self.logger.exception("Failed to stop the Telegram runtime after initialization failed.")
        try:
            cleanup_errors.extend(self.api.finish_delivery_shutdown(deadline))
        except BaseException as error:
            cleanup_errors.append(error)
            self.logger.exception("Failed to finish Telegram delivery shutdown after initialization failed.")
        for cleanup_error in cleanup_errors:
            self.logger.error(
                "Telegram delivery resource did not stop after initialization failed: %s",
                cleanup_error,
                exc_info=(type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
            )
        return tuple(cleanup_errors)

    @property
    def _(self) -> Callable[[str], str]:
        return self._translate

    @property
    def ngettext(self) -> Callable[[str, str, int], str]:
        return self._ngettext

    def update_locale(self, update: Update, _context: CallbackContext) -> None:
        if self.auto_locale():
            self._locale_update(update, self.logger)

    async def runtime_started(self, runtime: TelegramPollingRuntime) -> None:
        for auxiliary in self.api.bot_pool.bots if self.api.bot_pool else []:
            auxiliary.bind_runtime(runtime.async_runtime)
        if not self.mtproto.enabled:
            return
        try:
            await self.mtproto.connect()
        except (ConnectionError, TimeoutError, OSError, MTProtoRetryableError, MTProtoSessionOwnershipError) as error:
            self.logger.warning(
                "MTProto startup is unavailable; MsgLog ingestion remains pending (%s).",
                type(error).__name__,
                extra={"event": "telegram_channel.mtproto_start_failed", "error_type": type(error).__name__},
            )
            return
        if not self.mtproto.connected:
            self.logger.warning("MTProto startup did not establish a connection; MsgLog ingestion remains pending.", extra={"event": "telegram_channel.mtproto_disconnected"})
            return
        self.logger.info("Resuming pending MsgLog ingestions", extra={"event": "telegram_channel.msglog_resume"})
        self.msglog_scan.resume()

    async def runtime_stopped(self, _runtime: TelegramPollingRuntime) -> None:
        await self.mtproto.disconnect()
        self.logger.info("MTProto disconnected", extra={"event": "telegram_channel.mtproto_stopped"})

    def stop_channel_resources(self, deadline: float | None = None) -> None:
        """Stop scans before runtime teardown, then delivery and membership resources."""
        if deadline is None:
            deadline = time.monotonic() + self.SHUTDOWN_DRAIN_TIMEOUT + self.SHUTDOWN_JOIN_GRACE
        self._stopping.set()
        self.logger.info("Stopping Telegram delivery resources", extra={"event": "telegram_bot.stop_started"})
        scheduler = getattr(self, "msglog_scan", None)
        scan_errors: list[BaseException] = []
        if scheduler is not None:
            try:
                scan_errors.extend(scheduler.stop(max(0.0, deadline - time.monotonic())))
            except BaseException as error:
                scan_errors.append(error)
        errors = list(self.api.begin_delivery_shutdown(deadline))
        errors.extend(scan_errors)
        try:
            self.telegram_runtime.stop(deadline)
        except BaseException as error:
            errors.append(error)
        final_membership_errors = self.api.finish_delivery_shutdown(deadline)
        if final_membership_errors:
            errors.extend(final_membership_errors)
        self.logger.info("Stopped Telegram delivery resources", extra={"event": "telegram_bot.stop_completed"})
        if errors:
            raise TelegramResourceShutdownError(tuple(errors))
