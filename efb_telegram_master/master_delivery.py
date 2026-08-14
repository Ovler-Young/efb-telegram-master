"""Telegram-to-EFB message conversion and delivery."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import humanize
from ehforwarderbot import coordinator
from ehforwarderbot.constants import MsgType
from ehforwarderbot.exceptions import EFBChatNotFound, EFBException, EFBMessageError, EFBMessageTypeNotSupported, EFBOperationNotSupported
from ehforwarderbot.message import LocationAttribute
from ehforwarderbot.types import MessageID, ModuleID
from telegram import Contact, Message, Update
from telegram.constants import FileSizeLimit
from telegram.error import TelegramError
from telegram.ext import CallbackContext
from telegram.helpers import escape_markdown

from . import utils
from .message import ETMMsg
from .msg_type import TGMsgType, get_msg_type
from .ptb_compat import sync_reply_text
from .utils import EFBChannelChatIDStr, TelegramChatID, TelegramMessageID


class MasterMessageDelivery:
    """Convert a resolved Telegram update and deliver it to its slave chat."""

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
        self, bot, msglogs, chat_manager, message_reconstructor, localize: Callable[[str], str], flags: Callable[[str], object], send_removal: Callable[[object, ETMMsg], None], logger: logging.Logger
    ) -> None:
        self.bot = bot
        self.msglogs = msglogs
        self.chat_manager = chat_manager
        self.message_reconstructor = message_reconstructor
        self.localize = localize
        self.flags = flags
        self.send_removal = send_removal
        self.logger = logger
        self.type_dict = dict(self.TYPE_DICT)
        if self.flags("animated_stickers"):
            self.type_dict[TGMsgType.AnimatedSticker] = MsgType.Animation

    def deliver(self, update: Update, context: CallbackContext, destination: EFBChannelChatIDStr, quote: bool = False, edited=None) -> None:
        assert isinstance(update, Update) and update.effective_message
        message_id = utils.message_id_to_str(update=update)
        message = update.effective_message
        channel, uid, _ = utils.chat_id_str_to_id(destination)
        if channel not in coordinator.slaves:
            self.bot.reply_error(update, self.localize("Internal error: Slave channel “{0}” not found.").format(channel))
            return
        etm_message = ETMMsg()
        log_message = True
        try:
            etm_message.uid = MessageID(message_id)
            etm_message.type_telegram = message_type = get_msg_type(message)
            if message_type not in self.type_dict:
                log_message = False
                raise EFBMessageTypeNotSupported(self.localize("{type_name} messages are not supported by EFB Telegram Master channel.").format(type_name=message_type.name))
            etm_message.type = self.type_dict[message_type]
            etm_message.put_telegram_file(message)
            etm_message.chat = self.chat_manager.get_chat(channel, uid, build_dummy=True)
            etm_message.author = etm_message.chat.self or etm_message.chat.add_self()
            etm_message.deliver_to = coordinator.slaves[channel]
            if quote:
                self.attach_target_message(message, etm_message, channel)
            if etm_message.type not in coordinator.slaves[channel].supported_message_types:
                raise EFBMessageTypeNotSupported(
                    self.localize("{type_name} messages are not supported by slave channel {channel_name}.").format(
                        type_name=etm_message.type.name, channel_name=coordinator.slaves[channel].channel_name
                    )
                )
            if edited:
                self._apply_edit(etm_message, edited)
                if (self._markdown(message.text, message.text_markdown_v2) or self._markdown(message.caption, message.caption_markdown_v2)).startswith(self.DELETE_FLAG):
                    self.send_removal(coordinator.slaves[channel], etm_message)
                    self._remove_edited_message(message)
                    log_message = False
                    return
            self._populate_message(etm_message, message, message_type)
            slave_message = coordinator.send_message(etm_message)
            etm_message.uid = slave_message.uid if slave_message and slave_message.uid else None
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
                self.msglogs.add_or_update_message_log(etm_message, message)
                if etm_message.file:
                    etm_message.file.close()

    def _populate_message(self, etm_message: ETMMsg, message: Message, message_type: TGMsgType) -> None:
        text = self._markdown(message.text, message.text_markdown_v2)
        caption = self._markdown(message.caption, message.caption_markdown_v2)
        if message_type is TGMsgType.Text:
            etm_message.text = text
        elif message_type is TGMsgType.Photo:
            assert message.photo
            etm_message.text, etm_message.mime = caption, "image/jpeg"
            self._check_file_download(message.photo[-1])
        elif message_type in (TGMsgType.Sticker, TGMsgType.AnimatedSticker):
            assert message.sticker
            etm_message.text = ""
            self._check_file_download(message.sticker)
        elif message_type is TGMsgType.Animation:
            assert message.animation
            etm_message.text, etm_message.mime = caption, message.animation.mime_type or etm_message.mime
            etm_message.filename = self._gif_filename(message.animation)
            self._check_file_download(message.animation)
        elif message_type is TGMsgType.VideoSticker:
            assert message.sticker and message.sticker.is_video
            etm_message.text, etm_message.filename, etm_message.mime = caption, self._gif_filename(message.sticker, "sticker"), "image/gif"
            self._check_file_download(message.sticker)
        elif message_type is TGMsgType.Document:
            assert message.document
            etm_message.text, etm_message.filename, etm_message.mime = caption, message.document.file_name, message.document.mime_type
            self._check_file_download(message.document)
        elif message_type is TGMsgType.Video:
            assert message.video
            etm_message.text, etm_message.mime = caption, message.video.mime_type
            self._check_file_download(message.video)
        elif message_type is TGMsgType.VideoNote:
            assert message.video_note
            etm_message.text = caption
            self._check_file_download(message.video_note)
        elif message_type is TGMsgType.Audio:
            assert message.audio
            etm_message.text = f"{message.audio.title} - {message.audio.performer}\n{caption}"
            etm_message.mime = message.audio.mime_type
            self._check_file_download(message.audio)
        elif message_type is TGMsgType.Voice:
            assert message.voice
            etm_message.text, etm_message.mime = caption, message.voice.mime_type
            self._check_file_download(message.voice)
        elif message_type is TGMsgType.Location:
            assert message.location
            etm_message.text, etm_message.attributes = self.localize("Location"), LocationAttribute(message.location.latitude, message.location.longitude)
        elif message_type is TGMsgType.Venue:
            assert message.venue
            etm_message.text = f"📍 {message.venue.title}\n{message.venue.address}"
            etm_message.attributes = LocationAttribute(message.venue.location.latitude, message.venue.location.longitude)
        elif message_type is TGMsgType.Contact:
            assert message.contact
            contact: Contact = message.contact
            etm_message.text = self.localize("Shared a contact: {first_name} {last_name}\n{phone_number}").format(
                first_name=contact.first_name, last_name=contact.last_name, phone_number=contact.phone_number
            )
        elif message_type is TGMsgType.Dice:
            assert message.dice
            etm_message.text = f"{message.dice.emoji} = {message.dice.value}"
        else:
            raise EFBMessageTypeNotSupported(self.localize("Message type {0} is not supported.").format(message_type.name))

    def _apply_edit(self, etm_message: ETMMsg, edited) -> None:
        etm_message.edit = True
        etm_message.uid = MessageID(edited.slave_message_id)
        if etm_message.file_unique_id and etm_message.file_unique_id != edited.file_unique_id:
            etm_message.edit_media = True

    def _remove_edited_message(self, message: Message) -> None:
        if not self.flags("prevent_message_removal"):
            try:
                self.bot.delete_message(message.chat_id, message.message_id)
                return
            except TelegramError:
                pass
        sync_reply_text(self.bot, message, self.localize("Message is removed in remote chat."))

    def attach_target_message(self, telegram_message: Message, etm_message: ETMMsg, channel: ModuleID) -> ETMMsg:
        reply_to = telegram_message.reply_to_message
        assert reply_to
        target_log = self.msglogs.get_msg_log(master_msg_id=utils.message_id_to_str(TelegramChatID(reply_to.chat.id), TelegramMessageID(reply_to.message_id)))
        if not target_log or not target_log.slave_origin_uid or target_log.provenance == "mtproto_ingested":
            return etm_message
        target_channel, _, _ = utils.chat_id_str_to_id(EFBChannelChatIDStr(target_log.slave_origin_uid))
        if target_channel != channel:
            return etm_message
        target_message: ETMMsg = self.message_reconstructor.build(target_log, recur=False)
        target_message.target = None
        etm_message.target = target_message
        return etm_message

    def _check_file_download(self, file_obj: Any) -> None:
        size = getattr(file_obj, "file_size", None)
        if size and not self.flags("local_tdlib_api") and size > FileSizeLimit.FILESIZE_DOWNLOAD:
            raise EFBMessageError(
                self.localize("Attachment is too large ({size}). Maximum allowed by Telegram Bot API is {max_size}. (AT01)").format(
                    size=humanize.naturalsize(size), max_size=humanize.naturalsize(FileSizeLimit.FILESIZE_DOWNLOAD)
                )
            )

    @staticmethod
    def _markdown(plain: Optional[str], markdown: Optional[str]) -> str:
        return plain if plain and markdown == escape_markdown(plain, version=2) else markdown or ""

    @staticmethod
    def _gif_filename(file_obj: Any, fallback: str = "") -> Optional[str]:
        filename = getattr(file_obj, "file_name", None) or fallback or None
        return f"{filename}.gif" if filename and not filename.lower().endswith(".gif") else filename
