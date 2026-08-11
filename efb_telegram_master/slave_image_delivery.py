"""Image delivery with explicit Telegram and file-transfer dependencies."""

import os
from typing import Callable, Optional

import telegram
from ehforwarderbot import Message
from PIL import Image
from telegram import InputMedia, InputMediaDocument, InputMediaPhoto
from telegram._utils.types import ReplyMarkup
from telegram.constants import ChatAction

from .slave_delivery_helpers import edit_metadata, remote_image_url, send_identity, send_remote_image_placeholder
from .utils import OldMsgID, TelegramChatID, TelegramMessageID, TelegramTopicID


class ImageDelivery:
    IMG_MIN_SIZE = 1600
    IMG_MAX_SIZE = 1200
    IMG_SIZE_RATIO = 3.5
    IMG_SIZE_MAX_RATIO = 10

    def __init__(self, bot, flag, logger, translate, text_delivery, file_transfer, notice_sender, temp_directory: Callable[[], Optional[str]]) -> None:
        self.bot = bot
        self.flag = flag
        self.logger = logger
        self.translate = translate
        self.text_delivery = text_delivery
        self.file_transfer = file_transfer
        self.notice_sender = notice_sender
        self.temp_directory = temp_directory

    def slave_message_image(
        self,
        msg: Message,
        tg_dest: TelegramChatID,
        thread_id: Optional[TelegramTopicID],
        msg_template: str,
        reactions: str,
        old_msg_id: Optional[OldMsgID] = None,
        target_msg_id: Optional[TelegramMessageID] = None,
        reply_markup: Optional[ReplyMarkup] = None,
        silent: bool = False,
    ) -> telegram.Message:
        image_url = remote_image_url(msg)
        if not image_url:
            assert msg.file
        self.bot.send_chat_action(tg_dest, ChatAction.UPLOAD_PHOTO, message_thread_id=thread_id)
        if msg.path:
            self.logger.debug("[%s] Image file size is %s bytes.", msg.uid, os.stat(msg.path).st_size)

        if msg.text:
            text = self.text_delivery.html_substitutions(msg)
        elif msg_template:
            placeholder_flag = self.flag("default_media_prompt")
            if placeholder_flag == "emoji":
                text = "🖼️"
            elif placeholder_flag == "text":
                text = self.translate("Sent a picture.")
            else:
                text = ""
        else:
            text = ""

        if image_url:
            if old_msg_id:
                try:
                    edit_kwargs = edit_metadata(msg)
                    if msg.edit_media:
                        res = self.bot.edit_message_media(chat_id=old_msg_id[0], message_id=old_msg_id[1], media=InputMediaPhoto(image_url), reply_markup=reply_markup, **edit_kwargs)
                        if not text:
                            return res
                    return self.bot.edit_message_caption(
                        chat_id=old_msg_id[0], message_id=old_msg_id[1], reply_markup=reply_markup, prefix=msg_template, suffix=reactions, caption=text, parse_mode="HTML", **edit_kwargs
                    )
                except telegram.error.BadRequest as e:
                    self.logger.warning("[%s] Failed to edit remote image/caption (%s); sending new message instead.", msg.uid, type(e).__name__)
                    if old_msg_id[0] == tg_dest:
                        target_msg_id = target_msg_id or old_msg_id[1]

            try:
                return self.bot.send_photo(
                    tg_dest,
                    image_url,
                    prefix=msg_template,
                    suffix=reactions,
                    caption=text,
                    parse_mode="HTML",
                    reply_to_message_id=target_msg_id,
                    message_thread_id=thread_id,
                    reply_markup=reply_markup,
                    disable_notification=silent,
                    _fallback_to_document=False,
                    **send_identity(msg),
                )
            except telegram.error.BadRequest as e:
                self.logger.warning("[%s] Failed to send remote image URL (%s); sending editable placeholder.", msg.uid, type(e).__name__)
                return send_remote_image_placeholder(
                    self.bot, self.file_transfer, self.temp_directory, tg_dest, thread_id,
                    msg_template, reactions, text, target_msg_id, reply_markup, silent, as_document=False,
                )

        msg_file = msg.file
        assert msg_file is not None
        try:
            # Avoid Telegram compression of pictures by sending high definition image messages as files
            # Code adopted from wolfsilver's fork:
            # https://github.com/wolfsilver/efb-telegram-master/blob/99668b60f7ff7b6363dfc87751a18281d9a74a09/efb_telegram_master/slave_message.py#L142-L163
            #
            # Rules:
            # 1. If the picture is too large -- shorter side is greater than IMG_MIN_SIZE, send as file.
            # 2. If the picture is large and thin --
            #        longer side is greater than IMG_MAX_SIZE, and
            #        aspect ratio (longer to shorter side ratio) is greater than IMG_SIZE_RATIO,
            #    send as file.
            # 3. If the picture is too thin -- aspect ratio grater than IMG_SIZE_MAX_RATIO, send as file.

            try:
                if msg.path is None:
                    # When we don't have a local file path (e.g. file-like only),
                    # skip the heuristic and default to sending as photo.
                    send_as_file = False
                else:
                    pic_img = Image.open(msg.path)
                    max_size = max(pic_img.size)
                    min_size = min(pic_img.size)
                    img_ratio = max_size / min_size

                    if min_size > self.IMG_MIN_SIZE:
                        send_as_file = True
                    elif max_size > self.IMG_MAX_SIZE and img_ratio > self.IMG_SIZE_RATIO:
                        send_as_file = True
                    elif img_ratio >= self.IMG_SIZE_MAX_RATIO:
                        send_as_file = True
                    else:
                        send_as_file = False
            except IOError:  # Ignore when the image cannot be properly identified.
                send_as_file = False

            file_too_large = self.file_transfer.check_size(msg_file)
            edit_media = msg.edit_media
            if file_too_large:
                oversized_message, edit_media = self.notice_sender.send(
                    msg, file_too_large, tg_dest, thread_id, msg_template, reactions, text, old_msg_id, target_msg_id, reply_markup, silent
                )
                if oversized_message is not None:
                    return oversized_message

            if old_msg_id:
                try:
                    edit_kwargs = edit_metadata(msg)
                    if edit_media:
                        assert msg.path
                        media: InputMedia
                        file = self.file_transfer.prepare(msg_file, msg.path, msg.filename)
                        if send_as_file:
                            media = InputMediaDocument(file)
                        else:
                            media = InputMediaPhoto(file)
                        res = self.bot.edit_message_media(chat_id=old_msg_id[0], message_id=old_msg_id[1], media=media, reply_markup=reply_markup, **edit_kwargs)
                        if not text:
                            return res
                    return self.bot.edit_message_caption(
                        chat_id=old_msg_id[0], message_id=old_msg_id[1], reply_markup=reply_markup, prefix=msg_template, suffix=reactions, caption=text, parse_mode="HTML", **edit_kwargs
                    )
                except telegram.error.BadRequest as e:
                    self.logger.warning("[%s] Failed to edit media/caption (%s); sending new message instead.", msg.uid, type(e).__name__)
                    if old_msg_id[0] == tg_dest:
                        target_msg_id = target_msg_id or old_msg_id[1]
                    msg_file.seek(0)

            if send_as_file:
                assert msg.path
                file = self.file_transfer.prepare(msg_file, msg.path, msg.filename)
                return self.bot.send_document(
                    tg_dest,
                    file,
                    prefix=msg_template,
                    suffix=reactions,
                    caption=text,
                    parse_mode="HTML",
                    filename=msg.filename,
                    reply_to_message_id=target_msg_id,
                    message_thread_id=thread_id,
                    reply_markup=reply_markup,
                    disable_notification=silent,
                    **send_identity(msg),
                )
            else:
                try:
                    assert msg.path
                    file = self.file_transfer.prepare(msg_file, msg.path, msg.filename)
                    return self.bot.send_photo(
                        tg_dest,
                        file,
                        prefix=msg_template,
                        suffix=reactions,
                        caption=text,
                        parse_mode="HTML",
                        reply_to_message_id=target_msg_id,
                        message_thread_id=thread_id,
                        reply_markup=reply_markup,
                        disable_notification=silent,
                        **send_identity(msg),
                    )
                except telegram.error.BadRequest as e:
                    self.logger.error("[%s] Failed to send as image (%s); sending as document.", msg.uid, type(e).__name__)
                    assert msg.path
                    msg_file.seek(0)
                    file = self.file_transfer.prepare(msg_file, msg.path, msg.filename)
                    return self.bot.send_document(
                        tg_dest,
                        file,
                        prefix=msg_template,
                        suffix=reactions,
                        caption=text,
                        parse_mode="HTML",
                        filename=msg.filename,
                        reply_to_message_id=target_msg_id,
                        message_thread_id=thread_id,
                        reply_markup=reply_markup,
                        disable_notification=silent,
                        **send_identity(msg),
                    )
        finally:
            msg_file.close()
