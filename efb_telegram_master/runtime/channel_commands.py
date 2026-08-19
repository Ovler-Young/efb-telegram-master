"""Telegram command, information, and configuration handling."""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from typing import List, Optional

import ehforwarderbot
import telegram
from ehforwarderbot import Channel, coordinator
from ehforwarderbot.exceptions import EFBChatNotFound, EFBMessageReactionNotPossible, EFBOperationNotSupported
from ehforwarderbot.status import ReactToMessage
from ehforwarderbot.types import ChatID, InstanceID, ModuleID, ReactionName
from telegram import Message, Update
from telegram.constants import ChatType
from telegram.ext import CallbackContext

from .. import utils as etm_utils
from ..chat_object_cache import ChatObjectCacheManager
from ..link_completion import LinkCompletionService
from ..msglog_scan import MsgLogScanScheduler
from ..persistence.chat_association_repository import ChatAssociationRepository
from ..persistence.msglog_repository import MsgLogRepository
from ..ptb_compat import SupportsSendMessage, get_forwarded_chat, sync_reply_html, sync_reply_text
from ..utils import EFBChannelChatIDStr, TelegramChatID, TelegramMessageID
from .channel_locale import LocaleState


class TelegramCommandService:
    """Own Telegram-facing commands and the channel's parsed configuration."""

    def __init__(
        self,
        channel_id: ModuleID,
        instance_id: Optional[InstanceID],
        version: str,
        api: SupportsSendMessage,
        chat_associations: ChatAssociationRepository,
        chat_manager: ChatObjectCacheManager,
        msglogs: MsgLogRepository,
        message_reconstructor,
        msglog_scan: MsgLogScanScheduler,
        link_completion: LinkCompletionService,
        admins: list[int],
        topic_group: TelegramChatID | None,
        logger: logging.Logger,
        locale_state: "LocaleState",
    ) -> None:
        self.channel_id, self.instance_id, self.version = channel_id, instance_id, version
        self.api = api
        self.chat_associations, self.chat_manager = chat_associations, chat_manager
        self.msglogs, self.message_reconstructor, self.msglog_scan, self.link_completion = msglogs, message_reconstructor, msglog_scan, link_completion
        self.admins, self.topic_group, self.logger = admins, topic_group, logger
        self.locale_state = locale_state

    @property
    def _(self) -> Callable[[str], str]:
        return self.locale_state.gettext

    @property
    def ngettext(self) -> Callable[[str, str, int], str]:
        return self.locale_state.ngettext

    def start(self, update: Update, context: CallbackContext) -> None:
        assert isinstance(update.effective_message, telegram.Message)
        assert isinstance(update.effective_chat, telegram.Chat)
        command_args = self.resolve_command_args(update.effective_message.text, context.args)
        if command_args:
            forwarded_chat = get_forwarded_chat(update.effective_message)
            allowed = (update.effective_message.chat.type != ChatType.PRIVATE and update.effective_chat.id != self.topic_group) or (
                forwarded_chat and forwarded_chat.type == ChatType.CHANNEL and forwarded_chat.id != self.topic_group
            )
            if allowed:
                self.link_completion.complete(update, command_args)
            else:
                self.api.send_message(update.effective_chat.id, self._("You cannot link remote chats to here. Please try again."))
            return
        self.api.send_message(update.effective_chat.id, self._("This is EFB Telegram Master Channel.\n\nTo learn more, please visit https://etm.1a23.studio ."))

    @staticmethod
    def resolve_command_args(message_text: Optional[str], parsed_args: Optional[List[str]]) -> List[str]:
        args = list(parsed_args or [])
        if not message_text:
            return args
        try:
            raw_args = shlex.split(message_text)[1:]
        except ValueError:
            raw_args = message_text.split()[1:]
        return raw_args if len(raw_args) > len(args) else args

    def sync_msglog(self, update: Update, _context: CallbackContext) -> None:
        if update.effective_message is None:
            return
        if update.effective_user is None or update.effective_user.id not in self.admins:
            sync_reply_text(self.api, update.effective_message, self._("This command is for ETM admins only."))
            return
        if update.effective_chat is None or not update.effective_chat.is_forum:
            sync_reply_text(self.api, update.effective_message, self._("This command must be used in a bound forum group."))
            return
        group_id = TelegramChatID(update.effective_chat.id)
        if not self.chat_associations.get_topic_slaves(group_id):
            sync_reply_text(self.api, update.effective_message, self._("This forum group has no bound topics."))
            return
        state = self.msglog_scan.schedule(int(group_id))
        sync_reply_text(self.api, update.effective_message, self._("MsgLog sync {state} for this group.").format(state=state))

    def help(self, update: Update, _context: CallbackContext) -> None:
        assert isinstance(update.message, Message)
        sync_reply_text(
            self.api,
            update.message,
            self._(
                "EFB Telegram Master Channel\n/start <token> [true|false]\n/link\n    Link a remote chat to an empty Telegram group.\n    Followed by a regular expression to filter results.\n/chat\n    Generate a chat head to start a conversation.\n    Followed by a regular expression to filter results.\n/extra\n    List all additional features from slave channels.\n/unlink_all\n    Unlink all remote chats in this chat.\n/info\n    Show information of the current Telegram chat.\n/react [emoji]\n    React to a message with an emoji, or show a list of members reacted.\n/update_info\n    Update info of linked Telegram group.\n    Only works in singly linked group where the bot is an admin.\n/init_topics\n/sync_msglog\n/rm\n    Remove the quoted message from its remote chat.\n/help\n    Print this command list."
            ),
        )

    def info(self, update: Update, _context: CallbackContext) -> None:
        assert isinstance(update.effective_message, Message)
        if update.effective_message.chat.type != ChatType.PRIVATE:
            message = self.info_topic(update) if update.effective_chat and update.effective_chat.is_forum else self.info_group(update)
        elif (forwarded_chat := get_forwarded_chat(update.effective_message)) and forwarded_chat.type == ChatType.CHANNEL:
            message = self.info_channel(update)
        else:
            message = self.info_general()
        for offset in range(0, len(message), 4095):
            sync_reply_text(self.api, update.effective_message, message[offset : offset + 4095])

    def info_topic(self, update: Update) -> str:
        assert isinstance(update.effective_message, Message)
        topic_links = self.chat_associations.get_topic_slaves(topic_chat_id=TelegramChatID(update.effective_message.chat_id))
        thread_id = update.effective_message.message_thread_id
        chat_ids: List[EFBChannelChatIDStr] = []
        if thread_id:
            chat_ids = [destination for destination, topic_id in topic_links if topic_id == thread_id]
            if not chat_ids:
                return "This chat is not managed by this bot"
        elif topic_links:
            chat_ids = [destination for destination, _topic_id in topic_links]
        message = self._("The topic {topic_name} ({topic_id}) is linked to:").format(topic_name=update.effective_message.chat.title, topic_id=update.effective_message.chat_id)
        return message + self.build_link_chats_info_str(chat_ids)

    def info_general(self) -> str:
        profile = coordinator.profile
        if self.instance_id:
            message = (
                self._("This is EFB Telegram Master Channel {version}, running on profile “{profile}”, instance “{instance}”, on EFB {fw_version}.")
                if profile != "default"
                else self._("This is EFB Telegram Master Channel {version}, running on default profile, instance “{instance}”, on EFB {fw_version}.")
            )
        else:
            message = (
                self._("This is EFB Telegram Master Channel {version}, running on profile “{profile}”, default instance, on EFB {fw_version}.")
                if profile != "default"
                else self._("This is EFB Telegram Master Channel {version}, running on default profile and instance, on EFB {fw_version}.")
            )
        message = message.format(version=self.version, fw_version=ehforwarderbot.__version__, profile=profile, instance=self.instance_id)
        message += "\n" + self.ngettext("{count} slave channel activated:", "{count} slave channels activated:", len(coordinator.slaves)).format(count=len(coordinator.slaves))
        for slave in coordinator.slaves.values():
            message += "\n- %s %s (%s, %s)" % (slave.channel_emoji, slave.channel_name, slave.channel_id, slave.__version__)
        if coordinator.middlewares:
            message += self.ngettext("\n\n{count} middleware activated:", "\n\n{count} middlewares activated:", len(coordinator.middlewares)).format(count=len(coordinator.middlewares))
            for middleware in coordinator.middlewares:
                message += "\n- %s (%s, %s)" % (middleware.middleware_name, middleware.middleware_id, middleware.__version__)
        return message

    def info_channel(self, update: Update) -> str:
        assert update.effective_message is not None
        chat = get_forwarded_chat(update.effective_message)
        assert chat is not None
        links = self.chat_associations.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, ChatID(str(chat.id))))
        if links:
            return self._("The channel {group_name} ({group_id}) is linked to:").format(group_name=chat.title, group_id=chat.id) + self.build_link_chats_info_str(links)
        return self._("The channel {group_name} ({group_id}) is not linked to any remote chat. To link one, use /link.").format(group_name=chat.title, group_id=chat.id)

    def info_group(self, update: Update) -> str:
        assert update.message is not None
        links = self.chat_associations.get_chat_assoc(master_uid=etm_utils.chat_id_to_str(self.channel_id, ChatID(str(update.message.chat_id))))
        if links:
            return self._("The group {group_name} ({group_id}) is linked to:").format(group_name=update.message.chat.title, group_id=update.message.chat_id) + self.build_link_chats_info_str(links)
        return self._("The group {group_name} ({group_id}) is not linked to any remote chat. To link one, use /link.").format(group_name=update.message.chat.title, group_id=update.message.chat_id)

    def build_link_chats_info_str(self, links: List[EFBChannelChatIDStr]) -> str:
        message = ""
        for link in links:
            channel_id, chat_id, _ = etm_utils.chat_id_str_to_id(link)
            chat_object = self.chat_manager.get_chat(channel_id, chat_id)
            if chat_object:
                message += "\n- %s (%s:%s)" % (chat_object.full_name, channel_id, chat_id)
                continue
            try:
                module = coordinator.get_module_by_id(channel_id)
                channel_name = f"{module.channel_emoji} {module.channel_name}" if isinstance(module, Channel) else module.middleware_name
                message += self._("\n- {channel_name}: Unknown chat ({channel_id}:{chat_id})").format(channel_name=channel_name, channel_id=channel_id, chat_id=chat_id)
            except NameError:
                message += self._("\n- Unknown channel {channel_id}: ({chat_id})").format(channel_id=channel_id, chat_id=chat_id)
        return message

    def react(self, update: Update, _context: CallbackContext) -> None:
        assert isinstance(update.effective_message, Message)
        message = update.effective_message
        args = message.text and message.text.split(" ", 1)
        reaction = ReactionName(args[1]) if args and len(args) > 1 else None
        if not message.reply_to_message:
            sync_reply_html(
                self.api,
                message,
                self._("Reply to a message with this command and an emoji to send a reaction. Ex.: <code>/react 👍</code>.\nSend <code>/react -</code> to remove your reaction from a message."),
            )
            return
        target = message.reply_to_message
        msg_log = self.msglogs.get_msg_log(master_msg_id=etm_utils.message_id_to_str(chat_id=TelegramChatID(target.chat_id), message_id=TelegramMessageID(target.message_id)))
        if msg_log is None:
            sync_reply_text(self.api, message, self._("The message you replied to is not recorded in ETM database. You cannot react to this message."))
            return
        if msg_log.provenance == "mtproto_ingested":
            sync_reply_text(self.api, message, self._("This recovered message cannot be reacted to from its remote chat."))
            return
        if reaction is None:
            reactors = self.message_reconstructor.build(msg_log).reactions
            if not reactors:
                sync_reply_html(self.api, message, self._("This message has no reactions yet. Reply to a message with this command and an emoji to send a reaction. Ex.: <code>/react 👍</code>."))
                return
            text = "\n".join(f"{key}:\n" + "\n".join(f"    {user.display_name}" for user in users) for key, users in reactors.items() if users)
            sync_reply_text(self.api, message, text)
            return
        channel_id, chat_uid, _ = etm_utils.chat_id_str_to_id(EFBChannelChatIDStr(msg_log.slave_origin_uid))
        if channel_id not in coordinator.slaves:
            sync_reply_text(self.api, message, self._("The slave channel involved in this message ({}) is not available. You cannot react to this message.").format(channel_id))
            return
        channel = coordinator.slaves[channel_id]
        if channel.suggested_reactions is None:
            sync_reply_text(self.api, message, self._("The channel involved in this message ({}) does not accept reactions. You cannot react to this message.").format(channel_id))
            return
        try:
            chat = channel.get_chat(chat_uid)
        except EFBChatNotFound:
            sync_reply_text(self.api, message, self._("The chat involved in this message ({}) is not found. You cannot react to this message.").format(chat_uid))
            return
        try:
            coordinator.send_status(ReactToMessage(chat=chat, msg_id=msg_log.slave_message_id, reaction=None if reaction == ReactionName("-") else reaction))
        except EFBOperationNotSupported:
            sync_reply_text(self.api, message, self._("You cannot react anything to this message."))
        except EFBMessageReactionNotPossible:
            prompt = self._("{} is not accepted as a reaction to this message.").format(reaction)
            if channel.suggested_reactions:
                prompt += "\n" + self._("You may want to try: {}").format(", ".join(channel.suggested_reactions[:10]))
            sync_reply_text(self.api, message, prompt)
