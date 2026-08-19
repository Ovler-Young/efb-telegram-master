# coding=utf-8

import datetime
from typing import Collection, Dict, Tuple

from ehforwarderbot.message import MessageAttribute, MessageCommands
from ehforwarderbot.types import ReactionName
from peewee import SQL, AutoField, BlobField, BooleanField, CharField, DatabaseProxy, DateTimeField, IntegerField, Model, TextField
from typing_extensions import TypedDict

from .utils import EFBChannelChatIDStr, TgChatMsgIDStr

database = DatabaseProxy()
UTC_LEASE_CLOCK = "utc"


def utc_now_naive() -> datetime.datetime:
    """Return the current UTC time in the MsgLog storage representation."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


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

    class Meta:
        indexes = ((("slave_uid",), True), (("topic_chat_id", "message_thread_id"), True))


class ChatAssoc(BaseModel):
    master_uid = TextField()
    slave_uid = TextField()

    class Meta:
        indexes = ((("slave_uid",), True),)


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
    time = DateTimeField(default=utc_now_naive, null=True)
    """Time of the message sent."""

    class Meta:
        indexes = ((("slave_origin_uid", "time", "master_msg_id"), False),)


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
    rescan_requested = BooleanField(default=False)
    lease_owner = TextField(null=True)
    lease_expires_at = DateTimeField(null=True)
    lease_clock = TextField(null=True, default=UTC_LEASE_CLOCK, constraints=[SQL("DEFAULT 'utc'")])
    status = TextField(default="pending")
    error = TextField(null=True)
    created_at = DateTimeField(default=utc_now_naive)
    updated_at = DateTimeField(default=utc_now_naive)


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


HistoryMigrationEntry.add_index(
    HistoryMigrationEntry.index(
        HistoryMigrationEntry.slave_chat_id,
        HistoryMigrationEntry.target_chat_id,
        HistoryMigrationEntry.position,
        unique=True,
        where=HistoryMigrationEntry.message_thread_id.is_null(True),
        name="historymigrationentry_target_position_without_thread_unique",
    )
)
HistoryMigrationEntry.add_index(
    HistoryMigrationEntry.index(
        HistoryMigrationEntry.slave_chat_id,
        HistoryMigrationEntry.target_chat_id,
        HistoryMigrationEntry.message_thread_id,
        HistoryMigrationEntry.position,
        unique=True,
        where=HistoryMigrationEntry.message_thread_id.is_null(False),
        name="historymigrationentry_target_position_with_thread_unique",
    )
)


class SlaveChatInfo(BaseModel):
    slave_channel_id = TextField()
    slave_channel_emoji = CharField()
    slave_chat_uid = TextField()
    slave_chat_group_id = TextField(null=True)
    slave_chat_name = TextField()
    slave_chat_alias = TextField(null=True)
    slave_chat_type = CharField()
    pickle = BlobField(null=True)


class SlaveMessageDelivery(BaseModel):
    slave_origin_uid = TextField()
    slave_message_id = TextField()
    state = TextField(default="pending", constraints=[SQL("DEFAULT 'pending'")])
    lease_expires_at = DateTimeField(null=True)
    lease_clock = TextField(null=True, default=UTC_LEASE_CLOCK, constraints=[SQL("DEFAULT 'utc'")])
    owner_token = TextField(null=True)

    class Meta:
        indexes = ((("slave_origin_uid", "slave_message_id"), True),)


SlaveChatInfo.add_index(
    SlaveChatInfo.index(
        SlaveChatInfo.slave_channel_id,
        SlaveChatInfo.slave_chat_uid,
        unique=True,
        where=SlaveChatInfo.slave_chat_group_id.is_null(True),
        name="slavechatinfo_identity_without_group_unique",
    )
)


DATABASE_MODELS = (
    ChatAssoc,
    MsgLog,
    SlaveChatInfo,
    TopicAssoc,
    HistoryMigrationEntry,
    MsgLogIngestionScan,
    SlaveMessageDelivery,
)


def bind_models_to_proxy() -> None:
    for model in DATABASE_MODELS:
        model._meta.set_database(database)


SlaveChatInfo.add_index(
    SlaveChatInfo.index(
        SlaveChatInfo.slave_channel_id,
        SlaveChatInfo.slave_chat_uid,
        SlaveChatInfo.slave_chat_group_id,
        unique=True,
        where=SlaveChatInfo.slave_chat_group_id.is_null(False),
        name="slavechatinfo_identity_with_group_unique",
    )
)
