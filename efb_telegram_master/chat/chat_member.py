import copy
from abc import ABC
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Dict, Optional, TypeVar

from ehforwarderbot import Middleware, coordinator
from ehforwarderbot.chat import BaseChat, Chat, ChatMember, SelfChatMember, SystemChatMember
from ehforwarderbot.types import ChatID

if TYPE_CHECKING:
    from efb_telegram_master.core.db import DatabaseManager


class ETMBaseChatMixin(BaseChat, ABC):  # lgtm [py/missing-equals]
    # Allow mypy to recognize subclass output for `return self` methods.
    _Self = TypeVar("_Self", bound="ETMBaseChatMixin")
    chat_type_name = "BaseChat"

    # noinspection PyMissingConstructor
    def __init__(self, db: "DatabaseManager", *args, **kwargs):
        self.db = db
        self.chat_associations = db.chat_associations
        self.slave_chat_info = db.slave_chat_info
        super().__init__(*args, **kwargs)

    def remove_from_db(self):
        """Remove this chat from database."""
        self.slave_chat_info.delete_slave_chat_info(self.module_id, self.uid)

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        for dependency in ("db", "chat_associations", "slave_chat_info"):
            state.pop(dependency, None)
        return state

    def __setstate__(self, state: Dict[str, Any]):
        from efb_telegram_master import TelegramChannel

        # Import inline to prevent cyclic import
        self.__dict__.update(state)
        with suppress(NameError, AttributeError):
            if isinstance(coordinator.master, TelegramChannel):
                self.db = coordinator.master.db
                self.chat_associations = coordinator.master.chat_associations
                self.slave_chat_info = coordinator.master.slave_chat_info

    def __copy__(self):
        rv = self.__reduce_ex__(4)
        if isinstance(rv, str):
            return self
        obj = copy._reconstruct(self, None, *rv)
        obj.db = self.db
        obj.chat_associations = self.chat_associations
        obj.slave_chat_info = self.slave_chat_info
        return obj


class ETMChatMember(ETMBaseChatMixin, ChatMember):
    chat_type_name = "ChatMember"

    def __init__(
        self,
        db: "DatabaseManager",
        chat: "Chat",
        *,
        name: str = "",
        alias: Optional[str] = None,
        uid: ChatID = ChatID(""),
        vendor_specific: Optional[Dict[str, Any]] = None,
        description: str = "",
        middleware: Optional[Middleware] = None,
    ):
        super().__init__(db, chat, name=name, alias=alias, uid=uid, vendor_specific=vendor_specific, description=description, middleware=middleware)


class ETMSelfChatMember(ETMChatMember, SelfChatMember):
    chat_type_name = "SelfChatMember"


class ETMSystemChatMember(ETMChatMember, SystemChatMember):
    chat_type_name = "SystemChatMember"
