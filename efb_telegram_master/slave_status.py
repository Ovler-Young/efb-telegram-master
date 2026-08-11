"""Slave-channel status delivery and state updates."""

from __future__ import annotations

import itertools
import logging
import time
from typing import Protocol

import telegram.error
from ehforwarderbot import Message
from ehforwarderbot.constants import MsgType
from ehforwarderbot.message import StatusAttribute
from ehforwarderbot.status import ChatUpdates, MemberUpdates, MessageReactionsUpdate, MessageRemoval, Status
from telegram.constants import ChatAction

from . import utils
from .message import ETMMsg
from .utils import TelegramChatID, TelegramTopicID


class ReactionDispatcher(Protocol):
    def dispatch_message(self, msg, msg_template: str, old_msg_id, tg_dest, thread_id, silent: bool = False, dedupe_key=None, database_old_msg_id=None, target_msg_id_override=None) -> None: ...


def deliver_message_status(bot, message: Message, destination: TelegramChatID, thread_id: TelegramTopicID | None) -> None:
    assert isinstance(message.attributes, StatusAttribute)
    actions = {
        StatusAttribute.Types.TYPING: ChatAction.TYPING,
        StatusAttribute.Types.UPLOADING_VOICE: ChatAction.RECORD_VOICE,
        StatusAttribute.Types.UPLOADING_IMAGE: ChatAction.UPLOAD_PHOTO,
        StatusAttribute.Types.UPLOADING_VIDEO: ChatAction.UPLOAD_VIDEO,
        StatusAttribute.Types.UPLOADING_FILE: ChatAction.UPLOAD_DOCUMENT,
    }
    action = actions.get(message.attributes.status_type)
    if action:
        bot.send_chat_action(destination, action, message_thread_id=thread_id)


class SlaveStatusService:
    """Apply slave status changes without depending on the channel object."""

    REACTION_DB_WAIT_TIMEOUT = 2.0
    REACTION_DB_WAIT_INTERVAL = 0.05

    def __init__(self, logger: logging.Logger, slave_chat_info, chat_manager, msglogs, bot, flag, router, reaction_dispatcher: ReactionDispatcher, translate) -> None:
        self.logger = logger
        self.slave_chat_info = slave_chat_info
        self.chat_manager = chat_manager
        self.msglogs = msglogs
        self.bot = bot
        self.flag = flag
        self.router = router
        self.reaction_dispatcher = reaction_dispatcher
        self.translate = translate

    def send_status(self, status: Status) -> None:
        if isinstance(status, ChatUpdates):
            self.logger.debug("Received chat updates from channel %s", status.channel)
            for chat_id in status.removed_chats:
                self.slave_chat_info.delete_slave_chat_info(status.channel.channel_id, chat_id)
                self.chat_manager.delete_chat_object(status.channel.channel_id, chat_id)
            for chat_id in itertools.chain(status.new_chats, status.modified_chats):
                self.chat_manager.update_chat_obj(status.channel.get_chat(chat_id), full_update=True)
        elif isinstance(status, MemberUpdates):
            self.logger.debug("Received member updates from channel %s about group %s", status.channel, status.chat_id)
            for member_id in status.removed_members:
                self.slave_chat_info.delete_slave_chat_info(status.channel.channel_id, member_id, status.chat_id)
            self.chat_manager.delete_chat_members(status.channel.channel_id, status.chat_id, status.removed_members)
            self.chat_manager.update_chat_obj(status.channel.get_chat(status.chat_id), full_update=True)
        elif isinstance(status, MessageRemoval):
            self._remove_message(status)
        elif isinstance(status, MessageReactionsUpdate):
            self.update_reactions(status)
        else:
            self.logger.error("Received unsupported status type %s.", type(status).__name__)

    def _remove_message(self, status: MessageRemoval) -> None:
        self.logger.debug("Received message removal request from channel %s on message %s", status.source_channel, status.message)
        old_msg = self.msglogs.get_msg_log(slave_msg_id=status.message.uid, slave_origin_uid=utils.chat_id_to_str(chat=status.message.chat))
        if old_msg is None:
            self.logger.info("Message removal has no database record.")
            return
        if old_msg.provenance == "mtproto_ingested":
            self.logger.info("Ignoring removal for ingested synthetic message %s from %s.", status.message.uid, status.message.chat)
            return
        old_msg_id = utils.message_id_str_to_id(utils.TgChatMsgIDStr(old_msg.master_msg_id_alt or old_msg.master_msg_id))
        try:
            if not self.flag("prevent_message_removal"):
                self.bot.delete_message(*old_msg_id, _sender_bot_id=old_msg.sender_bot_id)
                return
        except telegram.error.TelegramError as error:
            self.logger.warning("Failed to delete message %s.%s (%s); sending notification instead.", *old_msg_id, type(error).__name__)
        self.bot.send_message(chat_id=old_msg_id[0], text=self.translate("Message is removed in remote chat."), reply_to_message_id=old_msg_id[1], disable_notification=True)

    @staticmethod
    def reaction_target_message_id(old_msg: ETMMsg, old_msg_db) -> utils.TgChatMsgIDStr:
        if old_msg_db.master_msg_id_alt and old_msg.deliver_to and old_msg.deliver_to.channel_id == old_msg.chat.module_id:
            return old_msg_db.master_msg_id_alt
        if old_msg.type in (MsgType.Text, MsgType.Link):
            return old_msg_db.master_msg_id or old_msg_db.master_msg_id_alt
        return old_msg_db.master_msg_id_alt or old_msg_db.master_msg_id

    @staticmethod
    def reaction_edit_target_missing(error: telegram.error.BadRequest) -> bool:
        message = (error.message or "").strip().casefold().removeprefix("bad request: ").strip()
        return message in {"message to edit not found", "message not found"}

    def update_reactions(self, status: MessageReactionsUpdate) -> None:
        origin_uid = utils.chat_id_to_str(chat=status.chat)
        row = self.msglogs.get_msg_log(slave_msg_id=status.msg_id, slave_origin_uid=origin_uid)
        deadline = time.monotonic() + self.REACTION_DB_WAIT_TIMEOUT
        while row is None and time.monotonic() < deadline:
            time.sleep(self.REACTION_DB_WAIT_INTERVAL)
            row = self.msglogs.get_msg_log(slave_msg_id=status.msg_id, slave_origin_uid=origin_uid)
        if row is None:
            self.logger.error("Trying to update reactions of message, but message is not found in database. Message ID %s from %s: %s.", status.msg_id, status.chat, status.reactions)
            return
        if getattr(row, "provenance", "live") == "mtproto_ingested":
            self.logger.info("Ignoring reaction update for ingested synthetic message %s from %s.", status.msg_id, status.chat)
            return
        message: ETMMsg = row.build_etm_msg(chat_manager=self.chat_manager)
        message.reactions, message.edit, message.edit_media = status.reactions, True, False
        if row.sender_bot_id:
            message.vendor_specific = message.vendor_specific or {}
            message.vendor_specific["_sender_bot_id"] = row.sender_bot_id
        plan = self.router.route(message)
        if plan.destination is None:
            self.logger.error("Cannot update reactions for message %s from %s: destination not found.", status.msg_id, status.chat)
            return
        chat_id, message_id = utils.message_id_str_to_id(self.reaction_target_message_id(message, row))
        telegram_origin = bool(message.deliver_to and message.deliver_to.channel_id == message.chat.module_id)
        if telegram_origin and not row.master_msg_id_alt:
            message.edit = False
            message.vendor_specific = message.vendor_specific or {}
            self.reaction_dispatcher.dispatch_message(message, plan.message_template, None, plan.destination, plan.thread_id, database_old_msg_id=(chat_id, message_id), target_msg_id_override=message_id)
            return
        try:
            self.reaction_dispatcher.dispatch_message(message, plan.message_template, (chat_id, message_id), plan.destination, plan.thread_id)
        except telegram.error.BadRequest as error:
            if not (telegram_origin and row.master_msg_id_alt and self.reaction_edit_target_missing(error)):
                raise
            primary_chat_id, primary_message_id = utils.message_id_str_to_id(utils.TgChatMsgIDStr(row.master_msg_id))
            message.edit = False
            message.vendor_specific = message.vendor_specific or {}
            self.reaction_dispatcher.dispatch_message(message, plan.message_template, None, primary_chat_id, plan.thread_id, database_old_msg_id=(chat_id, message_id), target_msg_id_override=primary_message_id)
