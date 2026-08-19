# coding=utf-8

import logging
import shlex
from collections.abc import Callable
from typing import List, Optional, Tuple

from ehforwarderbot import coordinator
from ehforwarderbot.types import ChatID, ModuleID
from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import CallbackContext, ConversationHandler

from efb_telegram_master.chat.chat import ETMChatMixin
from efb_telegram_master.core import utils
from efb_telegram_master.core.ptb_compat import get_forwarded_chat, sync_reply_text
from efb_telegram_master.core.utils import EFBChannelChatIDStr, TelegramChatID, TelegramMessageID, TelegramTopicID, TgChatMsgIDStr, bounded_error_message
from efb_telegram_master.history.history_replay import history_location_text, history_location_url
from efb_telegram_master.link.callback_sessions import CallbackSessionStore


class LinkCompletionService:
    def __init__(
        self,
        bot,
        channel_id: ModuleID,
        multiple_slave_chats: Callable[[], bool],
        chat_associations,
        callback_sessions: CallbackSessionStore,
        topic_sync,
        history_replay,
        translate,
        ngettext,
        logger: logging.Logger,
        conversation_handler: ConversationHandler,
    ):
        self.bot = bot
        self.channel_id = channel_id
        self._multiple_slave_chats = multiple_slave_chats
        self.chat_associations = chat_associations
        self.callback_sessions = callback_sessions
        self.topic_sync = topic_sync
        self.history_replay = history_replay
        self._ = translate
        self.ngettext = ngettext
        self.logger = logger
        self._conversation_handler = conversation_handler

    def complete(self, update: Update, args: Optional[List[str]]):
        """Actual code of linking a chat by manipulating database.
        Triggered by ``/start BASE64(msg_id_to_str(chat_id, msg_id))``.
        """
        assert isinstance(update, Update)
        assert update.message
        assert update.effective_message
        assert update.effective_chat
        assert args

        resolved_args = list(args)
        message_text = update.effective_message.text
        if isinstance(message_text, str) and message_text:
            try:
                raw_args = shlex.split(message_text)[1:]
            except ValueError:
                raw_args = message_text.split()[1:]
            if len(raw_args) > len(resolved_args):
                resolved_args = raw_args

        try:
            msg_id = utils.message_id_str_to_id(TgChatMsgIDStr(utils.b64de(resolved_args[0])))
            storage_key = (TelegramChatID(int(msg_id[0])), TelegramMessageID(int(msg_id[1])))
            data = self.callback_sessions.get(self._conversation_handler, storage_key)
            if data is None or update.effective_user is None or not self.callback_sessions.is_owned_by(storage_key, update.effective_user.id):
                raise KeyError(storage_key)
        except KeyError:
            return sync_reply_text(self.bot, update.message, self._("Session expired or unknown parameter. (SE02)"))
        chat: ETMChatMixin = data.chats[0]
        previous_master_uids = tuple(chat.linked)
        is_relink = bool(previous_master_uids)
        chat_display_name = chat.full_name
        slave_channel, slave_chat_uid = chat.module_id, chat.uid
        chat_uid = utils.chat_id_to_str(slave_channel, slave_chat_uid)
        try:
            coordinator.get_module_by_id(slave_channel)
        except NameError:
            self.bot.edit_message_text(
                text=self._("{module_id} is not activated in current profile. It cannot be linked.").format(module_id=slave_channel), chat_id=storage_key[0], message_id=storage_key[1]
            )
            return

        # Use channel ID if command is forwarded from a channel.
        forwarded_chat = get_forwarded_chat(update.effective_message)
        if forwarded_chat and forwarded_chat.type == ChatType.CHANNEL:
            tg_chat_to_link = forwarded_chat
        else:
            tg_chat_to_link = update.effective_chat

        # Optional second argument can override backfill behaviour:
        #   true/on/1  -> always backfill
        #   false/off/0 -> never backfill
        backfill_override: Optional[bool] = None
        if len(resolved_args) >= 2:
            flag = resolved_args[1].strip().lower()
            if flag in ("true", "on", "yes", "1"):
                backfill_override = True
            elif flag in ("false", "off", "no", "0"):
                backfill_override = False

        txt = self._("Trying to link chat {0}...").format(chat_display_name)
        msg = self.bot.send_message(tg_chat_to_link.id, text=txt)

        chat.link(self.channel_id, ChatID(str(tg_chat_to_link.id)), self._multiple_slave_chats())
        self.chat_associations.remove_topic_assoc(
            slave_uid=chat_uid,
        )

        thread_id = None
        if tg_chat_to_link.is_forum:
            thread_id = self.topic_sync.create_topic(slave_uid=chat_uid, telegram_chat_id=TelegramChatID(tg_chat_to_link.id))
            if not thread_id:
                self.bot.send_message(
                    msg.chat.id,
                    self._("Failed to create topic for {name} in the group.\nPlease make sure the bot has the right.\nYou can send /init_topics to create again.").format(name=chat_display_name),
                    reply_to_message_id=msg.message_id,
                )
                thread_id = None
            else:
                try:
                    self.topic_sync.update_single_topic_info(TelegramChatID(tg_chat_to_link.id), thread_id, chat_uid)
                except Exception as error:
                    self.logger.warning(
                        "Auto update group info failed for %s.",
                        chat_uid,
                        extra={
                            "event": "chat_binding.link_topic_info_failed",
                            "error_type": type(error).__name__,
                            "error_message": bounded_error_message(error),
                        },
                    )

        txt = self._("Chat {0} is now linked.").format(chat_display_name)
        self.bot.edit_message_text(
            text=txt,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            _sender_bot_id=getattr(msg, "sender_bot_id", None),
        )

        self.bot.edit_message_text(chat_id=storage_key[0], message_id=storage_key[1], text=txt)
        self.callback_sessions.clear(self._conversation_handler, storage_key)

        target_chat_id = tg_chat_to_link.id
        target_thread_id = thread_id
        current_master_uids = self.chat_associations.get_chat_assoc(slave_uid=chat_uid)
        if current_master_uids:
            _, linked_chat_id, _ = utils.chat_id_str_to_id(current_master_uids[0])
            target_chat_id = int(linked_chat_id)
            if target_chat_id != tg_chat_to_link.id:
                target_thread_id = self.chat_associations.get_topic_thread_id(chat_uid, TelegramChatID(target_chat_id))

        # migrate history
        # auto:   backfill on first link, send history link on relink
        # True:   always backfill (even on relink)
        # False:  skip both (user opted out)
        do_backfill = backfill_override is True or (backfill_override is None and not is_relink)
        do_history_link = backfill_override is None and is_relink

        if do_backfill:
            try:
                self.history_replay.start(chat_uid, target_chat_id, target_thread_id, storage_key)
            except Exception as error:
                self.logger.warning(
                    "History migration failed for %s.",
                    chat_uid,
                    extra={
                        "event": "chat_binding.link_history_migration_failed",
                        "error_type": type(error).__name__,
                        "error_message": bounded_error_message(error),
                    },
                )
                try:
                    notice_kwargs = {"chat_id": target_chat_id, "text": self._("⚠️ History backfill failed, but the chat is linked."), "disable_notification": True}
                    if target_thread_id:
                        notice_kwargs["message_thread_id"] = target_thread_id
                    self.bot.send_message(**notice_kwargs)
                except Exception:
                    pass
        elif do_history_link:
            history_key = storage_key
            for previous_master_uid in previous_master_uids:
                _, previous_master_id, _ = utils.chat_id_str_to_id(previous_master_uid)
                candidate_key = (TelegramChatID(int(previous_master_id)), storage_key[1])
                if history_location_url(candidate_key) is not None:
                    history_key = candidate_key
                    break
            self.send_history_link(chat_uid, target_chat_id, history_key, target_thread_id)

    def send_history_link(self, slave_chat_id: EFBChannelChatIDStr, tg_chat_id: int, storage_key: Tuple[int, int], thread_id: Optional[TelegramTopicID] = None):
        """Send a message with a link to the chat history."""
        try:
            text = history_location_text(self._, storage_key)

            kwargs = {"chat_id": tg_chat_id, "text": text, "disable_notification": True}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            self.bot.send_message(**kwargs)

        except Exception as error:
            self.logger.warning(
                "Failed to send history link for %s.",
                slave_chat_id,
                extra={
                    "event": "chat_binding.link_history_link_failed",
                    "error_type": type(error).__name__,
                    "error_message": bounded_error_message(error),
                },
            )

    def unlink_all(self, update: Update, context: CallbackContext):
        """
        Unlink all chats linked to the telegram group.
        Triggered by `/unlink_all`.
        """
        assert isinstance(update, Update)
        assert update.message

        if update.message.chat.type != ChatType.PRIVATE:
            links = self.chat_associations.get_chat_assoc(master_uid=utils.chat_id_to_str(self.channel_id, ChatID(str(update.message.chat.id))))
            if len(links) < 1:
                return self.bot.send_message(update.message.chat.id, self._("No chat is linked to the group."), reply_to_message_id=update.message.message_id)
            else:
                self.chat_associations.remove_chat_assoc(master_uid=utils.chat_id_to_str(self.channel_id, ChatID(str(update.message.chat.id))))
                return self.bot.send_message(
                    update.message.chat.id,
                    self.ngettext("All {0} chat has been unlinked from this group.", "All {0} chats has been unlinked from this group.", len(links)).format(len(links)),
                    reply_to_message_id=update.message.message_id,
                )
        else:
            forwarded_chat = get_forwarded_chat(update.message)
            if forwarded_chat and forwarded_chat.type == ChatType.CHANNEL:
                links = self.chat_associations.get_chat_assoc(master_uid=utils.chat_id_to_str(self.channel_id, ChatID(str(forwarded_chat.id))))

                if len(links) < 1:
                    return self.bot.send_message(update.message.chat.id, self._("No chat is linked to the channel."), reply_to_message_id=update.message.message_id)
                else:
                    self.chat_associations.remove_chat_assoc(master_uid=utils.chat_id_to_str(self.channel_id, ChatID(str(forwarded_chat.id))))
                    return self.bot.send_message(
                        update.message.chat.id,
                        self.ngettext("All {0} chat has been unlinked from this channel.", "All {0} chats has been unlinked from this channel.", len(links)).format(len(links)),
                        reply_to_message_id=update.message.message_id,
                    )
            else:
                return self.bot.send_message(
                    update.message.chat.id, self._("Send `/unlink_all` to a group to unlink all remote chats from it."), parse_mode=ParseMode.MARKDOWN, reply_to_message_id=update.message.message_id
                )
