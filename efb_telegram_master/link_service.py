# coding=utf-8

import html
import logging
import urllib.parse
from typing import Callable, List, Optional, Tuple

import telegram
from ehforwarderbot.types import ChatID, ModuleID
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import CallbackContext, ConversationHandler

from . import utils
from .callback_sessions import CallbackSessionStore, ChatListStorage
from .chat import ETMChatMixin
from .constants import Flags
from .ptb_compat import get_forwarded_chat, sync_reply_text
from .utils import EFBChannelChatIDStr, TelegramChatID, TelegramMessageID, TelegramTopicID


def _bounded_error_message(error: BaseException) -> str:
    return str(error)[:200]


class LinkService:
    def __init__(
        self,
        bot,
        runtime,
        channel_id: ModuleID,
        multiple_slave_chats: bool,
        msglogs,
        chat_associations,
        chat_manager,
        callback_sessions: CallbackSessionStore,
        render_chat_list: Callable,
        translate: Callable[[str], str],
        ngettext: Callable,
        logger: logging.Logger,
    ):
        self.bot = bot
        self.runtime = runtime
        self.channel_id = channel_id
        self.multiple_slave_chats = multiple_slave_chats
        self.msglogs = msglogs
        self.chat_associations = chat_associations
        self.chat_manager = chat_manager
        self.callback_sessions = callback_sessions
        self.render_chat_list = render_chat_list
        self._ = translate
        self.ngettext = ngettext
        self.logger = logger
        self.link_handler: Optional[ConversationHandler] = None

    def set_handler(self, handler: ConversationHandler) -> None:
        self.link_handler = handler

    def _handler(self) -> ConversationHandler:
        assert self.link_handler is not None
        return self.link_handler

    def _get_bot_user(self) -> telegram.User:
        bot_user = self.runtime.me
        if bot_user is None:
            bot_user = self.bot.get_me()
            self.runtime.me = bot_user
        return bot_user

    def pre_link_check(self, message: Message):
        """Check if the bot would work properly in a linked group.
        If potential error is found, reply error messages to the user.

        Args:
            message: /link command message.
        """
        err_msg = []

        # Runtime identity is refreshed here after a startup credential change.
        # to check if user has updated the setting with bot father.
        # Assuming user will not revert the settings back.

        # Refresh bot status if any of the settings is not enabled.
        bot_user = self._get_bot_user()
        if not bot_user.can_join_groups or not bot_user.can_read_all_group_messages:
            bot_user = self.bot.get_me()
            self.runtime.me = bot_user

        if not bot_user.can_join_groups:
            err_msg.append(self._("This bot cannot join groups. Chat linking might not work properly. Please enable this setting with @BotFather."))
        if not bot_user.can_read_all_group_messages:
            err_msg.append(self._("This bot cannot read all messages in a group chat. Message delivery in linked groups might not work properly. Please adjust my privacy settings with @BotFather."))

        if err_msg:
            sync_reply_text(self.bot, message, "\n".join(err_msg))

    def show_list(self, update: Update, context: CallbackContext):
        """
        Show the list of available chats for linking.
        Triggered by `/link`.

        When triggered in private chat, it shows all chats available,
        or list of remote chats linked to the group otherwise.
        If no chat is linked to this group, then the bot messages
        the full list privately.
        """
        assert isinstance(update, Update)
        assert update.effective_message
        if update.effective_user is None:
            return ConversationHandler.END

        args = context.args or []
        message: Message = update.effective_message

        # Perform pre-link check
        self.pre_link_check(message)

        # Send link confirmation message when replying to a Telegram message
        # that is recorded in database.
        if message.reply_to_message:
            rtm: Message = message.reply_to_message
            msg_log = self.msglogs.get_msg_log(master_msg_id=utils.message_id_to_str(chat_id=TelegramChatID(rtm.chat_id), message_id=TelegramMessageID(rtm.message_id)))
            if msg_log:
                channel_id, chat_id, _ = utils.chat_id_str_to_id(EFBChannelChatIDStr(msg_log.slave_origin_uid))
                chat: ETMChatMixin = self.chat_manager.get_chat(channel_id, chat_id, build_dummy=True)
                tg_chat_id = TelegramChatID(message.chat_id)
                tg_msg_id = TelegramMessageID(sync_reply_text(self.bot, message, self._("Processing..."), _force_main_bot=True).message_id)
                storage_id: Tuple[TelegramChatID, TelegramMessageID] = (tg_chat_id, tg_msg_id)
                self.callback_sessions.start(self._handler(), storage_id, Flags.LINK_EXEC, update.effective_user.id, ChatListStorage([chat]))
                return self.build_action(chat, tg_chat_id, tg_msg_id)
            if message.message_thread_id:
                topic = message.message_thread_id
                if topic:
                    slave_origin_uid = self.chat_associations.get_topic_slave(topic_chat_id=TelegramChatID(message.chat_id), message_thread_id=TelegramTopicID(topic))
                    if slave_origin_uid:
                        channel_id, chat_id, _ = utils.chat_id_str_to_id(slave_origin_uid)
                        topic_chat: ETMChatMixin = self.chat_manager.get_chat(channel_id, chat_id, build_dummy=True)
                        topic_tg_chat_id = TelegramChatID(message.chat_id)
                        topic_tg_msg_id = TelegramMessageID(sync_reply_text(self.bot, message, self._("Processing..."), _force_main_bot=True).message_id)
                        topic_storage_id: Tuple[TelegramChatID, TelegramMessageID] = (topic_tg_chat_id, topic_tg_msg_id)
                        self.callback_sessions.start(self._handler(), topic_storage_id, Flags.LINK_EXEC, update.effective_user.id, ChatListStorage([topic_chat]))
                        return self.build_action(topic_chat, topic_tg_chat_id, topic_tg_msg_id)

        if message.chat.type != ChatType.PRIVATE:
            links = self.chat_associations.get_chat_assoc(master_uid=utils.chat_id_to_str(self.channel_id, ChatID(str(message.chat.id))))
            if links:
                return self.render_list(TelegramChatID(message.chat.id), update.effective_user.id, pattern=" ".join(args), chats=links, filter_availability=False)
        elif (forwarded_chat := get_forwarded_chat(message)) and forwarded_chat.type == ChatType.CHANNEL:
            chat_id = ChatID(str(forwarded_chat.id))
            links = self.chat_associations.get_chat_assoc(master_uid=utils.chat_id_to_str(self.channel_id, chat_id))
            if links:
                return self.render_list(TelegramChatID(message.chat.id), update.effective_user.id, pattern=" ".join(args), chats=links, filter_availability=False)
        assert message.from_user
        return self.render_list(TelegramChatID(message.from_user.id), update.effective_user.id, pattern=" ".join(args))

    def render_list(
        self,
        chat_id: TelegramChatID,
        owner_id: int,
        message_id: Optional[TelegramMessageID] = None,
        offset: int = 0,
        pattern: str = "",
        chats: Optional[List[EFBChannelChatIDStr]] = None,
        filter_availability: bool = True,
    ):
        """
        Generate the list for chat linking, and update it to a message.

        Args:
            chat_id: Chat ID
            message_id: ID of message to be updated, None to send a new message.
            offset: Offset for pagination.
            pattern (str): Regex expression to filter chats.
            chats (List[str]): Specified chats to link
            filter_availability (bool): Whether to show only chats that are available.
                Only works when ``chats`` are specified.

        Returns:
            int: The next state
        """

        if message_id is None:
            message_id = self.bot.send_message(chat_id, self._("Processing..."), _force_main_bot=True).message_id
        self.bot.send_chat_action(chat_id, ChatAction.TYPING)
        if chats:
            msg_text = self._("This Telegram group is currently linked with...")
        else:
            msg_text = self._("Please choose the chat you want to link with...")
        msg_text += self._("\n\nLegend:\n")

        legend, chat_btn_list = self.render_chat_list((chat_id, message_id), owner_id, offset, pattern=pattern, source_chats=chats, filter_availability=filter_availability)
        for i in legend:
            msg_text += "%s\n" % i

        self.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=msg_text, reply_markup=InlineKeyboardMarkup(chat_btn_list))

        self.callback_sessions.set_state(self._handler(), (chat_id, message_id), Flags.LINK_CONFIRM)

        return Flags.LINK_CONFIRM

    def confirm(self, update: Update, context: CallbackContext) -> int:
        """
        Confirmation of chat linking. Triggered by callback message on status `Flags.CONFIRM_LINK`.

        A part of ``/link`` conversation handler.

        Returns:
            int: Next status
        """
        assert isinstance(update, Update)
        assert update.effective_chat
        assert update.effective_message
        assert update.callback_query
        assert update.callback_query.data

        tg_chat_id = TelegramChatID(update.effective_chat.id)
        tg_msg_id = TelegramMessageID(update.effective_message.message_id)
        callback_uid: str = update.callback_query.data
        storage_id = (tg_chat_id, tg_msg_id)
        callback_user = update.callback_query.from_user
        effective_user = update.effective_user
        if self.callback_sessions.contains(storage_id) and (effective_user is None or callback_user.id != effective_user.id or not self.callback_sessions.is_owned_by(storage_id, effective_user.id)):
            self.bot.answer_callback_query(update.callback_query.id, text=self._("Session expired or unknown parameter. (SE02)"))
            return Flags.LINK_CONFIRM
        expired_text = self._("Session expired. Please try again. (SE01)")
        invalid_text = self._("Invalid parameter ({0}). (IP01)").format(callback_uid)
        if callback_uid.split(maxsplit=1)[0] == "offset":
            offset = self.callback_sessions.parse_index(callback_uid, "offset")
            storage = self.callback_sessions.expired(self._handler(), storage_id, update.callback_query.id, expired_text)
            if storage is None:
                return ConversationHandler.END
            if offset is None or not self.callback_sessions.is_valid_page_offset(storage, offset):
                return self.callback_sessions.end(self._handler(), storage_id, update.callback_query.id, invalid_text)
            # Offer a new page of chats
            assert effective_user is not None
            self.bot.answer_callback_query(update.callback_query.id)
            return self.render_list(tg_chat_id, effective_user.id, message_id=tg_msg_id, offset=offset)

        if callback_uid == Flags.CANCEL_PROCESS:
            # Terminate the process
            txt = self._("Cancelled.")
            return self.callback_sessions.end(self._handler(), storage_id, update.callback_query.id, txt)

        if callback_uid[:4] != "chat":
            # The only possible command now is "chat".
            return self.callback_sessions.end(self._handler(), storage_id, update.callback_query.id, invalid_text)

        callback_idx = self.callback_sessions.parse_index(callback_uid, "chat")
        storage = self.callback_sessions.expired(self._handler(), storage_id, update.callback_query.id, expired_text)
        if storage is None:
            return ConversationHandler.END
        if callback_idx is None or not self.callback_sessions.is_current_selection(storage, callback_idx):
            return self.callback_sessions.end(self._handler(), storage_id, update.callback_query.id, invalid_text)
        chat: ETMChatMixin = storage.chats[callback_idx]

        self.bot.answer_callback_query(update.callback_query.id)
        self.build_action(chat, tg_chat_id, tg_msg_id)
        return Flags.LINK_EXEC

    def build_action(self, chat: ETMChatMixin, tg_chat_id: TelegramChatID, tg_msg_id: TelegramMessageID):
        chat_display_name = chat.full_name
        storage = self.callback_sessions.lookup((tg_chat_id, tg_msg_id))
        assert storage is not None
        storage.chats = [chat]
        txt = self._("You've selected chat {0}.").format(html.escape(chat_display_name))
        if chat.linked:
            txt += self._("\nThis chat has already linked to Telegram.")
        txt += self._("\nWhat would you like to do?\n\n<i>* If the link button doesn't work for you, please try to link manually.</i>")
        bot_username = self._get_bot_user().username
        assert bot_username is not None
        link_url = f"https://telegram.me/{bot_username}?startgroup={urllib.parse.quote(utils.b64en(utils.message_id_to_str(tg_chat_id, tg_msg_id)))}"
        self.logger.debug("Generated Telegram start link for chat %s message %s.", tg_chat_id, tg_msg_id)
        if chat.linked:
            btn_list = [InlineKeyboardButton(self._("Relink"), url=link_url), InlineKeyboardButton(self._("Restore"), callback_data="unlink 0")]
        else:
            btn_list = [InlineKeyboardButton(self._("Link"), url=link_url)]
        btn_list.append(InlineKeyboardButton(self._("Manual {link_or_relink}").format(link_or_relink=btn_list[0].text), callback_data="manual_link 0"))
        buttons = [btn_list, [InlineKeyboardButton(self._("Cancel"), callback_data=Flags.CANCEL_PROCESS)]]

        self.bot.edit_message_text(text=txt, chat_id=tg_chat_id, message_id=tg_msg_id, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML")

    def execute(self, update: Update, context: CallbackContext) -> int:
        """
        Action to link a chat. Triggered by callback message with status `Flags.LINK_EXEC`.
        """
        assert isinstance(update, Update)
        assert update.effective_chat
        assert update.effective_message
        assert update.callback_query
        assert update.callback_query.data

        tg_chat_id = TelegramChatID(update.effective_chat.id)
        tg_msg_id = TelegramMessageID(update.effective_message.message_id)
        callback_uid = update.callback_query.data
        callback_user = update.callback_query.from_user
        effective_user = update.effective_user
        storage_id = (tg_chat_id, tg_msg_id)
        if self.callback_sessions.contains(storage_id) and (effective_user is None or callback_user.id != effective_user.id or not self.callback_sessions.is_owned_by(storage_id, effective_user.id)):
            self.bot.answer_callback_query(update.callback_query.id, text=self._("Session expired or unknown parameter. (SE02)"))
            return Flags.LINK_EXEC

        if callback_uid == Flags.CANCEL_PROCESS:
            txt = self._("Cancelled.")
            return self.callback_sessions.end(self._handler(), storage_id, update.callback_query.id, txt)

        expired_text = self._("Session expired. Please try again. (SE01)")
        callback_parts = callback_uid.split()
        if len(callback_parts) != 2:
            txt = self._("Command ‘{command}’ ({query}) is not recognised, please try again.").format(command=callback_parts[0] if callback_parts else "", query=callback_uid)
            return self.callback_sessions.end(self._handler(), storage_id, update.callback_query.id, txt)
        cmd, _ = callback_parts
        callback_idx = self.callback_sessions.parse_index(callback_uid, cmd)
        storage = self.callback_sessions.expired(self._handler(), storage_id, update.callback_query.id, expired_text)
        if storage is None:
            return ConversationHandler.END
        if callback_idx is None or not self.callback_sessions.is_current_selection(storage, callback_idx):
            txt = self._("Invalid parameter ({0}). (IP01)").format(callback_uid)
            return self.callback_sessions.end(self._handler(), storage_id, update.callback_query.id, txt)
        chat: ETMChatMixin = storage.chats[callback_idx]
        chat_display_name = chat.full_name
        if cmd == "unlink":
            chat.unlink()
            txt = self._("Chat {} is restored.").format(chat_display_name)
            self.bot.edit_message_text(text=txt, chat_id=tg_chat_id, message_id=tg_msg_id)
        elif cmd == "manual_link":
            txt = self._(
                "To link {chat_display_name} manually, please:\n\n"
                "1. Add me to the Telegram Group you want to link to.\n"
                "2. Send one of the following codes:\n\n"
                "<code>/start {code}</code>\n"
                "<code>/start {code} true</code>\n"
                "<code>/start {code} false</code>\n\n"
                "<i>* The second argument can override backfill behaviour:</i>\n"
                "<i>* true/on/1  -> always backfill</i>\n"
                "<i>* false/off/0 -> never backfill</i>\n"
                "3. Then I would notify you if the chat is linked successfully.\n"
                "\n"
                "<i>* To link a channel, send one of the codes above to your channel, "
                "and forward it to the bot. Note that the bot will not process any "
                "message others sent in channels.</i>"
            ).format(chat_display_name=html.escape(chat_display_name), code=html.escape(utils.b64en(utils.message_id_to_str(tg_chat_id, tg_msg_id))))
            self.bot.edit_message_text(
                text=txt, chat_id=tg_chat_id, message_id=tg_msg_id, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(self._("Cancel"), callback_data=Flags.CANCEL_PROCESS)]]), parse_mode="HTML"
            )
            return Flags.LINK_EXEC
        else:
            txt = self._("Command ‘{command}’ ({query}) is not recognised, please try again.").format(command=cmd, query=callback_uid)
            self.bot.edit_message_text(text=txt, chat_id=tg_chat_id, message_id=tg_msg_id)
        self.bot.answer_callback_query(update.callback_query.id)
        self.callback_sessions.clear(self._handler(), storage_id)
        return ConversationHandler.END
