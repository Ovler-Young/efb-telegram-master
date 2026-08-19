"""Filters for Telegram message events and message relationships."""

from typing import Optional, cast

from telethon.events import MessageEdited, NewMessage
from telethon.events.common import EventCommon

from .filters import BaseFilter

__all__ = ["edited", "message", "reply_to"]


class _Message(BaseFilter):
    def filter(self, event: EventCommon):
        # Telethon versions differ on whether edited messages inherit this event.
        return isinstance(event, (NewMessage.Event, MessageEdited.Event))

    def __repr__(self):
        return "Message"


message = _Message()


class _EditedMessage(_Message):
    def __init__(self, message_id: Optional[int]):
        self.message_id = message_id

    def filter(self, event: EventCommon):
        if not super().filter(event):
            return False
        if not isinstance(event, MessageEdited.Event):
            return False
        if self.message_id is not None:
            message = cast(MessageEdited.Event, event).message
            return message.id == self.message_id
        return True

    def __repr__(self):
        return f"EditedMessage({self.message_id})"


edited = _EditedMessage
"""Filter edited messages, optionally by message ID."""


class _ReplyToMessage(_Message):
    def __init__(self, *message_ids: Optional[int]):
        self.message_ids = message_ids

    def filter(self, event: EventCommon):
        if not super().filter(event):
            return False
        message = cast(NewMessage.Event, event).message
        if message.reply_to_msg_id is None:
            return False
        if self.message_ids:
            return message.reply_to_msg_id in self.message_ids
        return True

    def __repr__(self):
        return f"ReplyTo({self.message_ids})"


reply_to = _ReplyToMessage
"""Filter messages that reply to the given message IDs."""
