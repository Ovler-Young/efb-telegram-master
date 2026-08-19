from typing import TYPE_CHECKING, Any, Dict, Optional

from ehforwarderbot import Middleware
from ehforwarderbot.channel import SlaveChannel
from ehforwarderbot.chat import ChatNotificationState, GroupChat, PrivateChat, SystemChat
from ehforwarderbot.types import ChatID, ModuleID

from efb_telegram_master.chat.chat import ETMChatMixin
from efb_telegram_master.chat.chat_member import ETMChatMember, ETMSystemChatMember
from efb_telegram_master.core.constants import Emoji

if TYPE_CHECKING:
    from efb_telegram_master.core.db import DatabaseManager


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
