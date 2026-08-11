"""Recipient selection and reusable slave-chat list rendering."""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from typing import Callable, List, Optional, Pattern, Tuple, Union

from ehforwarderbot import coordinator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, ConversationHandler

from . import utils
from .callback_sessions import CallbackSessionStore, ChatListStorage
from .chat import ETMChatMixin
from .constants import Emoji, Flags
from .utils import EFBChannelChatIDStr, TelegramChatID, TelegramMessageID


class RecipientSuggestionService:
    """Render candidate chats and deliver messages after a recipient is selected."""

    def __init__(
        self, bot, callback_sessions: CallbackSessionStore, chat_manager, message_delivery, chats_per_page: Callable[[], int], translate: Callable[[str], str], logger: logging.Logger
    ) -> None:
        self.bot = bot
        self.callback_sessions = callback_sessions
        self.chat_manager = chat_manager
        self.message_delivery = message_delivery
        self.chats_per_page = chats_per_page
        self._ = translate
        self.logger = logger
        self.suggestion_handler: Optional[ConversationHandler] = None

    def set_handler(self, handler: ConversationHandler) -> None:
        self.suggestion_handler = handler

    def _handler(self) -> ConversationHandler:
        assert self.suggestion_handler is not None
        return self.suggestion_handler

    def render_chat_list(
        self,
        storage_id: Tuple[TelegramChatID, TelegramMessageID],
        offset: int = 0,
        pattern: Optional[str] = "",
        source_chats: Optional[List[EFBChannelChatIDStr]] = None,
        filter_availability: bool = True,
    ) -> Tuple[List[str], List[List[InlineKeyboardButton]]]:
        """Return a paginated keyboard and legend for candidate slave chats."""
        self.logger.debug(
            "Generating chat pagination (storage=%s, offset=%s, source_chat_count=%d).",
            storage_id,
            offset,
            len(source_chats) if source_chats else 0,
        )
        legend: List[str] = [
            self._("{0}: Linked").format(Emoji.LINK),
            self._("{0}: User").format(Emoji.USER),
            self._("{0}: Group").format(Emoji.GROUP),
        ]
        chat_list = self.callback_sessions.lookup(storage_id)
        if chat_list is None or chat_list.length == 0:
            re_filter: Union[str, Pattern, None] = None
            if pattern:
                escaped_pattern = re.escape(pattern)
                if pattern == escaped_pattern:
                    re_filter = pattern
                else:
                    try:
                        re_filter = re.compile(pattern, re.DOTALL | re.IGNORECASE)
                    except re.error:
                        re_filter = pattern
            chats: List[ETMChatMixin] = []
            if source_chats:
                for source_chat in source_chats:
                    channel_id, chat_uid, _ = utils.chat_id_str_to_id(source_chat)
                    with suppress(NameError):
                        coordinator.get_module_by_id(channel_id)
                    chat = self.chat_manager.get_chat(channel_id, chat_uid, build_dummy=not filter_availability)
                    if chat is None:
                        self.logger.debug("Chat %s is unavailable for pagination.", source_chat)
                    elif chat.match(re_filter):
                        chats.append(chat)
            else:
                chats = [chat for chat in self.chat_manager.all_chats if chat.match(re_filter)]
            chats.sort(key=lambda chat: chat.last_message_time, reverse=True)
            chat_list = ChatListStorage(chats, offset)
            self.callback_sessions.store(storage_id, chat_list)

        chat_list.offset = offset
        for channel in chat_list.channels.values():
            legend.append(f"{channel.channel_emoji}: {channel.channel_name}")

        buttons: List[List[InlineKeyboardButton]] = []
        per_page = self.chats_per_page()
        for index in range(offset, min(offset + per_page, chat_list.length)):
            chat = chat_list.chats[index]
            mode = Emoji.LINK if chat.linked else ""
            buttons.append([InlineKeyboardButton(f"{chat.channel_emoji}{chat.chat_type_emoji}{mode}: {chat.long_name}", callback_data=f"chat {index}")])

        pages: List[InlineKeyboardButton] = []
        if offset - per_page >= 0:
            pages.append(InlineKeyboardButton(self._("< Prev"), callback_data=f"offset {offset - per_page}"))
        pages.append(InlineKeyboardButton(self._("Cancel"), callback_data=Flags.CANCEL_PROCESS))
        if offset + per_page < chat_list.length:
            pages.append(InlineKeyboardButton(self._("Next >"), callback_data=f"offset {offset + per_page}"))
        buttons.append(pages)
        return legend, buttons

    def register_suggestions(self, update: Update, candidates: List[EFBChannelChatIDStr], chat_id: TelegramChatID, message_id: TelegramMessageID) -> None:
        storage_id = (chat_id, message_id)
        legends, buttons = self.render_chat_list(storage_id, source_chats=candidates)
        if len(buttons) <= 1:
            self.callback_sessions.discard(storage_id)
            return
        storage = self.callback_sessions.lookup(storage_id)
        assert storage is not None
        storage.set_chat_suggestion(update)
        self.bot.edit_message_text(
            text=self._("Error: No recipient specified.\nPlease reply to a previous message, or choose a recipient:\n\nLegend:\n") + "\n".join(legends),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        self.callback_sessions.set_state(self._handler(), storage_id, Flags.SUGGEST_RECIPIENTS)

    def suggested_recipient(self, update: Update, context: CallbackContext) -> int:
        """Deliver an undirected Telegram message to the selected slave chat."""
        assert update.effective_message and update.effective_chat and update.callback_query and update.callback_query.data
        chat_id = TelegramChatID(update.effective_chat.id)
        message_id = TelegramMessageID(update.effective_message.message_id)
        callback_data = update.callback_query.data
        callback_query_id = update.callback_query.id
        storage_id = (chat_id, message_id)
        expired_text = self._("Error: No recipient specified.\nPlease reply to a previous message.\n\nSession expired, please try again.")
        invalid_text = self._("Error: No recipient specified.\nPlease reply to a previous message.\n\nInvalid parameter ({0}).").format(callback_data)
        if callback_data.split(maxsplit=1)[0] == "chat":
            callback_index = self.callback_sessions.parse_index(callback_data, "chat")
            storage = self.callback_sessions.expired(self._handler(), storage_id, callback_query_id, expired_text)
            if storage is None:
                return ConversationHandler.END
            if callback_index is None or storage.update is None or not self.callback_sessions.is_current_selection(storage, callback_index):
                return self.callback_sessions.end(self._handler(), storage_id, callback_query_id, invalid_text)
            slave_chat = storage.chats[callback_index]
            self.message_delivery.deliver(storage.update, context, utils.chat_id_to_str(chat=slave_chat))
            self.bot.edit_message_text(text=self._("Delivering the message to {0}.").format(slave_chat.full_name), chat_id=chat_id, message_id=message_id)
        elif callback_data == Flags.CANCEL_PROCESS:
            self.bot.edit_message_text(text=self._("Error: No recipient specified.\nPlease reply to a previous message."), chat_id=chat_id, message_id=message_id)
        else:
            self.bot.edit_message_text(text=invalid_text, chat_id=chat_id, message_id=message_id)
        self.callback_sessions.clear(self._handler(), storage_id)
        self.bot.answer_callback_query(callback_query_id)
        return ConversationHandler.END
