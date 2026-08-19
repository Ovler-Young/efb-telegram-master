"""Filters for message text and interactive content."""

import re
from typing import cast

from telethon.events import NewMessage
from telethon.events.common import EventCommon
from telethon.tl.custom import Message
from telethon.tl.types import MessageMediaWebPage

from .filter_messages import _Message

__all__ = ["has_button", "regex", "text"]


class _TextMessage(_Message):
    def filter(self, event: EventCommon):
        if not super().filter(event):
            return False
        message: Message = cast(NewMessage.Event, event).message
        return bool(message.raw_text) and message.action is None and (message.media is None or isinstance(message.media, MessageMediaWebPage))

    def __repr__(self):
        return "Text"


text = _TextMessage()
"""Filter text messages."""


class _RegexText(_TextMessage):
    def __init__(self, pattern: str):
        self.pattern = re.compile(pattern)

    def filter(self, event: EventCommon):
        if not super().filter(event):
            return False
        message: Message = cast(NewMessage.Event, event).message
        return bool(self.pattern.search(message.raw_text) or self.pattern.search(message.text))

    def __repr__(self):
        return f"RegexText({self.pattern})"


regex = _RegexText
"""Filter text messages whose content matches a regular expression."""


class _HasButton(_Message):
    def filter(self, event: EventCommon):
        if not super().filter(event):
            return False
        message: Message = cast(NewMessage.Event, event).message
        return message.button_count > 0

    def __repr__(self):
        return "HasButton"


has_button = _HasButton()
"""Filter messages with at least one button."""
