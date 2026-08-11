"""Telegram delivery for animations and stickers."""

import os
import tempfile
from typing import Callable, Optional

import telegram
from ehforwarderbot import Message
from PIL import Image
from telegram import InputFile, InputMediaAnimation
from telegram._utils.types import ReplyMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError

from .slave_delivery_helpers import chat_info_keyboard, edit_metadata, send_identity
from .utils import OldMsgID, TelegramChatID, TelegramMessageID, TelegramTopicID


class SlaveMediaDelivery:
    def __init__(self, bot, logger, text_delivery, file_transfer, notice_sender, temp_directory: Callable[[], Optional[str]]) -> None:
        self.bot = bot
        self.logger = logger
        self.text_delivery = text_delivery
        self.file_transfer = file_transfer
        self.notice_sender = notice_sender
        self.temp_directory = temp_directory

    def animation(
        self,
        msg: Message,
        destination: TelegramChatID,
        thread_id: Optional[TelegramTopicID],
        template: str,
        reactions: str,
        old_message_id: Optional[OldMsgID] = None,
        reply_to: Optional[TelegramMessageID] = None,
        reply_markup: Optional[ReplyMarkup] = None,
        silent: Optional[bool] = None,
    ) -> telegram.Message:
        self.bot.send_chat_action(destination, ChatAction.UPLOAD_PHOTO, message_thread_id=thread_id)
        if msg.path:
            self.logger.debug("[%s] Animation file size is %s bytes.", msg.uid, os.stat(msg.path).st_size)
        text = self.text_delivery.html_substitutions(msg) if msg.text else ""
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
                    assert msg.file and msg.path
                    media = InputMediaAnimation(self.file_transfer.prepare(msg.file, msg.path, msg.filename))
                    sent = self.bot.edit_message_media(chat_id=old_message_id[0], message_id=old_message_id[1], media=media, reply_markup=reply_markup, **metadata)
                    if not text:
                        return sent
                return self.bot.edit_message_caption(
                    chat_id=old_message_id[0], message_id=old_message_id[1], prefix=template, suffix=reactions, reply_markup=reply_markup, caption=text, parse_mode="HTML", **metadata
                )
            assert msg.file and msg.path
            attachment = self.file_transfer.prepare(msg.file, msg.path, msg.filename)
            animation = attachment if isinstance(attachment, str) else InputFile(attachment, filename=msg.filename or "")
            return self.bot.send_animation(
                destination,
                animation,
                prefix=template,
                suffix=reactions,
                caption=text,
                parse_mode="HTML",
                reply_to_message_id=reply_to,
                message_thread_id=thread_id,
                reply_markup=reply_markup,
                disable_notification=silent,
                **send_identity(msg),
            )
        finally:
            if msg.file is not None:
                msg.file.close()

    def sticker(
        self,
        msg: Message,
        destination: TelegramChatID,
        thread_id: Optional[TelegramTopicID],
        template: str,
        reactions: str,
        old_message_id: Optional[OldMsgID] = None,
        reply_to: Optional[TelegramMessageID] = None,
        reply_markup: Optional[ReplyMarkup] = None,
        silent: bool = False,
    ) -> telegram.Message:
        self.bot.send_chat_action(destination, ChatAction.UPLOAD_PHOTO, message_thread_id=thread_id)
        sticker_markup = chat_info_keyboard(msg, template, reactions, reply_markup)
        if msg.path:
            self.logger.debug("[%s] Sticker file size is %s bytes.", msg.uid, os.stat(msg.path).st_size)
        try:
            if msg.edit_media and old_message_id is not None:
                reply_to = old_message_id[1] if old_message_id[0] == str(destination) else reply_to
                old_message_id = None
            if old_message_id and not msg.edit_media:
                try:
                    return self.bot.edit_message_reply_markup(chat_id=old_message_id[0], message_id=old_message_id[1], reply_markup=sticker_markup, **edit_metadata(msg))
                except TelegramError:
                    return self.bot.send_message(
                        chat_id=old_message_id[0], reply_to_message_id=old_message_id[1], prefix=template, text=msg.text, suffix=reactions, reply_markup=reply_markup, disable_notification=silent
                    )
            notice = self.file_transfer.check_size(msg.file)
            if notice:
                oversized, _ = self.notice_sender.send(
                    msg, notice, destination, thread_id, template, reactions, self.text_delivery.html_substitutions(msg), old_message_id, reply_to, reply_markup, silent
                )
                if oversized is not None:
                    return oversized
            webp = None
            try:
                assert msg.file is not None
                webp = tempfile.NamedTemporaryFile(suffix=".webp", dir=self.temp_directory())
                Image.open(msg.file).convert("RGBA").save(webp, "webp")
                webp.seek(0)
                return self.bot.send_sticker(
                    destination,
                    self.file_transfer.prepare(webp, webp.name, msg.filename),
                    reply_markup=sticker_markup,
                    message_thread_id=thread_id,
                    reply_to_message_id=reply_to,
                    disable_notification=silent,
                    **send_identity(msg),
                )
            except IOError:
                self.logger.warning("[%s] Failed to convert image to webp sticker, sending as document.", msg.uid)
                assert msg.file and msg.path
                return self.bot.send_document(
                    destination,
                    self.file_transfer.prepare(msg.file, msg.path, msg.filename),
                    prefix=template,
                    suffix=reactions,
                    message_thread_id=thread_id,
                    caption=msg.text,
                    filename=msg.filename,
                    reply_to_message_id=reply_to,
                    reply_markup=reply_markup,
                    disable_notification=silent,
                    **send_identity(msg),
                )
            finally:
                if webp and not webp.closed:
                    webp.close()
        finally:
            if msg.file and not msg.file.closed:
                msg.file.close()
