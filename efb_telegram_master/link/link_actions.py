# coding=utf-8

import html
import logging
import urllib.parse
from typing import Callable

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ConversationHandler

from efb_telegram_master.chat.chat import ETMChatMixin
from efb_telegram_master.core import utils
from efb_telegram_master.core.constants import Flags
from efb_telegram_master.core.utils import TelegramChatID, TelegramMessageID


def get_bot_user(bot, runtime) -> telegram.User:
    bot_user = runtime.me
    if bot_user is None:
        bot_user = bot.get_me()
        runtime.me = bot_user
    return bot_user


class LinkActionService:
    """Render and execute the action menu for an already selected chat."""

    def __init__(self, bot, runtime, translate: Callable[[str], str], logger: logging.Logger):
        self.bot = bot
        self.runtime = runtime
        self._ = translate
        self.logger = logger

    def render(self, chat: ETMChatMixin, tg_chat_id: TelegramChatID, tg_msg_id: TelegramMessageID) -> None:
        chat_display_name = chat.full_name
        txt = self._("You've selected chat {0}.").format(html.escape(chat_display_name))
        if chat.linked:
            txt += self._("\nThis chat has already linked to Telegram.")
        txt += self._("\nWhat would you like to do?\n\n<i>* If the link button doesn't work for you, please try to link manually.</i>")
        bot_username = get_bot_user(self.bot, self.runtime).username
        assert bot_username is not None
        link_url = f"https://telegram.me/{bot_username}?startgroup={urllib.parse.quote(utils.b64en(utils.message_id_to_str(tg_chat_id, tg_msg_id)))}"
        self.logger.debug("Generated Telegram start link for chat %s message %s.", tg_chat_id, tg_msg_id)
        if chat.linked:
            btn_list = [InlineKeyboardButton(self._("Relink"), url=link_url), InlineKeyboardButton(self._("Restore"), callback_data="unlink 0")]
        else:
            btn_list = [InlineKeyboardButton(self._("Link"), url=link_url)]
        btn_list.append(InlineKeyboardButton(self._("Manual {link_or_relink}").format(link_or_relink=btn_list[0].text), callback_data="manual_link 0"))
        buttons = [btn_list, [InlineKeyboardButton(self._("Cancel"), callback_data=Flags.CANCEL_PROCESS)]]
        self.bot.edit_message_text(text=txt, chat_id=tg_chat_id, message_id=tg_msg_id, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)

    def execute(self, command: str, chat: ETMChatMixin, tg_chat_id: TelegramChatID, tg_msg_id: TelegramMessageID) -> int:
        if command == "unlink":
            chat.unlink()
            self.bot.edit_message_text(text=self._("Chat {} is restored.").format(chat.full_name), chat_id=tg_chat_id, message_id=tg_msg_id)
            return ConversationHandler.END

        assert command == "manual_link"
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
        ).format(chat_display_name=html.escape(chat.full_name), code=html.escape(utils.b64en(utils.message_id_to_str(tg_chat_id, tg_msg_id))))
        self.bot.edit_message_text(
            text=txt,
            chat_id=tg_chat_id,
            message_id=tg_msg_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(self._("Cancel"), callback_data=Flags.CANCEL_PROCESS)]]),
            parse_mode=ParseMode.HTML,
        )
        return Flags.LINK_EXEC
