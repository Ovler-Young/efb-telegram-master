# coding=utf-8

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, cast

from ehforwarderbot import coordinator
from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.types import ModuleID
from telegram import Update
from telegram.ext import ConversationHandler
from telegram.ext._utils.types import ConversationDict

from efb_telegram_master.chat.chat import ETMChatMixin
from efb_telegram_master.core.utils import EFBChannelChatIDStr, TelegramChatID, TelegramMessageID

CallbackSessionID = Tuple[TelegramChatID, TelegramMessageID]


@dataclass
class CallbackSession:
    """The callback state and Telegram user authorized to consume it."""

    owner_id: int
    storage: "ChatListStorage"


class ChatListStorage:
    """The candidate chats and pagination state for one callback message."""

    def __init__(self, chats: List[ETMChatMixin], offset: int = 0):
        self.__chats: List[ETMChatMixin] = []
        self.channels: Dict[ModuleID, SlaveChannel] = {}
        self.chats = chats.copy()
        self.offset = offset
        self.update: Optional[Update] = None
        self.candidates: Optional[List[EFBChannelChatIDStr]] = None

    @property
    def length(self) -> int:
        return len(self.chats)

    @property
    def chats(self) -> List[ETMChatMixin]:
        return self.__chats

    @chats.setter
    def chats(self, value: List[ETMChatMixin]) -> None:
        self.__chats = value
        self.offset = 0
        self.channels = {chat.module_id: coordinator.slaves[chat.module_id] for chat in value if chat.module_id in coordinator.slaves}

    def set_chat_suggestion(self, update: Update) -> None:
        self.update = update


class CallbackSessionStore:
    """Own callback-message storage and its corresponding PTB conversation state."""

    def __init__(self, bot, chats_per_page: Callable[[], int]):
        self._bot = bot
        self._chats_per_page = chats_per_page
        self._storage: Dict[CallbackSessionID, CallbackSession] = {}

    @staticmethod
    def set_state(handler: ConversationHandler, key: Tuple[int, ...], state: object) -> None:
        conversations = getattr(handler, "_conversations", None)
        if conversations is None:
            conversations = getattr(handler, "conversations")
        cast(ConversationDict, conversations)[key] = state

    @staticmethod
    def clear_state(handler: ConversationHandler, key: Tuple[int, ...]) -> None:
        conversations = getattr(handler, "_conversations", None)
        if conversations is None:
            conversations = getattr(handler, "conversations")
        cast(ConversationDict, conversations).pop(key, None)

    @staticmethod
    def parse_index(callback_data: str, command: str) -> Optional[int]:
        parts = callback_data.split()
        if len(parts) != 2 or parts[0] != command:
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None

    def start(self, handler: ConversationHandler, session_id: CallbackSessionID, state: object, owner_id: int, storage: ChatListStorage) -> None:
        self.store(session_id, owner_id, storage)
        self.set_state(handler, session_id, state)

    def store(self, session_id: CallbackSessionID, owner_id: int, storage: ChatListStorage) -> None:
        self._storage[session_id] = CallbackSession(owner_id, storage)

    def lookup(self, session_id: CallbackSessionID) -> Optional[ChatListStorage]:
        session = self._storage.get(session_id)
        return session.storage if session else None

    def is_owned_by(self, session_id: CallbackSessionID, user_id: Optional[int]) -> bool:
        session = self._storage.get(session_id)
        return user_id is not None and session is not None and session.owner_id == user_id

    def contains(self, session_id: CallbackSessionID) -> bool:
        return session_id in self._storage

    def get(self, handler: ConversationHandler, session_id: CallbackSessionID) -> Optional[ChatListStorage]:
        conversations = getattr(handler, "_conversations", None)
        if conversations is None:
            conversations = getattr(handler, "conversations")
        if session_id not in cast(ConversationDict, conversations):
            return None
        return self.lookup(session_id)

    def end(self, handler: ConversationHandler, session_id: CallbackSessionID, callback_query_id: str, text: str) -> int:
        self._storage.pop(session_id, None)
        self.clear_state(handler, session_id)
        self._bot.edit_message_text(text=text, chat_id=session_id[0], message_id=session_id[1])
        self._bot.answer_callback_query(callback_query_id)
        return ConversationHandler.END

    def expired(self, handler: ConversationHandler, session_id: CallbackSessionID, callback_query_id: str, text: str) -> Optional[ChatListStorage]:
        storage = self.get(handler, session_id)
        if storage is None:
            self.end(handler, session_id, callback_query_id, text)
        return storage

    def clear(self, handler: ConversationHandler, session_id: CallbackSessionID) -> None:
        self.discard(session_id)
        self.clear_state(handler, session_id)

    def discard(self, session_id: CallbackSessionID) -> None:
        self._storage.pop(session_id, None)

    def is_current_selection(self, storage: ChatListStorage, index: int) -> bool:
        per_page = self._chats_per_page()
        return storage.offset <= index < min(storage.offset + per_page, storage.length)

    def is_valid_page_offset(self, storage: ChatListStorage, offset: int) -> bool:
        per_page = self._chats_per_page()
        return 0 <= offset < storage.length and offset % per_page == 0
