"""Forum-topic, group-profile, and membership synchronization."""

from __future__ import annotations

import io
import logging
import time
from contextlib import suppress
from typing import IO, Callable, Optional

from ehforwarderbot import coordinator
from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.chat import SystemChatMember
from ehforwarderbot.exceptions import EFBChatNotFound, EFBOperationNotSupported
from ehforwarderbot.types import ChatID
from PIL import Image
from telegram import Update
from telegram.constants import ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import CallbackContext, CommandHandler, MessageHandler

from . import utils
from .chat import ETMChatMixin, ETMGroupChat
from .ptb_compat import Filters, get_forwarded_chat, sync_reply_text
from .utils import EFBChannelChatIDStr, TelegramChatID, TelegramTopicID


class TopicGroupService:
    """Synchronize chat bindings with arbitrary forum groups that have topics."""

    MIN_PICTURE_SIZE = 256
    MAX_TITLE_LENGTH = 255
    MAX_DESCRIPTION_LENGTH = 255

    def __init__(
        self, runtime, bot, chat_associations, chat_manager, msglog_scan, channel_id, localize: Callable[[str], str], ngettext: Callable[[str, str, int], str], logger: logging.Logger
    ) -> None:
        self.runtime = runtime
        self.bot = bot
        self.chat_associations = chat_associations
        self.chat_manager = chat_manager
        self.msglog_scan = msglog_scan
        self.channel_id = channel_id
        self._ = localize
        self.ngettext = ngettext
        self.logger = logger

    def register_handlers(self) -> None:
        self.runtime.application.add_handler(CommandHandler("update_info", self.runtime.as_async_callback(self.update_group_info)))
        self.runtime.application.add_handler(CommandHandler("init_topics", self.runtime.as_async_callback(self.topic_migration)))
        self.runtime.application.add_handler(MessageHandler(Filters.status_update.migrate, self.runtime.as_async_callback(self.chat_migration)))
        self.runtime.application.add_handler(MessageHandler(Filters.status_update.new_chat_members, self.runtime.as_async_callback(self.chat_joined)))
        self.runtime.application.add_handler(MessageHandler(Filters.status_update.left_chat_member, self.runtime.as_async_callback(self.chat_left)))

    @staticmethod
    def truncate_ellipsis(text: str, length: int) -> str:
        return text if len(text) <= length else text[: length - 1] + "…"

    def create_topic(self, slave_uid: EFBChannelChatIDStr, telegram_chat_id: TelegramChatID) -> Optional[TelegramTopicID]:
        thread_id = self.chat_associations.get_topic_thread_id(slave_uid=slave_uid, topic_chat_id=telegram_chat_id)
        if thread_id:
            return thread_id
        with self.chat_associations.topic_provisioning_transaction():
            thread_id = self.chat_associations.get_topic_thread_id(slave_uid=slave_uid, topic_chat_id=telegram_chat_id)
            if thread_id:
                return thread_id
            channel_id, chat_id, _ = utils.chat_id_str_to_id(slave_uid)
            chat: ETMChatMixin = self.chat_manager.get_chat(channel_id, chat_id, build_dummy=True)
            try:
                topic = self.bot.create_forum_topic(chat_id=telegram_chat_id, name=chat.chat_title)
            except Exception as error:
                self.logger.info("Failed to create topic (%s).", type(error).__name__)
                return None
            thread_id = TelegramTopicID(topic.message_thread_id)
            self.chat_associations.add_topic_assoc(telegram_chat_id, thread_id, slave_uid)
        self.msglog_scan.schedule_for_association(int(telegram_chat_id))
        return thread_id

    def update_group_info(self, update: Update, context: CallbackContext):
        assert update.effective_message and update.effective_chat
        if update.effective_chat.type == ChatType.PRIVATE:
            return self.bot.reply_error(update, self._("Send /update_info to a group where this bot is a group admin to update group title, description and profile picture."))
        telegram_chat = get_forwarded_chat(update.effective_message) or update.effective_chat
        chats = self.chat_associations.get_chat_assoc(master_uid=utils.chat_id_to_str(self.channel_id, ChatID(str(telegram_chat.id))))
        if telegram_chat.is_forum and len(chats) > 1:
            return self._update_forum_reply(update, TelegramChatID(telegram_chat.id))
        if len(chats) != 1:
            return self.bot.reply_error(
                update,
                self.ngettext(
                    "This only works in a group linked with one chat. Currently {0} chat linked to this group.",
                    "This only works in a group linked with one chat. Currently {0} chats linked to this group.",
                    len(chats),
                ).format(len(chats)),
            )
        return self._update_single_group(update, telegram_chat, chats[0])

    def _update_forum_reply(self, update: Update, chat_id: TelegramChatID):
        assert update.effective_message
        update_message = update.effective_message
        thread_id = update_message.message_thread_id
        try:
            success, result_message, _ = self._update_forum_group_info(chat_id, TelegramTopicID(thread_id) if thread_id else None)
        except Exception as error:
            self.logger.exception("Error occurred while updating forum group info.")
            return self.bot.reply_error(update, self._("Error occurred while updating forum group info.\n{0}").format(str(error)))
        if success:
            return sync_reply_text(self.bot, update_message, result_message)
        return self.bot.reply_error(update, result_message)

    def _update_single_group(self, update: Update, telegram_chat, slave_uid: EFBChannelChatIDStr):
        picture: Optional[IO] = None
        resized: Optional[IO] = None
        channel_id, chat_uid, _ = utils.chat_id_str_to_id(slave_uid)
        if channel_id not in coordinator.slaves:
            return self.bot.reply_error(update, self._("Channel linked ({channel}) is not found.").format(channel=channel_id))
        channel = coordinator.slaves[channel_id]
        try:
            chat = self.chat_manager.update_chat_obj(channel.get_chat(chat_uid), full_update=True)
            self.bot.set_chat_title(telegram_chat.id, self.truncate_ellipsis(chat.chat_title, self.MAX_TITLE_LENGTH))
            description, picture, resized = self._get_chat_info_and_picture(chat, channel)
            if description:
                with suppress(BadRequest, TelegramError):
                    self.bot.set_chat_description(telegram_chat.id, self.truncate_ellipsis(description, self.MAX_DESCRIPTION_LENGTH))
            if not picture:
                raise EFBOperationNotSupported()
            self.bot.set_chat_photo(telegram_chat.id, resized or picture)
            assert update.effective_message
            return sync_reply_text(self.bot, update.effective_message, self._("Chat details updated."))
        except EFBChatNotFound:
            return self.bot.reply_error(
                update,
                self._("Chat linked ({chat_uid}) is not found in the slave channel ({channel_name}, {channel_id}).").format(
                    chat_uid=chat_uid, channel_name=channel.channel_name, channel_id=channel_id
                ),
            )
        except EFBOperationNotSupported:
            return self.bot.reply_error(update, self._("No profile picture provided from this chat."))
        except TelegramError as error:
            return self.bot.reply_error(update, self._("Error occurred while update chat details.\n{0}").format(error.message))
        finally:
            for value in (picture, resized):
                if value and getattr(value, "close", None):
                    value.close()

    def _get_chat_info_and_picture(self, chat: ETMChatMixin, channel: SlaveChannel) -> tuple[Optional[str], Optional[IO], Optional[IO]]:
        picture: Optional[IO] = None
        resized: Optional[IO] = None
        description = chat.description or ""
        try:
            if isinstance(chat, ETMGroupChat):
                members = self._(", ").join(member.long_name for member in chat.members if not isinstance(member, SystemChatMember))
                description += ("\n" if description else "") + self.ngettext("{count} group member: {list}", "{count} group members: {list}", len(chat.members)).format(
                    count=len(chat.members), list=members
                )
            picture = channel.get_chat_picture(chat)
            if not picture:
                raise EFBOperationNotSupported()
            image = Image.open(picture)
            if min(image.size) < self.MIN_PICTURE_SIZE:
                scale = self.MIN_PICTURE_SIZE / min(image.size)
                resized = io.BytesIO()
                image.resize((int(scale * image.size[0]), int(scale * image.size[1])), getattr(getattr(Image, "Resampling", Image), "BICUBIC")).save(resized, "PNG")
                resized.seek(0)
            picture.seek(0)
        except EFBOperationNotSupported:
            pass
        except Exception as error:
            self.logger.warning("Failed to get chat picture (%s).", type(error).__name__)
        return description, picture, resized

    def _update_forum_group_info(self, chat_id: TelegramChatID, current_thread_id: Optional[TelegramTopicID]) -> tuple[bool, str, int]:
        topics = self.chat_associations.get_topic_slaves(chat_id)
        if not topics:
            return False, self._("No topics found in this forum group."), 0
        selected = [(slave_uid, thread_id) for slave_uid, thread_id in topics if current_thread_id is None or thread_id == current_thread_id]
        if current_thread_id is not None and not selected:
            return False, self._("This topic is not managed by this bot."), 0
        updated = 0
        for slave_uid, thread_id in selected:
            updated += self.update_single_topic_info(chat_id, thread_id, slave_uid)
            time.sleep(30)
        if updated:
            return True, self.ngettext("Updated {count} topic.", "Updated {count} topics.", updated).format(count=updated), updated
        return False, self._("Failed to update any topics."), 0

    def update_single_topic_info(self, chat_id: TelegramChatID, thread_id: TelegramTopicID, slave_uid: str) -> bool:
        picture: Optional[IO] = None
        resized: Optional[IO] = None
        try:
            channel_id, chat_uid, _ = utils.chat_id_str_to_id(EFBChannelChatIDStr(slave_uid))
            if channel_id not in coordinator.slaves:
                return False
            channel = coordinator.slaves[channel_id]
            chat = self.chat_manager.update_chat_obj(channel.get_chat(chat_uid), full_update=True)
            with suppress(TelegramError):
                self.bot.edit_forum_topic(chat_id=chat_id, message_thread_id=thread_id, name=self.truncate_ellipsis(chat.chat_title, self.MAX_TITLE_LENGTH), icon_custom_emoji_id="")
            description, picture, resized = self._get_chat_info_and_picture(chat, channel)
            if picture:
                message = self.bot.send_photo(chat_id=chat_id, photo=resized or picture, caption=description or chat.chat_title, message_thread_id=thread_id, disable_notification=True)
            elif description:
                message = self.bot.send_message(chat_id=chat_id, text=description, message_thread_id=thread_id, disable_notification=True)
            else:
                return True
            self.bot.pin_chat_message(chat_id=chat_id, message_id=message.message_id, disable_notification=True)
            return True
        except Exception as error:
            self.logger.warning("Failed to update topic %s for chat %s (%s).", thread_id, slave_uid, type(error).__name__)
            return False
        finally:
            for value in (picture, resized):
                if value and getattr(value, "close", None):
                    value.close()

    def topic_migration(self, update: Update, context: CallbackContext):
        assert update.effective_message
        self._create_topics_for_chat(TelegramChatID(update.effective_message.chat.id))

    def _create_topics_for_chat(self, chat_id: TelegramChatID) -> None:
        master_uid = utils.chat_id_to_str(self.channel_id, ChatID(str(chat_id)))
        for slave_uid in self.chat_associations.get_chat_assoc(master_uid=master_uid):
            self.create_topic(slave_uid, chat_id)

    def chat_migration(self, update: Update, context: CallbackContext):
        assert update.effective_message
        message = update.effective_message
        if message.migrate_from_chat_id is not None:
            return self.migrate_chat_associations(message.migrate_from_chat_id, message.chat.id)
        if message.migrate_to_chat_id is not None:
            self.migrate_chat_associations(message.chat.id, message.migrate_to_chat_id)

    def migrate_chat_associations(self, from_id: int, to_id: int) -> None:
        from_uid = utils.chat_id_to_str(self.channel_id, ChatID(str(from_id)))
        to_uid = utils.chat_id_to_str(self.channel_id, ChatID(str(to_id)))
        slave_uids = self.chat_associations.get_chat_assoc(master_uid=from_uid)
        for slave_uid in slave_uids:
            self.chat_associations.add_chat_assoc(to_uid, slave_uid, multiple_slave=True)
        self.chat_associations.remove_chat_assoc(master_uid=from_uid)
        if self.bot.get_chat_info(to_id).is_forum:
            for slave_uid in slave_uids:
                self.create_topic(slave_uid, TelegramChatID(to_id))

    def chat_joined(self, update: Update, context: CallbackContext):
        assert update.effective_message
        message = update.effective_message
        if message.new_chat_members and self.bot.bot_pool:
            self.bot.bot_pool.on_bots_joined_chat([member.id for member in message.new_chat_members], message.chat.id)

    def chat_left(self, update: Update, context: CallbackContext):
        assert update.effective_message
        message = update.effective_message
        if message.left_chat_member is None:
            return
        member_id = message.left_chat_member.id
        if member_id == self.bot.get_me().id:
            self.chat_associations.remove_chat_assoc(master_uid=utils.chat_id_to_str(self.channel_id, ChatID(str(message.chat.id))))
        elif self.bot.bot_pool:
            self.bot.bot_pool.on_bot_left_chat(member_id, message.chat.id)
