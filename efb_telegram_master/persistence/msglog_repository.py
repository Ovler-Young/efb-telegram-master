import logging
import pickle
import time
from datetime import datetime
from typing import List, Optional, Tuple

from ehforwarderbot import Message as EFBMessage
from ehforwarderbot.types import MessageID
from peewee import DoesNotExist, PostgresqlDatabase, fn
from telegram import Message

from ..message import ETMMsg
from ..models import MsgLog, PickledDict
from ..utils import EFBChannelChatIDStr, OldMsgID, TelegramChatID, TelegramMessageID, TgChatMsgIDStr, chat_id_to_str, message_id_to_str
from .database_observability import ObservedRepository, observe_database_method


class MsgLogRepository(ObservedRepository):
    FAIL_FLAG = "__fail__"
    logger = logging.getLogger(__name__)

    def __init__(self, database=None) -> None:
        super().__init__(database)

    @observe_database_method("get_master_msg_id")
    def get_master_msg_id(self, message: EFBMessage) -> Optional[TgChatMsgIDStr]:
        log: Optional[MsgLog] = MsgLog.get_or_none(MsgLog.slave_origin_uid == chat_id_to_str(chat=message.chat), MsgLog.slave_message_id == message.uid)
        return TgChatMsgIDStr(log.master_msg_id) if log else None

    def pickle_misc_msg(self, message: EFBMessage) -> Optional[bytes]:
        data: PickledDict = {}
        if message.is_system:
            data["is_system"] = message.is_system
        if message.attributes:
            data["attributes"] = message.attributes
        if message.commands:
            data["commands"] = message.commands
        if message.substitutions:
            data["substitutions"] = {key: chat_id_to_str(chat=value) for key, value in message.substitutions.items()}
        if message.reactions:
            data["reactions"] = {key: tuple(chat_id_to_str(chat=value) for value in values) for key, values in message.reactions.items()}
        if message.target:
            target_id = self.get_master_msg_id(message.target)
            if target_id:
                data["target"] = target_id
        return pickle.dumps(data) if data else None

    @observe_database_method("add_or_update_message_log")
    def add_or_update_message_log(self, msg: ETMMsg, master_message: Message, old_message_id: Optional[OldMsgID] = None, sender_bot_id: Optional[str] = None) -> None:
        sent_message_id = message_id_to_str(TelegramChatID(master_message.chat_id), TelegramMessageID(master_message.message_id))
        master_msg_id = sent_message_id
        master_msg_id_alt = None
        self.logger.debug("[%s] Received message logging request of %s", master_msg_id, msg.uid)

        row: Optional[MsgLog] = None
        if old_message_id is not None:
            old_message_id_str = message_id_to_str(*old_message_id)
            row = MsgLog.get_or_none((MsgLog.master_msg_id == old_message_id_str) | (MsgLog.master_msg_id_alt == old_message_id_str))
            if row is not None:
                master_msg_id = TgChatMsgIDStr(row.master_msg_id)
                master_msg_id_alt = sent_message_id if sent_message_id != master_msg_id else row.master_msg_id_alt
            elif sent_message_id != old_message_id_str:
                self.logger.debug("[%s] Message has an old ID: %s", sent_message_id, old_message_id_str)
                master_msg_id, master_msg_id_alt = old_message_id_str, sent_message_id

        if row is None:
            row = MsgLog.get_or_none(MsgLog.master_msg_id == master_msg_id)
        if row is not None:
            self.logger.debug("[%s] Message record is found in database, update it", master_msg_id)
        else:
            row = MsgLog()
            self.logger.debug("[%s] Message record is not found in database, insert it", master_msg_id)

        row.master_msg_id = master_msg_id
        row.master_msg_id_alt = master_msg_id_alt
        row.text = msg.text
        row.slave_origin_uid = chat_id_to_str(chat=msg.chat)
        row.slave_member_uid = chat_id_to_str(chat=msg.author)
        row.msg_type = msg.type.name
        row.sent_to = msg.deliver_to.channel_id
        row.slave_message_id = msg.uid or f"{self.FAIL_FLAG}.{time.time()}"
        row.media_type = msg.type_telegram.value
        row.file_id = msg.file_id
        row.file_unique_id = msg.file_unique_id
        row.mime = msg.mime
        row.sender_bot_id = sender_bot_id or getattr(msg, "sender_bot_id", None)
        row.provenance = "live"
        row.pickle = self.pickle_misc_msg(msg)
        fields = {
            MsgLog.master_msg_id_alt: row.master_msg_id_alt,
            MsgLog.text: row.text,
            MsgLog.slave_origin_uid: row.slave_origin_uid,
            MsgLog.slave_member_uid: row.slave_member_uid,
            MsgLog.msg_type: row.msg_type,
            MsgLog.sent_to: row.sent_to,
            MsgLog.slave_message_id: row.slave_message_id,
            MsgLog.media_type: row.media_type,
            MsgLog.file_id: row.file_id,
            MsgLog.file_unique_id: row.file_unique_id,
            MsgLog.mime: row.mime,
            MsgLog.sender_bot_id: row.sender_bot_id,
            MsgLog.provenance: row.provenance,
            MsgLog.pickle: row.pickle,
        }
        result = MsgLog.insert(row.__data__).on_conflict(conflict_target=[MsgLog.master_msg_id], update=fields).execute()
        self.logger.debug("[%s] Database insert/update outcome: %s", master_msg_id, result)

    @observe_database_method("get_msg_log")
    def get_msg_log(self, master_msg_id: Optional[TgChatMsgIDStr] = None, slave_msg_id: Optional[MessageID] = None, slave_origin_uid: Optional[EFBChannelChatIDStr] = None) -> Optional[MsgLog]:
        if (master_msg_id and (slave_msg_id or slave_origin_uid)) or not (master_msg_id or (slave_msg_id or slave_origin_uid)):
            raise ValueError("master_msg_id and slave_msg_id is mutual exclusive")
        if not master_msg_id and not (slave_msg_id and slave_origin_uid):
            raise ValueError("slave_msg_id and slave_origin_uid must exists together.")
        try:
            if master_msg_id:
                return MsgLog.select().where(MsgLog.master_msg_id == master_msg_id).order_by(MsgLog.time.desc()).first()
            return MsgLog.select().where((MsgLog.slave_message_id == slave_msg_id) & (MsgLog.slave_origin_uid == slave_origin_uid)).order_by(MsgLog.time.desc()).first()
        except DoesNotExist:
            return None

    @observe_database_method("delete_msg_log")
    def delete_msg_log(self, master_msg_id: Optional[TgChatMsgIDStr] = None, slave_msg_id: Optional[EFBChannelChatIDStr] = None, slave_origin_uid: Optional[EFBChannelChatIDStr] = None) -> None:
        if (master_msg_id and (slave_msg_id or slave_origin_uid)) or not (master_msg_id or (slave_msg_id or slave_origin_uid)):
            raise ValueError("master_msg_id and slave_msg_id is mutual exclusive")
        if not master_msg_id and not (slave_msg_id and slave_origin_uid):
            raise ValueError("slave_msg_id and slave_origin_uid must exists together.")
        try:
            if master_msg_id:
                MsgLog.delete().where(MsgLog.master_msg_id == master_msg_id).execute()
            else:
                MsgLog.delete().where((MsgLog.slave_message_id == slave_msg_id) & (MsgLog.slave_origin_uid == slave_origin_uid)).execute()
        except DoesNotExist:
            return

    @observe_database_method("get_recent_slave_chats")
    def get_recent_slave_chats(self, master_chat_id: TelegramChatID, limit: int = 5) -> List[EFBChannelChatIDStr]:
        query = (
            MsgLog.select(MsgLog.slave_origin_uid, fn.MAX(MsgLog.time))
            .where(MsgLog.master_msg_id.startswith(f"{master_chat_id}."))
            .group_by(MsgLog.slave_origin_uid)
            .order_by(fn.MAX(MsgLog.time).desc())
            .limit(limit)
        )
        return [EFBChannelChatIDStr(row.slave_origin_uid) for row in query]

    @observe_database_method("get_last_message")
    def get_last_message(self, slave_chat_id: EFBChannelChatIDStr) -> Optional[MsgLog]:
        try:
            return MsgLog.select().where(MsgLog.slave_origin_uid == slave_chat_id).order_by(MsgLog.time.desc()).limit(1).first()
        except DoesNotExist:
            return None

    @observe_database_method("get_recent_messages")
    def get_recent_messages(self, slave_chat_id: EFBChannelChatIDStr, limit: int = 1000) -> List[MsgLog]:
        try:
            query = MsgLog.select().where(MsgLog.slave_origin_uid == slave_chat_id).order_by(MsgLog.time.asc())
            if limit > 0:
                query = query.limit(limit)
            return list(query)
        except DoesNotExist:
            return []

    @observe_database_method("get_recent_msglog_page")
    def get_recent_message_page(self, slave_chat_id: EFBChannelChatIDStr, after: Optional[Tuple[Optional[datetime], TgChatMsgIDStr]], page_size: int) -> List[MsgLog]:
        query = MsgLog.select().where(MsgLog.slave_origin_uid == slave_chat_id)
        if after is not None:
            after_time, after_message_id = after
            nulls_first = not isinstance(self.database, PostgresqlDatabase)
            if after_time is None and nulls_first:
                query = query.where(((MsgLog.time.is_null(True)) & (MsgLog.master_msg_id > after_message_id)) | MsgLog.time.is_null(False))
            elif after_time is None:
                query = query.where((MsgLog.time.is_null(True)) & (MsgLog.master_msg_id > after_message_id))
            else:
                after_filter = (MsgLog.time > after_time) | ((MsgLog.time == after_time) & (MsgLog.master_msg_id > after_message_id))
                query = query.where(after_filter if nulls_first else after_filter | MsgLog.time.is_null(True))
        return list(query.order_by(MsgLog.time.asc(), MsgLog.master_msg_id.asc()).limit(page_size))
