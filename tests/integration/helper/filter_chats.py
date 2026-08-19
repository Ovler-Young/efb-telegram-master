"""Filters that scope events to Telegram chats."""

from typing import Set

from telethon.events.common import EventCommon

from .filters import BaseFilter

__all__ = ["in_chats"]


class _InChats(BaseFilter):
    def __init__(self, *args: int):
        self.chats: Set[int] = set(args)

    def filter(self, event: EventCommon) -> bool:
        return event.chat_id in self.chats

    def __repr__(self):
        return f"InChats({self.chats})"


in_chats = _InChats
"""Filter events in the given chat IDs."""
