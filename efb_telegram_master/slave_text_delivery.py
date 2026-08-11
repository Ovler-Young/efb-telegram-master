"""Text formatting used by slave text and image deliveries."""

import html
import urllib.parse
from typing import Optional

import telegram
from ehforwarderbot import Message
from ehforwarderbot.chat import Chat, SelfChatMember
from ehforwarderbot.message import LinkAttribute
from telegram._utils.types import ReplyMarkup
from telegram.constants import ChatAction

from .slave_delivery_helpers import edit_metadata, send_identity
from .utils import OldMsgID, TelegramChatID, TelegramMessageID, TelegramTopicID


class TextDelivery:
    def __init__(self, admin_id: int, bot=None, translate=None, logger=None) -> None:
        self.admin_id = admin_id
        self.bot = bot
        self.translate = translate or (lambda text: text)
        self.logger = logger

    def html_substitutions(self, msg: Message) -> str:
        text = msg.text or ""
        if not msg.substitutions:
            return html.escape(text)
        pieces: list[str] = []
        previous = 0
        for start, end in sorted(msg.substitutions):
            pieces.append(html.escape(text[previous:start]))
            substitute = msg.substitutions[start, end]
            if isinstance(substitute, SelfChatMember) or (isinstance(substitute, Chat) and substitute.has_self):
                pieces.append(f'<a href="tg://user?id={self.admin_id}">{html.escape(text[start:end])}</a>')
            else:
                pieces.append(f"<code>{html.escape(text[start:end])}</code>")
            previous = end
        pieces.append(html.escape(text[previous:]))
        return "".join(pieces)

    def text(self, msg: Message, destination: TelegramChatID, thread_id: Optional[TelegramTopicID], template: str, reactions: str, old_message_id: Optional[OldMsgID] = None, reply_to: Optional[TelegramMessageID] = None, reply_markup: Optional[ReplyMarkup] = None, silent: bool = False) -> telegram.Message:
        assert self.bot is not None
        self.bot.send_chat_action(destination, ChatAction.TYPING, message_thread_id=thread_id)
        text = self.html_substitutions(msg)
        if old_message_id:
            return self.bot.edit_message_text(chat_id=old_message_id[0], message_id=old_message_id[1], text=text, prefix=template, suffix=reactions, parse_mode="HTML", reply_markup=reply_markup, **edit_metadata(msg))
        return self.bot.send_message(destination, text=text, prefix=template, suffix=reactions, parse_mode="HTML", reply_to_message_id=reply_to, message_thread_id=thread_id, reply_markup=reply_markup, disable_notification=silent, **send_identity(msg))

    def link(self, msg: Message, destination: TelegramChatID, thread_id: Optional[TelegramTopicID], template: str, reactions: str, old_message_id: Optional[OldMsgID] = None, reply_to: Optional[TelegramMessageID] = None, reply_markup: Optional[ReplyMarkup] = None, silent: bool = False) -> telegram.Message:
        assert self.bot is not None and isinstance(msg.attributes, LinkAttribute)
        self.bot.send_chat_action(destination, ChatAction.TYPING, message_thread_id=thread_id)
        attributes = msg.attributes
        thumbnail_url = urllib.parse.quote(attributes.image or "", safe="?=&#:/")
        thumbnail = f'<a href="{thumbnail_url}">🔗</a>' if thumbnail_url else "🔗"
        text = "%s <a href=\"%s\">%s</a>\n%s" % (thumbnail, urllib.parse.quote(attributes.url, safe="?=&#:/"), html.escape(attributes.title or attributes.url), html.escape(attributes.description or ""))
        if msg.text:
            text += "\n\n" + self.html_substitutions(msg)
        if old_message_id:
            return self.bot.edit_message_text(text=text, chat_id=old_message_id[0], message_id=old_message_id[1], prefix=template, suffix=reactions, parse_mode="HTML", reply_markup=reply_markup, **edit_metadata(msg))
        return self.bot.send_message(chat_id=destination, text=text, prefix=template, suffix=reactions, parse_mode="HTML", reply_to_message_id=reply_to, message_thread_id=thread_id, reply_markup=reply_markup, disable_notification=silent, **send_identity(msg))

    def unsupported(self, msg: Message, destination: TelegramChatID, thread_id: Optional[TelegramTopicID], template: str, reactions: str, old_message_id: Optional[OldMsgID] = None, reply_to: Optional[TelegramMessageID] = None, reply_markup: Optional[ReplyMarkup] = None, silent: bool = False) -> telegram.Message:
        assert self.bot is not None
        self.bot.send_chat_action(destination, ChatAction.TYPING, message_thread_id=thread_id)
        text = self.html_substitutions(msg) if msg.text else ""
        prefix = template + " " + self.translate("(unsupported)")
        if old_message_id:
            return self.bot.edit_message_text(chat_id=old_message_id[0], message_id=old_message_id[1], text=text, parse_mode="HTML", prefix=prefix + self.translate(" [Edited]"), suffix=reactions, reply_markup=reply_markup, **edit_metadata(msg))
        return self.bot.send_message(destination, text=text, parse_mode="HTML", prefix=prefix, suffix=reactions, reply_to_message_id=reply_to, message_thread_id=thread_id, reply_markup=reply_markup, disable_notification=silent, **send_identity(msg))
