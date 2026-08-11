"""Telegram commands that mutate remote slave messages."""

from __future__ import annotations

import logging
from pickle import UnpicklingError
from typing import Callable

from ehforwarderbot import coordinator
from telegram import Message, Update
from telegram.error import TelegramError
from telegram.ext import CallbackContext

from . import utils
from .message import ETMMsg
from .msg_type import get_msg_type
from .ptb_compat import sync_reply_text
from .utils import TelegramChatID, TelegramMessageID


class MasterMessageMutations:
    """Handle deletion commands and unsupported Telegram updates."""

    def __init__(self, bot, msglogs, chat_manager, localize: Callable[[str], str], flags: Callable[[str], object], send_removal: Callable[[object, ETMMsg], None], logger: logging.Logger) -> None:
        self.bot = bot
        self.msglogs = msglogs
        self.chat_manager = chat_manager
        self.localize = localize
        self.flags = flags
        self.send_removal = send_removal
        self.logger = logger

    def delete_message(self, update: Update, context: CallbackContext) -> None:
        assert isinstance(update, Update) and update.message
        message: Message = update.message
        if message.reply_to_message is None:
            self.bot.reply_error(update, self.localize("Reply /rm to a message to remove it from its remote chat."))
            return
        reply = message.reply_to_message
        msg_log = self.msglogs.get_msg_log(master_msg_id=utils.message_id_to_str(chat_id=TelegramChatID(reply.chat_id), message_id=TelegramMessageID(reply.message_id)))
        if not msg_log or msg_log.slave_member_uid == self.msglogs.FAIL_FLAG:
            self.bot.reply_error(update, self.localize("This message is not found in ETM database. You cannot remove it from its remote chat."))
            return
        if msg_log.provenance == "mtproto_ingested":
            self.bot.reply_error(update, self.localize("This recovered message cannot be removed from its remote chat."))
            return
        try:
            etm_msg: ETMMsg = msg_log.build_etm_msg(self.chat_manager)
        except UnpicklingError:
            self.bot.reply_error(update, self.localize("This message is not found in ETM database. You cannot remove it from its remote chat."))
            return
        dest_channel = coordinator.slaves.get(etm_msg.chat.module_id)
        if dest_channel is None:
            self.bot.reply_error(update, self.localize("Module of this message ({module_id}) could not be found, or is not a slave channel.").format(module_id=etm_msg.chat.module_id))
            return
        try:
            self.send_removal(dest_channel, etm_msg)
        except Exception as error:
            self.logger.exception("Failed to remove Telegram message %s.%s from remote chat (%s).", reply.chat_id, reply.message_id, type(error).__name__)
            sync_reply_text(self.bot, reply, self.localize("Failed to remove this message from remote chat.\n\n{error!s}").format(error=error))
            return
        if not self.flags("prevent_message_removal"):
            try:
                self.bot.delete_message(reply.chat_id, reply.message_id, _sender_bot_id=msg_log.sender_bot_id)
                return
            except TelegramError:
                pass
        sync_reply_text(self.bot, reply, self.localize("Message is removed in remote chat."))

    def unsupported_message(self, update: Update, context: CallbackContext) -> None:
        assert isinstance(update, Update) and update.effective_message
        message_type = get_msg_type(update.effective_message)
        self.bot.reply_error(update, self.localize("{type_name} messages are not supported by EFB Telegram Master channel.").format(type_name=message_type.name))
