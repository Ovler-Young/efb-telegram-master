import io
import pickle
from typing import TYPE_CHECKING, Union, overload

from ehforwarderbot.chat import Chat, ChatMember, GroupChat, PrivateChat, SelfChatMember, SystemChat, SystemChatMember

from efb_telegram_master.chat.chat import ETMChatMixin
from efb_telegram_master.chat.chat_member import ETMBaseChatMixin, ETMChatMember, ETMSelfChatMember, ETMSystemChatMember
from efb_telegram_master.chat.chat_types import ETMGroupChat, ETMPrivateChat, ETMSystemChat

if TYPE_CHECKING:
    from efb_telegram_master.core.db import DatabaseManager


@overload
def convert_chat(db: "DatabaseManager", chat: PrivateChat) -> ETMPrivateChat: ...


@overload
def convert_chat(db: "DatabaseManager", chat: GroupChat) -> ETMGroupChat: ...


@overload
def convert_chat(db: "DatabaseManager", chat: SystemChat) -> ETMSystemChat: ...


@overload
def convert_chat(db: "DatabaseManager", chat: Chat) -> ETMChatMixin: ...


def convert_chat(db: "DatabaseManager", chat: Chat) -> ETMChatMixin:
    """Convert an EFB chat object to an ETM extended version."""
    if isinstance(chat, ETMChatMixin):
        return chat
    etm_chat: Union[ETMPrivateChat, ETMSystemChat, ETMGroupChat]
    if isinstance(chat, PrivateChat):
        etm_chat = ETMPrivateChat(
            db,
            module_id=chat.module_id,
            module_name=chat.module_name,
            channel_emoji=chat.channel_emoji,
            name=chat.name,
            alias=chat.alias,
            uid=chat.uid,
            vendor_specific=chat.vendor_specific.copy(),
            description=chat.description,
            notification=chat.notification,
            with_self=chat.has_self,
            other_is_self=chat.other is chat.self,
        )
        assert isinstance(etm_chat, ETMPrivateChat)
        if chat.self and etm_chat.self:
            copy_member(chat.self, etm_chat.self)
        if chat.self is not chat.other and chat.other and etm_chat.other:
            copy_member(chat.other, etm_chat.other)
        return etm_chat
    if isinstance(chat, SystemChat):
        etm_chat = ETMSystemChat(
            db,
            module_id=chat.module_id,
            module_name=chat.module_name,
            channel_emoji=chat.channel_emoji,
            name=chat.name,
            alias=chat.alias,
            uid=chat.uid,
            vendor_specific=chat.vendor_specific.copy(),
            description=chat.description,
            notification=chat.notification,
            with_self=chat.has_self,
        )
        assert isinstance(etm_chat, ETMSystemChat)
        if chat.self and etm_chat.self:
            copy_member(chat.self, etm_chat.self)
        if chat.other and etm_chat.other:
            copy_member(chat.other, etm_chat.other)
        return etm_chat
    if isinstance(chat, GroupChat):
        etm_chat = ETMGroupChat(
            db,
            module_id=chat.module_id,
            module_name=chat.module_name,
            channel_emoji=chat.channel_emoji,
            name=chat.name,
            alias=chat.alias,
            uid=chat.uid,
            vendor_specific=chat.vendor_specific.copy(),
            description=chat.description,
            notification=chat.notification,
            with_self=False,
        )
        assert isinstance(etm_chat, ETMGroupChat)
        for member in chat.members:
            if isinstance(member, ETMChatMember):
                etm_chat.members.append(member)
            elif isinstance(member, SystemChatMember):
                etm_chat.add_system_member(name=member.name, alias=member.alias, uid=member.uid, description=member.description, vendor_specific=member.vendor_specific.copy())
            elif isinstance(member, SelfChatMember):
                etm_chat.self = ETMSelfChatMember(db, etm_chat, name=member.name, alias=member.alias, uid=member.uid, description=member.description, vendor_specific=member.vendor_specific.copy())
                etm_chat.members.append(etm_chat.self)
            else:
                etm_chat.add_member(name=member.name, alias=member.alias, uid=member.uid, description=member.description, vendor_specific=member.vendor_specific.copy())
        return etm_chat
    raise TypeError(f"Chat type unknown: {type(chat)}, {chat!r}")


def copy_member(source: ChatMember, dest: ETMChatMember):
    """Copy values from source object to destination object."""
    dest.name = source.name
    dest.alias = source.alias
    dest.uid = source.uid
    dest.vendor_specific = source.vendor_specific.copy()
    dest.module_id = source.module_id
    dest.module_name = source.module_name
    dest.channel_emoji = source.channel_emoji
    dest.description = source.description


def pickle_chat(chat: ETMChatMixin) -> bytes:
    return pickle.dumps(chat)


def unpickle(data: bytes, db: "DatabaseManager") -> ETMChatMixin:
    if isinstance(data, memoryview):
        data = bytes(data)
    obj = _ChatUnpickler(io.BytesIO(data)).load()
    obj.db = db
    obj.chat_associations = db.chat_associations
    obj.slave_chat_info = db.slave_chat_info
    return obj


class _ChatUnpickler(pickle.Unpickler):
    _legacy_classes: dict[str, dict[str, type[object]]] = {
        "efb_telegram_master.chat": {
            "ETMPrivateChat": ETMPrivateChat,
            "ETMSystemChat": ETMSystemChat,
            "ETMGroupChat": ETMGroupChat,
        },
        "efb_telegram_master.chat_types": {
            "ETMPrivateChat": ETMPrivateChat,
            "ETMSystemChat": ETMSystemChat,
            "ETMGroupChat": ETMGroupChat,
        },
        "efb_telegram_master.chat_member": {
            "ETMBaseChatMixin": ETMBaseChatMixin,
            "ETMChatMember": ETMChatMember,
            "ETMSelfChatMember": ETMSelfChatMember,
            "ETMSystemChatMember": ETMSystemChatMember,
        },
    }

    def find_class(self, module: str, name: str):
        legacy_class = self._legacy_classes.get(module, {}).get(name)
        if legacy_class is not None:
            return legacy_class
        return super().find_class(module, name)
