"""Telegram delivery for document, voice, location, and video messages."""

import os
import tempfile
from typing import Callable, Optional

import telegram
from ehforwarderbot import Message
from ehforwarderbot.constants import MsgType
from ehforwarderbot.message import LocationAttribute
from telegram import InputMediaDocument, InputMediaVideo
from telegram._utils.types import ReplyMarkup
from telegram.constants import ChatAction

from .slave_delivery_helpers import chat_info_keyboard, edit_metadata, remote_image_filename, remote_image_url, send_identity, send_remote_image_placeholder
from .utils import OldMsgID, TelegramChatID, TelegramMessageID, TelegramTopicID


class SlaveFileDelivery:
    def __init__(self, bot, flag, logger, translate, text_delivery, file_transfer, notice_sender, temp_directory: Callable[[], Optional[str]]) -> None:
        self.bot = bot
        self.flag = flag
        self.logger = logger
        self.translate = translate
        self.text_delivery = text_delivery
        self.file_transfer = file_transfer
        self.notice_sender = notice_sender
        self.temp_directory = temp_directory

    def _caption(self, msg: Message, template: str, emoji: str, label: str) -> str:
        if msg.text:
            return self.text_delivery.html_substitutions(msg)
        if not template:
            return ""
        setting = self.flag("default_media_prompt")
        return emoji if setting == "emoji" else self.translate(label) if setting == "text" else ""

    def file(self, msg: Message, destination: TelegramChatID, thread_id: Optional[TelegramTopicID], template: str, reactions: str, old_message_id: Optional[OldMsgID] = None, reply_to: Optional[TelegramMessageID] = None, reply_markup: Optional[ReplyMarkup] = None, silent: bool = False) -> telegram.Message:
        self.bot.send_chat_action(destination, ChatAction.UPLOAD_DOCUMENT, message_thread_id=thread_id)
        image_url = remote_image_url(msg) if msg.type == MsgType.Image else None
        filename = remote_image_filename(msg, image_url) if image_url else os.path.basename(msg.path) if msg.filename is None and msg.path else msg.filename
        assert filename is not None
        filename = filename.replace(";", " ")
        text = self._caption(msg, template, "📄", "Sent a file.")
        try:
            if image_url:
                if old_message_id:
                    metadata = edit_metadata(msg)
                    if msg.edit_media:
                        sent = self.bot.edit_message_media(chat_id=old_message_id[0], message_id=old_message_id[1], media=InputMediaDocument(image_url), **metadata)
                        if not text:
                            return sent
                    return self.bot.edit_message_caption(chat_id=old_message_id[0], message_id=old_message_id[1], reply_markup=reply_markup, prefix=template, suffix=reactions, caption=text, parse_mode="HTML", **metadata)
                try:
                    return self.bot.send_document(destination, image_url, prefix=template, suffix=reactions, caption=text, parse_mode="HTML", filename=filename, reply_to_message_id=reply_to, message_thread_id=thread_id, reply_markup=reply_markup, disable_notification=silent, **send_identity(msg))
                except telegram.error.BadRequest as error:
                    self.logger.warning("[%s] Failed to send remote image URL as document (%s); sending editable placeholder.", msg.uid, type(error).__name__)
                    return send_remote_image_placeholder(self.bot, self.file_transfer, self.temp_directory, destination, thread_id, template, reactions, text, reply_to, reply_markup, silent, as_document=True)
            notice = self.file_transfer.check_size(msg.file)
            edit_media = msg.edit_media
            if notice:
                oversized, edit_media = self.notice_sender.send(msg, notice, destination, thread_id, template, reactions, text, old_message_id, reply_to, reply_markup, silent)
                if oversized is not None:
                    return oversized
            if old_message_id:
                metadata = edit_metadata(msg)
                if edit_media:
                    assert msg.file is not None and msg.path is not None
                    sent = self.bot.edit_message_media(chat_id=old_message_id[0], message_id=old_message_id[1], media=InputMediaDocument(self.file_transfer.prepare(msg.file, msg.path, msg.filename)), **metadata)
                    if not text:
                        return sent
                return self.bot.edit_message_caption(chat_id=old_message_id[0], message_id=old_message_id[1], reply_markup=reply_markup, prefix=template, suffix=reactions, caption=text, parse_mode="HTML", **metadata)
            assert msg.file is not None and msg.path is not None
            return self.bot.send_document(destination, self.file_transfer.prepare(msg.file, msg.path, filename), prefix=template, suffix=reactions, caption=text, parse_mode="HTML", filename=filename, reply_to_message_id=reply_to, message_thread_id=thread_id, reply_markup=reply_markup, disable_notification=silent, **send_identity(msg))
        finally:
            if msg.file is not None:
                msg.file.close()

    def voice(self, msg: Message, destination: TelegramChatID, thread_id: Optional[TelegramTopicID], template: str, reactions: str, old_message_id: Optional[OldMsgID] = None, reply_to: Optional[TelegramMessageID] = None, reply_markup: Optional[ReplyMarkup] = None, silent: bool = False) -> telegram.Message:
        self.bot.send_chat_action(destination, ChatAction.RECORD_VOICE, message_thread_id=thread_id)
        text = self.text_delivery.html_substitutions(msg) if msg.text else ""
        self.logger.debug("[%s] Message is a voice file.", msg.uid)
        try:
            notice = self.file_transfer.check_size(msg.file)
            edit_media = msg.edit_media
            if notice:
                oversized, edit_media = self.notice_sender.send(msg, notice, destination, thread_id, template, reactions, text, old_message_id, reply_to, reply_markup, silent)
                if oversized is not None:
                    return oversized
            if old_message_id and not edit_media:
                return self.bot.edit_message_caption(chat_id=old_message_id[0], message_id=old_message_id[1], reply_markup=reply_markup, prefix=template, suffix=reactions, caption=text, parse_mode="HTML", **edit_metadata(msg))
            if old_message_id:
                self.logger.warning("[%s] Cannot edit voice message media. Sending new message instead.", msg.uid)
                template += " " + self.translate("[Edited]")
                reply_to = reply_to or old_message_id[1] if str(destination) == old_message_id[0] else reply_to
            assert msg.file is not None
            import pydub
            with tempfile.NamedTemporaryFile(suffix=".ogg", dir=self.temp_directory()) as converted:
                try:
                    pydub.AudioSegment.from_file(msg.file).export(converted.name, format="ogg", codec="libopus", parameters=["-vbr", "on"])
                    return self.bot.send_voice(destination, self.file_transfer.prepare(converted, converted.name, msg.filename), prefix=template, suffix=reactions, caption=text, parse_mode="HTML", reply_to_message_id=reply_to, message_thread_id=thread_id, reply_markup=reply_markup, disable_notification=silent, **send_identity(msg))
                except pydub.exceptions.CouldntDecodeError as error:
                    self.logger.error("[%s] Failed to decode audio file for conversion (%s); sending as file.", msg.uid, type(error).__name__)
                    msg.file.seek(0)
                    return self.file(msg, destination, thread_id, template, reactions, reply_to=reply_to, reply_markup=reply_markup, silent=silent)
        finally:
            if msg.file is not None and not msg.file.closed:
                msg.file.close()

    def location(self, msg: Message, destination: TelegramChatID, thread_id: Optional[TelegramTopicID], template: str, reactions: str, old_message_id: Optional[OldMsgID] = None, reply_to: Optional[TelegramMessageID] = None, reply_markup: Optional[ReplyMarkup] = None, silent: bool = False) -> telegram.Message:
        self.bot.send_chat_action(destination, ChatAction.FIND_LOCATION, message_thread_id=thread_id)
        assert isinstance(msg.attributes, LocationAttribute)
        if old_message_id and old_message_id[0] == str(destination):
            template += " " + self.translate("[edited]")
            reply_to = reply_to or old_message_id[1]
        return self.bot.send_location(destination, latitude=msg.attributes.latitude, longitude=msg.attributes.longitude, reply_to_message_id=reply_to, message_thread_id=thread_id, reply_markup=chat_info_keyboard(msg, template, reactions, reply_markup), disable_notification=silent, **send_identity(msg))

    def video(self, msg: Message, destination: TelegramChatID, thread_id: Optional[TelegramTopicID], template: str, reactions: str, old_message_id: Optional[OldMsgID] = None, reply_to: Optional[TelegramMessageID] = None, reply_markup: Optional[ReplyMarkup] = None, silent: bool = False) -> telegram.Message:
        self.bot.send_chat_action(destination, ChatAction.UPLOAD_VIDEO, message_thread_id=thread_id)
        text = self._caption(msg, template, "🎥", "Sent a video.")
        try:
            notice = self.file_transfer.check_size(msg.file)
            edit_media = msg.edit_media
            if notice:
                oversized, edit_media = self.notice_sender.send(msg, notice, destination, thread_id, template, reactions, text, old_message_id, reply_to, reply_markup, silent)
                if oversized is not None:
                    return oversized
            if old_message_id:
                metadata = edit_metadata(msg)
                if edit_media:
                    assert msg.file is not None and msg.path is not None
                    sent = self.bot.edit_message_media(chat_id=old_message_id[0], message_id=old_message_id[1], media=InputMediaVideo(self.file_transfer.prepare(msg.file, msg.path, msg.filename)), reply_markup=reply_markup, **metadata)
                    if not text:
                        return sent
                return self.bot.edit_message_caption(chat_id=old_message_id[0], message_id=old_message_id[1], reply_markup=reply_markup, prefix=template, suffix=reactions, caption=text, parse_mode="HTML", **metadata)
            assert msg.file is not None and msg.path is not None
            return self.bot.send_video(destination, self.file_transfer.prepare(msg.file, msg.path, msg.filename), prefix=template, suffix=reactions, caption=text, parse_mode="HTML", reply_to_message_id=reply_to, message_thread_id=thread_id, reply_markup=reply_markup, disable_notification=silent, **send_identity(msg))
        finally:
            if msg.file is not None:
                msg.file.close()
