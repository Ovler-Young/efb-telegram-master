"""Telegram destination resolution for inbound messages."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from ehforwarderbot.types import ChatID, ModuleID
from telegram import Update
from telegram.ext import CallbackContext

from . import utils
from .chat_destination_cache import ChatDestinationCache
from .ptb_compat import sync_reply_text
from .utils import EFBChannelChatIDStr, TelegramChatID, TelegramMessageID


class MasterMessageInbound:
    """Resolve a Telegram update's destination before requesting delivery."""

    def __init__(
        self,
        bot,
        msglogs,
        chat_associations,
        chat_dest_cache: ChatDestinationCache,
        chat_manager,
        recipient_suggestions,
        delivery,
        channel_id: ModuleID,
        localize: Callable[[str], str],
        flags: Callable[[str], object],
        logger: logging.Logger,
    ) -> None:
        self.bot = bot
        self.msglogs = msglogs
        self.chat_associations = chat_associations
        self.chat_dest_cache = chat_dest_cache
        self.chat_manager = chat_manager
        self.recipient_suggestions = recipient_suggestions
        self.delivery = delivery
        self.channel_id = channel_id
        self.localize = localize
        self.flags = flags
        self.logger = logger

    def msg(self, update: Update, context: CallbackContext) -> None:
        assert isinstance(update, Update)
        assert update.effective_message and update.effective_chat
        message = update.effective_message
        message_key = utils.message_id_to_str(update=update)
        master_chat_uid = utils.chat_id_to_str(self.channel_id, ChatID(str(message.chat.id)))
        linked_slave_chats: Optional[list[EFBChannelChatIDStr]] = None

        def get_linked_slave_chats() -> list[EFBChannelChatIDStr]:
            nonlocal linked_slave_chats
            if linked_slave_chats is None:
                linked_slave_chats = self.chat_associations.get_chat_assoc(master_uid=master_chat_uid)
            return linked_slave_chats

        destination: Optional[EFBChannelChatIDStr] = None
        edited = None
        quote = False
        if update.edited_message or update.edited_channel_post:
            self.logger.debug("[%s] Message is edited: %s", message_key, message.edit_date)
            message_log = self.msglogs.get_msg_log(master_msg_id=utils.message_id_to_str(update=update))
            if message_log and message_log.provenance == "mtproto_ingested":
                self.logger.info("Ignoring edit for ingested synthetic message %s.", message_key)
                return
            if not message_log or message_log.slave_message_id == self.msglogs.FAIL_FLAG:
                sync_reply_text(self.bot, message, self.localize("Error: This message cannot be edited, and thus is not sent. (ME01)"), quote=True)
                return
            destination = EFBChannelChatIDStr(message_log.slave_origin_uid)
            edited = message_log
            quote = message_log.build_etm_msg(self.chat_manager).target is not None
        if destination is None:
            chats = get_linked_slave_chats()
            if len(chats) == 1:
                destination = chats[0]
            if destination:
                quote = message.reply_to_message is not None
                if message.chat.is_forum:
                    ideal_thread_id = self.chat_associations.get_topic_thread_id(slave_uid=destination, topic_chat_id=TelegramChatID(update.effective_chat.id))
                    if ideal_thread_id and ideal_thread_id != message.message_thread_id:
                        return
        if destination is None and message.chat.is_forum:
            topic_destinations = self.chat_associations.get_topic_slaves(topic_chat_id=TelegramChatID(message.chat.id))
            thread_id = message.message_thread_id
            if thread_id and topic_destinations:
                for candidate, topic_id in topic_destinations:
                    if topic_id == thread_id:
                        destination = candidate
                        reply_to = message.reply_to_message
                        quote = reply_to is not None and reply_to.message_id != reply_to.message_thread_id
                        break
                if destination is None:
                    self.logger.debug("[%s] Ignored message as it's a topic which wasn't created by this bot", message_key)
                    return
            elif topic_destinations is not None and len(get_linked_slave_chats()) == len(topic_destinations):
                return
        if destination is None:
            quote = False
            reply_to = message.reply_to_message
            cached_destination = self.chat_dest_cache.get(str(message.chat.id))
            if reply_to:
                destination_log = self.msglogs.get_msg_log(master_msg_id=utils.message_id_to_str(TelegramChatID(reply_to.chat.id), TelegramMessageID(reply_to.message_id)))
                if destination_log:
                    destination = EFBChannelChatIDStr(destination_log.slave_origin_uid)
                    self.chat_dest_cache.set(str(message.chat.id), destination)
                    self.logger.debug("[%s] Quoted message is found in database with destination: %s", message_key, destination)
            elif cached_destination:
                self.logger.debug("[%s] Cached destination found: %s", message_key, cached_destination)
                destination = EFBChannelChatIDStr(cached_destination)
                self._send_cached_chat_warning(update, TelegramChatID(message.chat.id), cached_destination)
        self.logger.debug("[%s] Destination chat = %s", message_key, destination)
        if destination is not None:
            self.delivery.deliver(update, context, destination, quote=quote, edited=edited)
            return
        self.logger.debug("[%s] Destination is not found for this message", message_key)
        candidates = self.msglogs.get_recent_slave_chats(TelegramChatID(message.chat.id), limit=5) or get_linked_slave_chats()[:5]
        if candidates:
            error_message = sync_reply_text(self.bot, message, self.localize("Error: No recipient specified.\nPlease reply to a previous message. (MS01)"), quote=True)
            self.recipient_suggestions.register_suggestions(update, candidates, TelegramChatID(update.effective_chat.id), TelegramMessageID(error_message.message_id))
        else:
            sync_reply_text(self.bot, message, self.localize("Error: No recipient specified.\nPlease reply to a previous message. (MS02)"), quote=True)

    def _send_cached_chat_warning(self, update: Update, cache_key: TelegramChatID, cached_destination: EFBChannelChatIDStr) -> None:
        assert update.effective_message
        if self.flags("send_to_last_chat") != "warn" or self.chat_dest_cache.is_warned(str(cache_key)):
            return
        self.chat_dest_cache.set_warned(str(cache_key))
        module_id, chat_id, _ = utils.chat_id_str_to_id(cached_destination)
        destination_chat = self.chat_manager.get_chat(module_id, chat_id)
        sync_reply_text(self.bot, update.effective_message, self.localize("This message is sent to “{dest}” with quick reply feature.\n\nLearn more about how this works, how to turn this feature off, and how to stop this warning at {docs}.").format(dest=destination_chat.full_name if destination_chat else cached_destination, docs="https://etm.1a23.studio/"), quote=True, disable_web_page_preview=True)
