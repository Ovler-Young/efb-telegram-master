"""Route Telegram callback errors to recovery and notification behavior."""

from __future__ import annotations

import html
import logging
import threading
import time
from collections.abc import Callable, Sequence

import telegram
from ehforwarderbot.types import ChatID, ModuleID
from telegram import Message, Update
from telegram.ext import CallbackContext

from .. import utils as etm_utils
from ..outbound_types import SchedulerStoppedError
from ..persistence.chat_association_repository import ChatAssociationRepository
from ..ptb_compat import sync_reply_html
from .telegram_api import TelegramAPI


class TelegramErrorRouter:
    """Classify Telegram callback errors and notify affected users."""

    CONFLICTION_TIMEOUT = 60

    def __init__(
        self,
        api: TelegramAPI,
        admins: Sequence[int],
        chat_associations: ChatAssociationRepository,
        channel_id: ModuleID,
        network_error_prompt_interval: Callable[[], int],
        translate: Callable[[str], str],
        ngettext: Callable[[str, str, int], str],
        stopping: threading.Event,
        logger: logging.Logger,
    ) -> None:
        self.api = api
        self.admins = admins
        self.chat_associations = chat_associations
        self.channel_id = channel_id
        self.network_error_prompt_interval = network_error_prompt_interval
        self._translate = translate
        self._ngettext = ngettext
        self._stopping = stopping
        self.logger = logger
        self.timeout_count = 0
        self.last_poll_confliction_time = 0.0

    def handle(self, update: object, context: CallbackContext) -> None:
        assert context.error
        error: Exception = context.error
        if "make sure that only one bot instance is running" in str(error):
            now = time.time()
            if now - self.last_poll_confliction_time < self.CONFLICTION_TIMEOUT:
                message = self._translate("Conflicted polling detected. If this error persists, please ensure you are running only one instance of this Telegram bot.")
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
                    self.admins[0],
                    self._translate("Message request is invalid.\n{error}\n<code>{update}</code>").format(error=html.escape(str(error)), update=html.escape(str(update))),
                    parse_mode="HTML",
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
                self.api,
                update.message,
                self._translate("This message is not processed due to poor internet environment of the server.\n<code>{code}</code>").format(code=html.escape(str(error))),
                quote=True,
            )
        interval = self.network_error_prompt_interval()
        if interval > 0 and self.timeout_count % interval == 0:
            self.api.send_message(
                self.admins[0],
                self._ngettext(
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
            self.chat_associations.add_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, ChatID(str(new_id))), slave_uid=link, multiple_slave=True)
        self.api.send_message(
            new_id,
            self._ngettext(
                "Chat migration detected.\nAll {count} remote chat are now linked to this new group.",
                "Chat migration detected.\nAll {count} remote chats are now linked to this new group.",
                len(links),
            ).format(count=len(links)),
        )

    def _notify_unhandled_error(self, update: object, error: Exception) -> None:
        try:
            self.api.send_message(
                self.admins[0],
                self._translate("EFB Telegram Master channel encountered error <code>{error}</code> caused by update <code>{update}</code>. See log for details.").format(
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
