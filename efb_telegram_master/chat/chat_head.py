"""Chat-head command and callback handling."""

from __future__ import annotations

from typing import Callable, List, Optional

from ehforwarderbot import Channel, coordinator
from ehforwarderbot.constants import MsgType
from ehforwarderbot.types import ChatID, MessageID
from telegram import InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import CallbackContext, ConversationHandler

from efb_telegram_master.chat.chat import ETMChatMixin
from efb_telegram_master.core import utils
from efb_telegram_master.core.constants import Flags
from efb_telegram_master.core.utils import EFBChannelChatIDStr, TelegramChatID, TelegramMessageID
from efb_telegram_master.delivery.message import ETMMsg
from efb_telegram_master.delivery.msg_type import TGMsgType
from efb_telegram_master.link.callback_sessions import CallbackSessionStore
from efb_telegram_master.persistence.msglog_repository import MsgLogRepository


class ChatHeadService:
    """Create chat-head messages that set the reply destination for a chat."""

    def __init__(
        self,
        bot,
        callback_sessions: CallbackSessionStore,
        chat_associations,
        chat_manager,
        source_channel: Channel,
        msglogs: MsgLogRepository,
        render_chat_list: Callable,
        translate: Callable[[str], str],
        conversation_handler: ConversationHandler,
    ) -> None:
        self.bot = bot
        self.callback_sessions = callback_sessions
        self.chat_associations = chat_associations
        self.chat_manager = chat_manager
        self.source_channel = source_channel
        self.channel_id = source_channel.channel_id
        self.msglogs = msglogs
        self.render_chat_list = render_chat_list
        self._ = translate
        self._conversation_handler = conversation_handler

    def start_chat_list(self, update: Update, context: CallbackContext):
        assert update.message
        if update.effective_user is None:
            return ConversationHandler.END
        chats = None
        if update.message.chat.type != ChatType.PRIVATE:
            chats = self.chat_associations.get_chat_assoc(master_uid=utils.chat_id_to_str(self.channel_id, ChatID(str(update.message.chat_id)))) or None
        if chats:
            target = TelegramChatID(update.message.chat_id)
        elif update.message.from_user:
            target = TelegramChatID(update.message.from_user.id)
        else:
            raise RuntimeError("No target chat is found when generating chat list.")
        return self.render_chat_head(target, update.effective_user.id, pattern=" ".join(context.args or []), chats=chats)

    def render_chat_head(
        self, chat_id: TelegramChatID, owner_id: int, message_id: Optional[TelegramMessageID] = None, offset: int = 0, pattern: str = "", chats: Optional[List[EFBChannelChatIDStr]] = None
    ):
        if message_id is None:
            message_id = self.bot.send_message(chat_id, text=self._("Processing..."), _force_main_bot=True).message_id
        self.bot.send_chat_action(chat_id, ChatAction.TYPING)
        if chats and len(chats) == 1:
            slave_channel_id, slave_chat_id, _ = utils.chat_id_str_to_id(chats[0])
            chat = self.chat_manager.get_chat(slave_channel_id, slave_chat_id)
            if chat:
                text = self._("This group is linked to {0}. Send a message to this group to deliver it to the chat.\nDo NOT reply to this system message.").format(chat.full_name)
            else:
                try:
                    channel = coordinator.get_module_by_id(slave_channel_id)
                    name = channel.channel_name if isinstance(channel, Channel) else channel.middleware_name
                    text = self._(
                        "This group is linked to an unknown chat ({chat_id}) on channel {channel_name} ({channel_id}). Possibly you can no longer reach this chat. Send /unlink_all to unlink all chats from this group."
                    ).format(channel_name=name, channel_id=slave_channel_id, chat_id=slave_chat_id)
                except NameError:
                    text = self._(
                        "This group is linked to a chat from a channel that is not activated ({channel_id}, {chat_id}). You cannot reach this chat unless the channel is enabled. Send /unlink_all to unlink all chats from this group."
                    ).format(channel_id=slave_channel_id, chat_id=slave_chat_id)
            self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
            return ConversationHandler.END
        text = self._("This Telegram group is linked to the following chats, choose one to start a conversation with.") if chats else "Choose a chat you want to start a conversation with."
        legend, buttons = self.render_chat_list((chat_id, message_id), owner_id, offset, pattern=pattern, source_chats=chats)
        text += self._("\n\nLegend:\n") + "".join(f"{entry}\n" for entry in legend)
        self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=InlineKeyboardMarkup(buttons))
        self.callback_sessions.set_state(self._conversation_handler, (chat_id, message_id), Flags.CHAT_HEAD_CONFIRM)

    def make_chat_head(self, update: Update, context: CallbackContext) -> int:
        assert update.effective_chat and update.effective_message and update.callback_query and update.callback_query.data
        chat_id = TelegramChatID(update.effective_chat.id)
        message_id = TelegramMessageID(update.effective_message.message_id)
        callback_data = update.callback_query.data
        storage_id = (chat_id, message_id)
        callback_user = update.callback_query.from_user
        effective_user = update.effective_user
        if self.callback_sessions.contains(storage_id) and (effective_user is None or callback_user.id != effective_user.id or not self.callback_sessions.is_owned_by(storage_id, effective_user.id)):
            self.bot.answer_callback_query(update.callback_query.id, text=self._("Session expired or unknown parameter. (SE02)"))
            return Flags.CHAT_HEAD_CONFIRM
        expired_text = self._("Session expired. Please try again. (SE01)")
        invalid_text = self._("Invalid command. ({0})").format(callback_data)
        if callback_data.split(maxsplit=1)[0] == "offset":
            offset = self.callback_sessions.parse_index(callback_data, "offset")
            storage = self.callback_sessions.expired(self._conversation_handler, storage_id, update.callback_query.id, expired_text)
            if storage is None:
                return ConversationHandler.END
            if offset is None or not self.callback_sessions.is_valid_page_offset(storage, offset):
                return self.callback_sessions.end(self._conversation_handler, storage_id, update.callback_query.id, invalid_text)
            assert effective_user is not None
            self.bot.answer_callback_query(update.callback_query.id)
            return self.render_chat_head(chat_id, effective_user.id, message_id, offset)
        if callback_data == Flags.CANCEL_PROCESS:
            return self.callback_sessions.end(self._conversation_handler, storage_id, update.callback_query.id, self._("Cancelled."))
        if not callback_data.startswith("chat "):
            return self.callback_sessions.end(self._conversation_handler, storage_id, update.callback_query.id, invalid_text)
        callback_index = self.callback_sessions.parse_index(callback_data, "chat")
        storage = self.callback_sessions.expired(self._conversation_handler, storage_id, update.callback_query.id, expired_text)
        if storage is None:
            return ConversationHandler.END
        if callback_index is None or not self.callback_sessions.is_current_selection(storage, callback_index):
            return self.callback_sessions.end(self._conversation_handler, storage_id, update.callback_query.id, invalid_text)
        chat: ETMChatMixin = storage.chats[callback_index]
        self.callback_sessions.clear(self._conversation_handler, storage_id)
        text = self._("Reply to this message to chat with {0}.").format(chat.full_name)
        self._record_chat_head(chat, update.effective_message, text)
        self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
        self.bot.answer_callback_query(update.callback_query.id)
        return ConversationHandler.END

    def _record_chat_head(self, chat: ETMChatMixin, message: Message, text: str) -> None:
        chat_head = ETMMsg()
        chat_head.chat = chat
        chat_head.author = chat.self or chat.add_self()
        chat_head.uid = MessageID("__chathead__")
        chat_head.type = MsgType.Text
        chat_head.text = text
        chat_head.type_telegram = TGMsgType.Text
        chat_head.deliver_to = self.source_channel
        self.msglogs.add_or_update_message_log(chat_head, message)
