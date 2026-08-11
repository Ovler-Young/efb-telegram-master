"""Stateless values and helpers shared by slave delivery services."""

import os
import tempfile
import urllib.parse
from typing import Callable, Optional

from ehforwarderbot import Message
from ehforwarderbot.message import Reactions
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .utils import chat_id_to_str

REMOTE_IMAGE_URL_VENDOR_KEY = "blueset.telegram.image_url"


def edit_metadata(message: Message) -> dict[str, str]:
    sender_bot_id = (message.vendor_specific or {}).get("_sender_bot_id")
    return {"_sender_bot_id": sender_bot_id} if sender_bot_id else {}


def send_identity(message: Message) -> dict[str, str]:
    return {"_slave_id": chat_id_to_str(chat=message.chat)}


def remote_image_url(message: Message) -> Optional[str]:
    url = (message.vendor_specific or {}).get(REMOTE_IMAGE_URL_VENDOR_KEY)
    parsed = urllib.parse.urlparse(url) if isinstance(url, str) else None
    return url if parsed and parsed.scheme in {"http", "https"} and parsed.netloc else None


def remote_image_filename(message: Message, url: str) -> str:
    if message.filename:
        return message.filename
    parsed = urllib.parse.urlparse(url)
    path = f"{parsed.path};{parsed.params}" if parsed.params else parsed.path
    return urllib.parse.unquote(os.path.basename(path)) or "image"


def remote_image_placeholder(temp_directory: Callable[[], Optional[str]]):
    from PIL import Image

    placeholder = tempfile.NamedTemporaryFile(suffix=".png", dir=temp_directory())
    Image.new("RGB", (64, 64), (245, 245, 245)).save(placeholder, "PNG")
    placeholder.seek(0)
    return placeholder


def send_remote_image_placeholder(
    bot,
    file_transfer,
    temp_directory: Callable[[], Optional[str]],
    destination,
    thread_id,
    template: str,
    reactions: str,
    text: str,
    reply_to,
    reply_markup,
    silent: bool,
    *,
    as_document: bool,
):
    placeholder = remote_image_placeholder(temp_directory)
    try:
        method = bot.send_document if as_document else bot.send_photo
        return method(
            destination,
            file_transfer.prepare(placeholder, placeholder.name, "remote-image-placeholder.png"),
            prefix=template,
            suffix=reactions,
            caption=text,
            parse_mode="HTML",
            filename="remote-image-placeholder.png" if as_document else None,
            reply_to_message_id=reply_to,
            message_thread_id=thread_id,
            reply_markup=reply_markup,
            disable_notification=silent,
        )
    finally:
        placeholder.close()


def chat_info_keyboard(message: Message, template: str, reactions: str, reply_markup) -> InlineKeyboardMarkup:
    rows = []
    for label in (template, message.text, reactions):
        if label:
            rows.append([InlineKeyboardButton(label, callback_data="void")])
    existing = reply_markup if isinstance(reply_markup, InlineKeyboardMarkup) else InlineKeyboardMarkup([])
    return InlineKeyboardMarkup(rows + [list(row) for row in existing.inline_keyboard])


def reactions_footer(reactions: Reactions) -> str:
    values = [f"{emoji}×{len(members)}" for emoji, members in reactions.items() if members]
    return f"[{', '.join(values)}]" if values else ""
