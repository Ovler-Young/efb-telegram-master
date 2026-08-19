"""Filters for non-message Telegram updates."""

from typing import Optional, cast

from telethon.events import ChatAction, MessageDeleted, UserUpdate
from telethon.events.common import EventCommon
from telethon.tl.custom import Message

from .filters import BaseFilter

__all__ = ["chat_action", "deleted", "new_photo", "new_title", "typing"]


class _Typing(BaseFilter):
    def filter(self, event: EventCommon):
        return isinstance(event, UserUpdate.Event)

    def __repr__(self):
        return "Message"


typing = _Typing()
"""Filter user typing updates."""


class _ChatAction(BaseFilter):
    def filter(self, event) -> bool:
        return isinstance(event, ChatAction.Event)

    def __repr__(self):
        return "ChatAction"


chat_action = _ChatAction()


class _NewTitle(_ChatAction):
    def filter(self, event) -> bool:
        if not super().filter(event):
            return False
        event = cast(ChatAction.Event, event)
        return event.new_title is not None

    def __repr__(self):
        return "NewTitle"


new_title = _NewTitle()


class _NewPhoto(_ChatAction):
    def filter(self, event) -> bool:
        if not super().filter(event):
            return False
        event = cast(ChatAction.Event, event)
        return event.new_photo is not None

    def __repr__(self):
        return "NewPhoto"


new_photo = _NewPhoto()


class _DeletedMessage(BaseFilter):
    def __init__(self, message: Optional[Message] = None):
        self.message = message

    def filter(self, event: EventCommon):
        if not isinstance(event, MessageDeleted.Event):
            return False
        if self.message is None:
            return True
        return self.message.id in event.deleted_ids

    def __repr__(self):
        return "DeletedMessage"


deleted = _DeletedMessage
