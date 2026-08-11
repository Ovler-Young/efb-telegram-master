import time
from abc import ABC
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, MutableSequence, Optional, Pattern, Union

from ehforwarderbot import Middleware
from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.chat import Chat, ChatNotificationState, GroupChat, PrivateChat, SelfChatMember, SystemChat
from ehforwarderbot.types import ChatID, ModuleID

from . import utils
from .chat_member import ETMBaseChatMixin, ETMChatMember, ETMSelfChatMember, ETMSystemChatMember
from .constants import Emoji
from .utils import EFBChannelChatIDStr

if TYPE_CHECKING:
    from .db import DatabaseManager

__all__ = ["ETMChatMixin", "ETMPrivateChat", "ETMSystemChat", "ETMGroupChat"]


class ETMChatMixin(ETMBaseChatMixin, Chat, ABC):
    _last_message_time: Optional[datetime] = None
    _last_message_time_query: float = 0
    LAST_MESSAGE_QUERY_TIMEOUT_MS: float = 60000  # 60s

    _linked: Optional[List[EFBChannelChatIDStr]] = None

    members: MutableSequence[ETMChatMember]  # type: ignore
    self: Optional[ETMSelfChatMember]

    chat_type_name = "Chat"
    chat_type_emoji = Emoji.UNKNOWN

    def match(self, pattern: Union[Pattern, str, None]) -> bool:
        """
        Match the chat against a compiled regex pattern or string
        with a string in the following format::

            Channel: <Channel name>
            Name: <Chat name>
            Alias: <Chat Alias>
            ID: <Chat Unique ID>
            Type: (User|Group)
            Mode: [Linked]
            Description: <Description>
            Notification: (ALL|MENTION|NONE)
            Other: <Python Dictionary String>

        If a string is provided instead of compiled regular expression pattern,
        simple string match is used instead.

        String match is about 5x faster than re.search when there’s no
        significance of regex used.
        Ref: https://etm.1a23.studio/pull/77

        Args:
            pattern: Regex pattern or string to look for

        Returns:
            If the pattern is found in the generated string.
        """
        if pattern is None:
            return True
        mode = []
        if self.linked:
            mode.append("Linked")
        mode_str = ", ".join(mode)
        entry_string = (
            f"Channel: {self.module_name}\n"
            f"Channel ID: {self.module_id}\n"
            f"Name: {self.name}\n"
            f"Alias: {self.alias}\n"
            f"ID: {self.uid}\n"
            f"Type: {self.chat_type_name}\n"
            f"Mode: {mode_str}\n"
            f"Description: {self.description}\n"
            f"Notification: {self.notification.name}\n"
            f"Other: {self.vendor_specific}"
        )
        if isinstance(pattern, str):
            return pattern.lower() in entry_string.lower()
        else:  # pattern is re.Pattern
            return bool(pattern.search(entry_string))

    def unlink(self):
        """Unlink this chat from any Telegram group."""
        self.db.remove_chat_assoc(slave_uid=utils.chat_id_to_str(self.module_id, self.uid))
        self._update_linked()

    def link(self, channel_id: ModuleID, chat_id: ChatID, multiple_slave: bool):
        self.db.add_chat_assoc(master_uid=utils.chat_id_to_str(channel_id, chat_id), slave_uid=utils.chat_id_to_str(self.module_id, self.uid), multiple_slave=multiple_slave)
        self._update_linked()

    @property
    def linked(self) -> List[EFBChannelChatIDStr]:
        if self._linked is None:
            self._update_linked()
        return self._linked or []

    def _update_linked(self):
        self._linked = self.db.get_chat_assoc(slave_uid=utils.chat_id_to_str(self.module_id, self.uid))

    @property
    def full_name(self) -> str:
        """Chat name with channel name and emoji"""
        chat_long_name = self.long_name
        if self.module_name:
            instance_id_idx = self.module_id.find("#")
            if instance_id_idx >= 0:
                instance_id = self.module_id[instance_id_idx + 1 :]
                return f"‘{chat_long_name}’ @ ‘{self.channel_emoji} {self.module_name} ({instance_id})’"
            else:
                return f"‘{chat_long_name}’ @ ‘{self.channel_emoji} {self.module_name}’"
        else:
            return f"‘{chat_long_name}’ @ ‘{self.module_id}’"

    @property
    def chat_title(self) -> str:
        """Chat title used in updating title for Telegram group.

        Shows only alias if available.

        An asterisk (*) is added to the beginning if the channel is not
        running on its default instance.
        """
        non_default_instance_flag = "*" if "#" in self.module_id else ""
        return f"{non_default_instance_flag}{self.channel_emoji}{self.chat_type_emoji} {self.display_name}"

    @property
    def last_message_time(self) -> datetime:
        """Time of the last recorded message from this chat.
        Returns ``datetime.min`` when no recorded message is found.
        """
        now = time.time()
        if self._last_message_time is None or now - self._last_message_time_query > self.LAST_MESSAGE_QUERY_TIMEOUT_MS / 1000:
            msg_log = self.db.get_last_message(slave_chat_id=utils.chat_id_to_str(chat=self))
            self._last_message_time_query = now
            if msg_log is None:
                self._last_message_time = datetime.min
            else:
                self._last_message_time = msg_log.time
        assert self._last_message_time
        return self._last_message_time

    def update_to_db(self):
        """Update this object to database."""
        self.db.set_slave_chat_info(self)

    @property
    def pickle(self) -> bytes:
        from .chat_codec import pickle_chat

        return pickle_chat(self)

    def remove_from_db(self):
        super().remove_from_db()
        for i in self.members:
            self.db.delete_slave_chat_info(self.module_id, i.uid, self.uid)

    def add_self(self) -> ETMSelfChatMember:
        if getattr(self, "self", None) and isinstance(self.self, ETMSelfChatMember):
            return self.self
        assert not any(isinstance(i, SelfChatMember) for i in self.members)
        s = ETMSelfChatMember(self.db, self)
        self.members.append(s)
        return s

    def add_member(  # type: ignore[override]
        self,
        name: str,
        uid: ChatID,
        alias: Optional[str] = None,  # type: ignore
        vendor_specific: Optional[Dict[str, Any]] = None,
        id="",
        description: str = "",
        middleware: Optional[Middleware] = None,
    ) -> ETMChatMember:
        # TODO: remove deprecated ID
        assert not id, f"id is {id!r}"
        member = ETMChatMember(self.db, self, name=name, alias=alias, uid=uid, vendor_specific=vendor_specific, description=description, middleware=middleware)
        self.members.append(member)
        return member

    # type: ignore
    def add_system_member(  # type: ignore[override]
        self,
        name: str = "",
        alias: Optional[str] = None,
        uid: ChatID = ChatID(""),  # type: ignore
        vendor_specific: Optional[Dict[str, Any]] = None,
        description: str = "",
        id="",
        middleware: Optional[Middleware] = None,
    ) -> ETMSystemChatMember:
        # TODO: remove deprecated ID
        assert not id, f"id is {id!r}"
        member = self.make_system_member(name=name, alias=alias, uid=uid, vendor_specific=vendor_specific, description=description, middleware=middleware)
        self.members.append(member)
        return member

    def make_system_member(
        self,
        name: str = "",
        alias: Optional[str] = None,
        id: ChatID = ChatID(""),
        uid: ChatID = ChatID(""),
        vendor_specific: Optional[Dict[str, Any]] = None,
        description: str = "",
        middleware: Optional[Middleware] = None,
    ) -> ETMSystemChatMember:
        # TODO: remove deprecated ID
        assert not id, f"id is {id!r}"
        return ETMSystemChatMember(self.db, self, name=name, alias=alias, uid=uid, vendor_specific=vendor_specific, description=description, middleware=middleware)

    def get_member(self, member_id: ChatID) -> ETMChatMember:
        return super().get_member(member_id)  # type: ignore


class ETMPrivateChat(ETMChatMixin, PrivateChat):
    chat_type_name = "Private"
    chat_type_emoji = Emoji.USER

    other: ETMChatMember

    def __init__(
        self,
        db: "DatabaseManager",
        *,
        channel: Optional[SlaveChannel] = None,
        middleware: Optional[Middleware] = None,
        module_name: str = "",
        channel_emoji: str = "",
        module_id: ModuleID = ModuleID(""),
        name: str = "",
        alias: Optional[str] = None,
        uid: ChatID = ChatID(""),
        vendor_specific: Optional[Dict[str, Any]] = None,
        description: str = "",
        notification: ChatNotificationState = ChatNotificationState.ALL,
        with_self: bool = True,
        other_is_self: bool = False,
    ):
        super().__init__(
            db,
            channel=channel,
            middleware=middleware,
            module_name=module_name,
            channel_emoji=channel_emoji,
            module_id=module_id,
            name=name,
            alias=alias,
            uid=uid,
            vendor_specific=vendor_specific,
            description=description,
            notification=notification,
            with_self=with_self,
            other_is_self=other_is_self,
        )


class ETMSystemChat(ETMChatMixin, SystemChat):
    chat_type_name = "System"
    chat_type_emoji = Emoji.SYSTEM

    other: ETMSystemChatMember

    def __init__(
        self,
        db: "DatabaseManager",
        *,
        channel: Optional[SlaveChannel] = None,
        middleware: Optional[Middleware] = None,
        module_name: str = "",
        channel_emoji: str = "",
        module_id: ModuleID = ModuleID(""),
        name: str = "",
        alias: Optional[str] = None,
        uid: ChatID = ChatID(""),
        vendor_specific: Optional[Dict[str, Any]] = None,
        description: str = "",
        notification: ChatNotificationState = ChatNotificationState.ALL,
        with_self: bool = True,
    ):
        super().__init__(
            db,
            channel=channel,
            middleware=middleware,
            module_name=module_name,
            channel_emoji=channel_emoji,
            module_id=module_id,
            name=name,
            alias=alias,
            uid=uid,
            vendor_specific=vendor_specific,
            description=description,
            notification=notification,
            with_self=with_self,
        )


class ETMGroupChat(ETMChatMixin, GroupChat):
    chat_type_name = "Group"
    chat_type_emoji = Emoji.GROUP

    def __init__(
        self,
        db: "DatabaseManager",
        *,
        channel: Optional[SlaveChannel] = None,
        middleware: Optional[Middleware] = None,
        module_name: str = "",
        channel_emoji: str = "",
        module_id: ModuleID = ModuleID(""),
        name: str = "",
        alias: Optional[str] = None,
        uid: ChatID = ChatID(""),
        vendor_specific: Optional[Dict[str, Any]] = None,
        description: str = "",
        notification: ChatNotificationState = ChatNotificationState.ALL,
        with_self: bool = True,
    ):
        super().__init__(
            db,
            channel=channel,
            middleware=middleware,
            module_name=module_name,
            channel_emoji=channel_emoji,
            module_id=module_id,
            name=name,
            alias=alias,
            uid=uid,
            vendor_specific=vendor_specific,
            description=description,
            notification=notification,
            with_self=with_self,
        )
