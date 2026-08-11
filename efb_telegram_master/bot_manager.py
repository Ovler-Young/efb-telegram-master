"""Telegram channel lifecycle wiring."""

from __future__ import annotations

import html
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import telegram
from ehforwarderbot.types import ChatID, ModuleID
from telegram import Message, Update
from telegram.ext import CallbackContext

from . import utils as etm_utils
from .auxiliary_bot import MembershipProbeShutdownTimeout
from .bot_pool import build_bot_pool
from .chat_association_repository import ChatAssociationRepository
from .metrics_runtime import configure_runtime_metrics
from .msglog_ingestion_repository import MsgLogIngestionRepository
from .msglog_scan import MsgLogScanScheduler
from .mtproto import MTProtoClient, MTProtoRetryableError, MTProtoSessionOwnershipError
from .outbound import OutboundQueue
from .outbound_types import SchedulerStoppedError
from .ptb_compat import sync_reply_html
from .rate_limiter import SlidingWindowRateLimiter
from .telegram_api import TelegramAPI
from .telegram_runtime import TelegramPollingRuntime, build_telegram_polling_runtime

if TYPE_CHECKING:
    from . import TelegramChannel


class TelegramResourceShutdownError(RuntimeError):
    """One or more owned Telegram resources did not stop."""

    def __init__(self, errors: tuple[BaseException, ...]) -> None:
        self.errors = errors
        details = "; ".join(f"{type(error).__name__}: {error}" for error in errors)
        super().__init__(f"Telegram resource shutdown failed: {details}")


class TelegramBotManager:
    """Construct and stop the Telegram runtime and its delivery collaborator."""

    logger = logging.getLogger(__name__)
    DEFAULT_SEND_WORKER_COUNT = 8
    BLOCKING_SEND_TIMEOUT = 300.0
    SHUTDOWN_DRAIN_TIMEOUT = 5.0
    SHUTDOWN_JOIN_GRACE = 1.0
    CONFLICTION_TIMEOUT = 60

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
        self.timeout_count = 0
        self.last_poll_confliction_time = 0.0
        config = channel.config
        self.admins: Sequence[int] = config["admins"]
        self.telegram_runtime = build_telegram_polling_runtime(
            config,
            channel,
            self.logger,
            self.runtime_started,
            self.runtime_stopped,
        )
        self.msglog_scan = MsgLogScanScheduler(self.telegram_runtime, mtproto, msglog_ingestion, chat_associations, self.logger)
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
        self.telegram_runtime.add_base_dispatchers(config["admins"], self.update_locale)

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

    def error(self, update: object, context: CallbackContext) -> None:
        assert context.error
        error: Exception = context.error
        if "make sure that only one bot instance is running" in str(error):
            now = time.time()
            if now - self.last_poll_confliction_time < self.CONFLICTION_TIMEOUT:
                message = self._("Conflicted polling detected. If this error persists, please ensure you are running only one instance of this Telegram bot.")
                self.logger.critical(message, extra={"event": "telegram_channel.polling_conflict"})
                self.api.send_message(self.admins[0], message)
            self.last_poll_confliction_time = now
            return
        if "Invalid server response" in str(error) and not update:
            self.logger.error("Telegram API returned an invalid server response", extra={"event": "telegram_channel.api_invalid_response"})
            return
        self._handle_error(update, error)

    def _handle_error(self, update: object, error: Exception) -> None:
        try:
            raise error
        except SchedulerStoppedError:
            if self._stopping.is_set():
                self.logger.info(
                    "Ignoring outbound delivery cancellation during Telegram shutdown.",
                    extra={"event": "telegram_channel.outbound_cancelled_during_shutdown"},
                )
            else:
                self._notify_unhandled_error(update, error)
        except telegram.error.Forbidden:
            self.logger.error(
                "Telegram authorization failure while handling update (%s).", type(error).__name__, extra={"event": "telegram_channel.authorization_failed", "error_type": type(error).__name__}
            )
        except telegram.error.BadRequest as request_error:
            assert isinstance(update, Update)
            if request_error.message == "Message is not modified" and update.callback_query:
                self.logger.error("Telegram callback message was not modified", extra={"event": "telegram_channel.callback_not_modified"})
            else:
                self.logger.exception("Telegram message request failed (%s).", type(error).__name__, extra={"event": "telegram_channel.request_failed", "error_type": type(error).__name__})
                self.api.send_message(
                    self.admins[0], self._("Message request is invalid.\n{error}\n<code>{update}</code>").format(error=html.escape(str(error)), update=html.escape(str(update))), parse_mode="HTML"
                )
        except (telegram.error.TimedOut, telegram.error.NetworkError):
            self._handle_network_error(update, error)
        except telegram.error.ChatMigrated as migration:
            self._handle_chat_migration(update, migration)
        except Exception:
            self._notify_unhandled_error(update, error)

    def _handle_network_error(self, update: object, error: Exception) -> None:
        self.timeout_count += 1
        self.logger.error(
            "Telegram network error #%d while handling update (%s).",
            self.timeout_count,
            type(error).__name__,
            extra={"event": "telegram_channel.network_error", "error_type": type(error).__name__, "retry_count": self.timeout_count},
        )
        if isinstance(update, Update) and isinstance(update.message, Message):
            sync_reply_html(
                self.api, update.message, self._("This message is not processed due to poor internet environment of the server.\n<code>{code}</code>").format(code=html.escape(str(error))), quote=True
            )
        interval = self.network_error_prompt_interval()
        if interval > 0 and self.timeout_count % interval == 0:
            self.api.send_message(
                self.admins[0],
                self.ngettext(
                    "<b>EFB Telegram Master channel</b>\nYou may have a poor internet connection on your server. Currently {count} network error is detected.\nFor more details, please refer to the log.",
                    "<b>EFB Telegram Master channel</b>\nYou may have a poor internet connection on your server. Currently {count} network errors are detected.\nFor more details, please refer to the log.",
                    self.timeout_count,
                ).format(count=self.timeout_count),
                parse_mode="HTML",
            )

    def _handle_chat_migration(self, update: object, migration: telegram.error.ChatMigrated) -> None:
        assert isinstance(update, Update) and isinstance(update.message, Message)
        old_id, new_id = ChatID(str(update.message.chat_id)), migration.new_chat_id
        links = self.chat_associations.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, old_id))
        for link in links:
            self.chat_associations.remove_chat_assoc(slave_uid=link)
            self.chat_associations.add_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, ChatID(str(new_id))), slave_uid=link)
        self.api.send_message(
            new_id,
            self.ngettext(
                "Chat migration detected.\nAll {count} remote chat are now linked to this new group.",
                "Chat migration detected.\nAll {count} remote chats are now linked to this new group.",
                len(links),
            ).format(count=len(links)),
        )

    def _notify_unhandled_error(self, update: object, error: Exception) -> None:
        try:
            self.api.send_message(
                self.admins[0],
                self._("EFB Telegram Master channel encountered error <code>{error}</code> caused by update <code>{update}</code>. See log for details.").format(
                    error=html.escape(str(error)), update=html.escape(str(update))
                ),
                parse_mode="HTML",
            )
        except Exception as notification_error:
            self.logger.exception(
                "Failed to send error message through Telegram (%s).",
                type(notification_error).__name__,
                extra={"event": "telegram_channel.error_notification_failed", "error_type": type(notification_error).__name__},
            )
        finally:
            self.logger.exception(
                "Unhandled Telegram bot error while handling update (%s).", type(error).__name__, extra={"event": "telegram_channel.unhandled_error", "error_type": type(error).__name__}
            )

    def stop_channel_resources(self) -> None:
        """Stop delivery work, then the runtime, then join membership workers."""
        self._stopping.set()
        self.logger.info("Stopping Telegram delivery resources", extra={"event": "telegram_bot.stop_started"})
        errors = list(self.api.begin_delivery_shutdown(self.SHUTDOWN_JOIN_GRACE))
        initial_membership_errors = self.api.finish_delivery_shutdown(time.monotonic() + self.SHUTDOWN_JOIN_GRACE)
        try:
            self.telegram_runtime.stop()
        except BaseException as error:
            errors.append(error)
        final_membership_errors = self.api.finish_delivery_shutdown(time.monotonic() + self.SHUTDOWN_DRAIN_TIMEOUT)
        if final_membership_errors:
            errors.extend(final_membership_errors)
        else:
            errors.extend(error for error in initial_membership_errors if not isinstance(error, MembershipProbeShutdownTimeout))
        self.logger.info("Stopped Telegram delivery resources", extra={"event": "telegram_bot.stop_completed"})
        if errors:
            raise TelegramResourceShutdownError(tuple(errors))
