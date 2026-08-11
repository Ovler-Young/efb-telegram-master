# coding=utf-8

import datetime
import pickle
from contextlib import suppress
from typing import TYPE_CHECKING, Collection, Dict, List, Tuple

from ehforwarderbot import Channel, MsgType, coordinator
from ehforwarderbot.message import MessageAttribute, MessageCommands, Substitutions
from ehforwarderbot.types import MessageID, ModuleID, ReactionName
from peewee import SQL, AutoField, BlobField, CharField, DatabaseProxy, DateTimeField, IntegerField, Model, TextField
from typing_extensions import TypedDict

from .chat_object_cache import ChatObjectCacheManager
from .message import ETMMsg
from .msg_type import TGMsgType
from .utils import EFBChannelChatIDStr, TgChatMsgIDStr, chat_id_str_to_id

if TYPE_CHECKING:
    from .chat import ETMChatMixin
    from .chat_member import ETMChatMember

database = DatabaseProxy()

PickledDict = TypedDict(
    "PickledDict",
    {
        "target": TgChatMsgIDStr,
        "is_system": bool,
        "attributes": MessageAttribute,
        "commands": MessageCommands,
        "substitutions": Dict[Tuple[int, int], EFBChannelChatIDStr],
        "reactions": Dict[ReactionName, Collection[EFBChannelChatIDStr]],
    },
    total=False,
)
"""
Dict entries for ``pickle`` field of ``msglog`` log.

- ``target``: ``master_msg_id`` of the target message
- ``is_system``
- ``attributes``
- ``commands``
- ``substitutions``: ``Dict[Tuple[int, int], SlaveChatID]``
- ``reactions``: ``Dict[str, Collection[SlaveChatID]]``
"""


class BaseModel(Model):
    class Meta:
        database = database


class TopicAssoc(BaseModel):
    id = AutoField()
    topic_chat_id = TextField()
    message_thread_id = TextField()
    slave_uid = TextField()


class ChatAssoc(BaseModel):
    master_uid = TextField()
    slave_uid = TextField()


class MsgLog(BaseModel):
    master_msg_id = TextField(unique=True, primary_key=True)
    """Message ID from Telegram."""
    master_msg_id_alt = TextField(null=True)
    """Editable message ID from Telegram if ``master_msg_id`` is not editable
    and a separate one is sent.
    """
    slave_message_id = TextField()
    """Message from slave channel."""
    text = TextField()
    """Text in the message."""
    slave_origin_uid = TextField()
    """Channel + chat ID of chat the message is sent to."""
    slave_origin_display_name = TextField(null=True)
    """Deprecated."""
    slave_member_uid = TextField(null=True)
    """Module + chat ID of the user that sent the message in slave channel.
    Can be ``blueset.telegram __self__``."""
    slave_member_display_name = TextField(null=True)
    """Deprecated."""
    media_type = TextField(null=True)
    """Message type in Telegram."""
    mime = TextField(null=True)
    """MIME type of attachment."""
    file_id = TextField(null=True)
    """File ID of attachment in Telegram."""
    file_unique_id = TextField(null=True)
    """Unique file ID of attachment in Telegram."""
    msg_type = TextField()
    """Message type in EFB framework."""
    pickle = BlobField(null=True)
    """Miscellaneous data serialized with ``pickle``, per spec in
    ``MsgLogRepository.pickle_misc_msg()``.
    """
    sent_to = TextField()
    """Module ID of the message sent to."""
    sender_bot_id = TextField(null=True)
    """Telegram bot user ID that sent this message. NULL means the main bot."""
    provenance = TextField(default="live", constraints=[SQL("DEFAULT 'live'")])
    """Origin of this record: ``live`` or ``mtproto_ingested``."""
    time = DateTimeField(default=datetime.datetime.now, null=True)
    """Time of the message sent."""

    def build_etm_msg(self, chat_manager: ChatObjectCacheManager, recur: bool = True) -> ETMMsg:
        c_module, c_id, _ = chat_id_str_to_id(EFBChannelChatIDStr(self.slave_origin_uid))
        assert self.slave_member_uid is not None
        a_module, a_id, a_grp = chat_id_str_to_id(EFBChannelChatIDStr(self.slave_member_uid))
        chat: "ETMChatMixin" = chat_manager.get_chat(c_module, c_id, build_dummy=True)
        author: "ETMChatMember" = chat_manager.get_chat_member(a_module, a_grp, a_id, build_dummy=True)  # type: ignore
        msg = ETMMsg(
            uid=MessageID(self.slave_message_id),
            chat=chat,
            author=author,
            text=self.text,
            type=MsgType(self.msg_type),
            type_telegram=TGMsgType(self.media_type),
            mime=self.mime or None,
            file_id=self.file_id or None,
        )
        msg.sender_bot_id = self.sender_bot_id
        with suppress(NameError):
            to_module = coordinator.get_module_by_id(ModuleID(self.sent_to))
            if isinstance(to_module, Channel):
                msg.deliver_to = to_module
        if self.pickle:
            pickle_data = bytes(self.pickle) if isinstance(self.pickle, memoryview) else self.pickle
            misc_data: PickledDict = pickle.loads(pickle_data)
            if "target" in misc_data and recur:
                target_row = self.get_or_none(MsgLog.master_msg_id == misc_data["target"])
                if target_row:
                    msg.target = target_row.build_etm_msg(chat_manager, recur=False)
            if "is_system" in misc_data:
                msg.is_system = misc_data["is_system"]
            if "attributes" in misc_data:
                msg.attributes = misc_data["attributes"]
            if "commands" in misc_data:
                msg.commands = misc_data["commands"]
            if "substitutions" in misc_data:
                subs = Substitutions({})
                for sk, sv in misc_data["substitutions"].items():
                    module_id, chat_id, group_id = chat_id_str_to_id(sv)
                    if group_id:
                        subs[sk] = chat_manager.get_chat_member(module_id, group_id, chat_id, build_dummy=True)
                    else:
                        subs[sk] = chat_manager.get_chat(module_id, chat_id, build_dummy=True)
                msg.substitutions = subs
            if "reactions" in misc_data:
                reactions: Dict[ReactionName, List[ETMChatMember]] = {}
                for rk, rv in misc_data["reactions"].items():
                    reactions[rk] = []
                    for idx in rv:
                        module_id, chat_id, group_id = chat_id_str_to_id(idx)
                        reactions[rk].append(chat_manager.get_chat_member(module_id, group_id, chat_id, build_dummy=True))  # type: ignore
                msg.reactions = reactions
        return msg


class MsgLogIngestionScan(BaseModel):
    """One leased descending MTProto scan for a bound Telegram group."""

    id = AutoField()
    source_chat_id = TextField(unique=True)
    scan_boundary = IntegerField()
    cursor = IntegerField()
    existing_streak = IntegerField(default=0)
    scanned_count = IntegerField(default=0)
    inserted_count = IntegerField(default=0)
    existing_count = IntegerField(default=0)
    skipped_count = IntegerField(default=0)
    lease_owner = TextField(null=True)
    lease_expires_at = DateTimeField(null=True)
    status = TextField(default="pending")
    error = TextField(null=True)
    created_at = DateTimeField(default=datetime.datetime.now)
    updated_at = DateTimeField(default=datetime.datetime.now)


class MsgLogIngestionLeaseLostError(RuntimeError):
    """Raised when a worker no longer owns an active ingestion lease."""


class HistoryMigrationEntry(BaseModel):
    id = AutoField()
    slave_chat_id = TextField()
    target_chat_id = TextField()
    message_thread_id = TextField(null=True)
    source_master_msg_id = TextField()
    formatted_text = TextField(null=True)
    media_type = TextField(null=True)
    source_time = DateTimeField(null=True)
    position = IntegerField()
    created_at = DateTimeField(default=datetime.datetime.now)

    class Meta:
        indexes = ((("slave_chat_id", "target_chat_id", "message_thread_id", "position"), False),)


class SlaveChatInfo(BaseModel):
    slave_channel_id = TextField()
    slave_channel_emoji = CharField()
    slave_chat_uid = TextField()
    slave_chat_group_id = TextField(null=True)
    slave_chat_name = TextField()
    slave_chat_alias = TextField(null=True)
    slave_chat_type = CharField()
    pickle = BlobField(null=True)
