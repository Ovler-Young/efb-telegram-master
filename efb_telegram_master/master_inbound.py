"""Telegram-to-EFB message routing and conversion."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import humanize
from ehforwarderbot import coordinator
from ehforwarderbot.constants import MsgType
from ehforwarderbot.exceptions import EFBChatNotFound, EFBException, EFBMessageError, EFBMessageTypeNotSupported, EFBOperationNotSupported
from ehforwarderbot.message import LocationAttribute
from ehforwarderbot.types import ChatID, MessageID, ModuleID
from telegram import Contact, Message, Update
from telegram.constants import FileSizeLimit
from telegram.error import TelegramError
from telegram.ext import CallbackContext
from telegram.helpers import escape_markdown

from . import utils
from .chat_destination_cache import ChatDestinationCache
from .message import ETMMsg
from .msg_type import TGMsgType, get_msg_type
from .ptb_compat import sync_reply_text
from .utils import EFBChannelChatIDStr, TelegramChatID, TelegramMessageID


class MasterMessageInbound:
    """Route Telegram updates and convert routed messages into EFB messages."""

    DELETE_FLAG = "rm`"
    TYPE_DICT = {
        TGMsgType.Text: MsgType.Text,
        TGMsgType.Audio: MsgType.File,
        TGMsgType.Document: MsgType.File,
        TGMsgType.Photo: MsgType.Image,
        TGMsgType.Sticker: MsgType.Sticker,
        TGMsgType.VideoSticker: MsgType.Animation,
        TGMsgType.Video: MsgType.Video,
        TGMsgType.VideoNote: MsgType.Video,
        TGMsgType.Voice: MsgType.Voice,
        TGMsgType.Location: MsgType.Location,
        TGMsgType.Venue: MsgType.Location,
        TGMsgType.Animation: MsgType.Animation,
        TGMsgType.Contact: MsgType.Text,
        TGMsgType.Dice: MsgType.Text,
    }

    def __init__(
        self,
        bot,
        msglogs,
        chat_associations,
        chat_dest_cache: ChatDestinationCache,
        chat_manager,
        chat_binding,
        channel_id: ModuleID,
        localize: Callable[[str], str],
        flags: Callable[[str], object],
        send_removal: Callable[[object, ETMMsg], None],
        logger: logging.Logger,
    ) -> None:
        self.bot = bot
        self.msglogs = msglogs
        self.chat_associations = chat_associations
        self.chat_dest_cache = chat_dest_cache
        self.chat_manager = chat_manager
        self.chat_binding = chat_binding
        self.channel_id = channel_id
        self.localize = localize
        self.flags = flags
        self.send_removal = send_removal
        self.logger = logger
        self.type_dict = dict(self.TYPE_DICT)
        if self.flags("animated_stickers"):
            self.type_dict[TGMsgType.AnimatedSticker] = MsgType.Animation

    def msg(self, update: Update, context: CallbackContext) -> None:
        assert isinstance(update, Update)
        assert update.effective_message and update.effective_chat
        message = update.effective_message
        mid = utils.message_id_to_str(update=update)
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
            self.logger.debug("[%s] Message is edited: %s", mid, message.edit_date)
            msg_log = self.msglogs.get_msg_log(master_msg_id=utils.message_id_to_str(update=update))
            if msg_log and msg_log.provenance == "mtproto_ingested":
                self.logger.info("Ignoring edit for ingested synthetic message %s.", mid)
                return
            if not msg_log or msg_log.slave_message_id == self.msglogs.FAIL_FLAG:
                sync_reply_text(self.bot, message, self.localize("Error: This message cannot be edited, and thus is not sent. (ME01)"), quote=True)
                return
            destination = EFBChannelChatIDStr(msg_log.slave_origin_uid)
            edited = msg_log
            quote = msg_log.build_etm_msg(self.chat_manager).target is not None
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
                for dest, topic_id in topic_destinations:
                    if topic_id == thread_id:
                        destination = dest
                        reply_to = message.reply_to_message
                        quote = reply_to is not None and reply_to.message_id != reply_to.message_thread_id
                        break
                if destination is None:
                    self.logger.debug("[%s] Ignored message as it's a topic which wasn't created by this bot", mid)
                    return
            else:
                destinations = get_linked_slave_chats()
                if topic_destinations is not None and len(destinations) == len(topic_destinations):
                    return
        if destination is None:
            quote = False
            reply_to = message.reply_to_message
            cached_dest = self.chat_dest_cache.get(str(message.chat.id))
            if reply_to:
                dest_msg = self.msglogs.get_msg_log(master_msg_id=utils.message_id_to_str(TelegramChatID(reply_to.chat.id), TelegramMessageID(reply_to.message_id)))
                if dest_msg:
                    destination = EFBChannelChatIDStr(dest_msg.slave_origin_uid)
                    self.chat_dest_cache.set(str(message.chat.id), destination)
                    self.logger.debug("[%s] Quoted message is found in database with destination: %s", mid, destination)
            elif cached_dest:
                self.logger.debug("[%s] Cached destination found: %s", mid, cached_dest)
                destination = EFBChannelChatIDStr(cached_dest)
                self._send_cached_chat_warning(update, TelegramChatID(message.chat.id), cached_dest)
        self.logger.debug("[%s] Destination chat = %s", mid, destination)
        if destination is not None:
            self.process_telegram_message(update, context, destination, quote=quote, edited=edited)
            return
        self.logger.debug("[%s] Destination is not found for this message", mid)
        candidates = self.msglogs.get_recent_slave_chats(TelegramChatID(message.chat.id), limit=5) or get_linked_slave_chats()[:5]
        if candidates:
            tg_err_msg = sync_reply_text(self.bot, message, self.localize("Error: No recipient specified.\nPlease reply to a previous message. (MS01)"), quote=True)
            self.chat_binding.register_suggestions(update, candidates, TelegramChatID(update.effective_chat.id), TelegramMessageID(tg_err_msg.message_id))
        else:
            sync_reply_text(self.bot, message, self.localize("Error: No recipient specified.\nPlease reply to a previous message. (MS02)"), quote=True)

    def process_telegram_message(self, update: Update, context: CallbackContext, destination: EFBChannelChatIDStr, quote: bool = False, edited=None) -> None:
        assert isinstance(update, Update) and update.effective_message
        message_id = utils.message_id_to_str(update=update)
        message = update.effective_message
        channel, uid, _ = utils.chat_id_str_to_id(destination)
        if channel not in coordinator.slaves:
            self.bot.reply_error(update, self.localize("Internal error: Slave channel “{0}” not found.").format(channel))
            return
        m = ETMMsg()
        log_message = True
        try:
            m.uid = MessageID(message_id)
            m.type_telegram = mtype = get_msg_type(message)
            if mtype not in self.type_dict:
                log_message = False
                raise EFBMessageTypeNotSupported(self.localize("{type_name} messages are not supported by EFB Telegram Master channel.").format(type_name=mtype.name))
            m.type = self.type_dict[mtype]
            m.put_telegram_file(message)
            m.chat = self.chat_manager.get_chat(channel, uid, build_dummy=True)
            m.author = m.chat.self or m.chat.add_self()
            m.deliver_to = coordinator.slaves[channel]
            if quote:
                self.attach_target_message(message, m, channel)
            if m.type not in coordinator.slaves[channel].supported_message_types:
                raise EFBMessageTypeNotSupported(
                    self.localize("{type_name} messages are not supported by slave channel {channel_name}.").format(type_name=m.type.name, channel_name=coordinator.slaves[channel].channel_name)
                )
            if edited:
                self._apply_edit(m, message, edited)
                if (self._markdown(message.text, message.text_markdown_v2) or self._markdown(message.caption, message.caption_markdown_v2)).startswith(self.DELETE_FLAG):
                    self.send_removal(coordinator.slaves[channel], m)
                    self._remove_edited_message(message)
                    log_message = False
                    return
            self._populate_message(m, message, mtype)
            slave_msg = coordinator.send_message(m)
            m.uid = slave_msg.uid if slave_msg and slave_msg.uid else None
        except EFBChatNotFound as error:
            self.bot.reply_error(update, error.args[0] or self.localize("Chat is not found."))
        except EFBMessageTypeNotSupported as error:
            self.bot.reply_error(update, error.args[0] or self.localize("Message type is not supported."))
        except EFBOperationNotSupported as error:
            self.bot.reply_error(update, self.localize("Message editing is not supported.\n\n{exception!s}").format(exception=error))
        except EFBException as error:
            self.bot.reply_error(update, self.localize("Message is not sent.\n\n{exception!s}").format(exception=error))
            self.logger.exception("Message %s is not sent (%s).", message_id, type(error).__name__)
        except Exception as error:
            self.bot.reply_error(update, self.localize("Message is not sent.\n\n{exception!r}").format(exception=error))
            self.logger.exception("Message %s is not sent (%s).", message_id, type(error).__name__)
        finally:
            if log_message:
                self.msglogs.add_or_update_message_log(m, message)
                if m.file:
                    m.file.close()

    def _populate_message(self, m: ETMMsg, message: Message, mtype: TGMsgType) -> None:
        text = self._markdown(message.text, message.text_markdown_v2)
        caption = self._markdown(message.caption, message.caption_markdown_v2)
        if mtype is TGMsgType.Text:
            m.text = text
        elif mtype is TGMsgType.Photo:
            assert message.photo
            m.text, m.mime = caption, "image/jpeg"
            self._check_file_download(message.photo[-1])
        elif mtype in (TGMsgType.Sticker, TGMsgType.AnimatedSticker):
            assert message.sticker
            m.text = ""
            self._check_file_download(message.sticker)
        elif mtype is TGMsgType.Animation:
            assert message.animation
            m.text, m.mime = caption, message.animation.mime_type or m.mime
            m.filename = self._gif_filename(message.animation)
            self._check_file_download(message.animation)
        elif mtype is TGMsgType.VideoSticker:
            assert message.sticker and message.sticker.is_video
            m.text, m.filename, m.mime = caption, self._gif_filename(message.sticker, "sticker"), "image/gif"
            self._check_file_download(message.sticker)
        elif mtype is TGMsgType.Document:
            assert message.document
            m.text, m.filename, m.mime = caption, message.document.file_name, message.document.mime_type
            self._check_file_download(message.document)
        elif mtype is TGMsgType.Video:
            assert message.video
            m.text, m.mime = caption, message.video.mime_type
            self._check_file_download(message.video)
        elif mtype is TGMsgType.VideoNote:
            assert message.video_note
            m.text = caption
            self._check_file_download(message.video_note)
        elif mtype is TGMsgType.Audio:
            assert message.audio
            m.text = f"{message.audio.title} - {message.audio.performer}\n{caption}"
            m.mime = message.audio.mime_type
            self._check_file_download(message.audio)
        elif mtype is TGMsgType.Voice:
            assert message.voice
            m.text, m.mime = caption, message.voice.mime_type
            self._check_file_download(message.voice)
        elif mtype is TGMsgType.Location:
            assert message.location
            m.text, m.attributes = self.localize("Location"), LocationAttribute(message.location.latitude, message.location.longitude)
        elif mtype is TGMsgType.Venue:
            assert message.venue
            m.text = f"📍 {message.venue.title}\n{message.venue.address}"
            m.attributes = LocationAttribute(message.venue.location.latitude, message.venue.location.longitude)
        elif mtype is TGMsgType.Contact:
            assert message.contact
            contact: Contact = message.contact
            m.text = self.localize("Shared a contact: {first_name} {last_name}\n{phone_number}").format(first_name=contact.first_name, last_name=contact.last_name, phone_number=contact.phone_number)
        elif mtype is TGMsgType.Dice:
            assert message.dice
            m.text = f"{message.dice.emoji} = {message.dice.value}"
        else:
            raise EFBMessageTypeNotSupported(self.localize("Message type {0} is not supported.").format(mtype.name))

    def _apply_edit(self, m: ETMMsg, message: Message, edited) -> None:
        m.edit = True
        m.uid = MessageID(edited.slave_message_id)
        if m.file_unique_id and m.file_unique_id != edited.file_unique_id:
            m.edit_media = True

    def _remove_edited_message(self, message: Message) -> None:
        if not self.flags("prevent_message_removal"):
            try:
                self.bot.delete_message(message.chat_id, message.message_id)
                return
            except TelegramError:
                pass
        sync_reply_text(self.bot, message, self.localize("Message is removed in remote chat."))

    def attach_target_message(self, tg_msg: Message, etm_msg: ETMMsg, channel: ModuleID) -> ETMMsg:
        reply_to = tg_msg.reply_to_message
        assert reply_to
        target_log = self.msglogs.get_msg_log(master_msg_id=utils.message_id_to_str(TelegramChatID(reply_to.chat.id), TelegramMessageID(reply_to.message_id)))
        if not target_log or not target_log.slave_origin_uid or target_log.provenance == "mtproto_ingested":
            return etm_msg
        target_channel, _, _ = utils.chat_id_str_to_id(EFBChannelChatIDStr(target_log.slave_origin_uid))
        if target_channel != channel:
            return etm_msg
        target_msg: ETMMsg = target_log.build_etm_msg(self.chat_manager, recur=False)
        target_msg.target = None
        etm_msg.target = target_msg
        return etm_msg

    def _send_cached_chat_warning(self, update: Update, cache_key: TelegramChatID, cached_dest: EFBChannelChatIDStr) -> None:
        assert update.effective_message
        if self.flags("send_to_last_chat") != "warn" or self.chat_dest_cache.is_warned(str(cache_key)):
            return
        self.chat_dest_cache.set_warned(str(cache_key))
        dest_module, dest_chat_id, _ = utils.chat_id_str_to_id(cached_dest)
        dest_chat = self.chat_manager.get_chat(dest_module, dest_chat_id)
        sync_reply_text(self.bot, update.effective_message, self.localize("This message is sent to “{dest}” with quick reply feature.\n\nLearn more about how this works, how to turn this feature off, and how to stop this warning at {docs}.").format(dest=dest_chat.full_name if dest_chat else cached_dest, docs="https://etm.1a23.studio/"), quote=True, disable_web_page_preview=True)

    def _check_file_download(self, file_obj: Any) -> None:
        size = getattr(file_obj, "file_size", None)
        if size and not self.flags("local_tdlib_api") and size > FileSizeLimit.FILESIZE_DOWNLOAD:
            raise EFBMessageError(self.localize("Attachment is too large ({size}). Maximum allowed by Telegram Bot API is {max_size}. (AT01)").format(size=humanize.naturalsize(size), max_size=humanize.naturalsize(FileSizeLimit.FILESIZE_DOWNLOAD)))

    @staticmethod
    def _markdown(plain: Optional[str], markdown: Optional[str]) -> str:
        return plain if plain and markdown == escape_markdown(plain, version=2) else markdown or ""

    @staticmethod
    def _gif_filename(file_obj: Any, fallback: str = "") -> Optional[str]:
        filename = getattr(file_obj, "file_name", None) or fallback or None
        return f"{filename}.gif" if filename and not filename.lower().endswith(".gif") else filename
