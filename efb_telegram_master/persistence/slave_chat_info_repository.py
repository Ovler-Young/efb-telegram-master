import logging
from typing import TYPE_CHECKING, Optional

from ehforwarderbot.types import ChatID, ModuleID
from peewee import IntegrityError

from ..models import SlaveChatInfo
from .database_observability import ObservedRepository, observe_database_method

if TYPE_CHECKING:
    from ..chat import ETMChatMixin


class SlaveChatInfoRepository(ObservedRepository):
    logger = logging.getLogger(__name__)

    def __init__(self, database) -> None:
        super().__init__(database)

    @observe_database_method("get_slave_chat_info")
    def get_slave_chat_info(self, slave_channel_id: Optional[ModuleID] = None, slave_chat_uid: Optional[ChatID] = None, slave_chat_group_id: Optional[ChatID] = None) -> Optional[SlaveChatInfo]:
        if slave_channel_id is None or slave_chat_uid is None:
            raise ValueError("Both slave_channel_id and slave_chat_id should be provided.")
        query = (SlaveChatInfo.slave_channel_id == slave_channel_id) & (SlaveChatInfo.slave_chat_uid == slave_chat_uid)
        group_query = SlaveChatInfo.slave_chat_group_id.is_null(True) if slave_chat_group_id is None else SlaveChatInfo.slave_chat_group_id == slave_chat_group_id
        return SlaveChatInfo.select().where(query & group_query).first()

    @observe_database_method("set_slave_chat_info")
    def set_slave_chat_info(self, chat_object: "ETMChatMixin") -> SlaveChatInfo:
        parent_chat: Optional["ETMChatMixin"] = getattr(chat_object, "chat", None)
        group_id = parent_chat.uid if parent_chat else None
        values = dict(
            slave_channel_id=chat_object.module_id,
            slave_channel_emoji=chat_object.channel_emoji,
            slave_chat_uid=chat_object.uid,
            slave_chat_group_id=group_id,
            slave_chat_name=chat_object.name,
            slave_chat_alias=chat_object.alias,
            slave_chat_type=chat_object.chat_type_name,
            pickle=chat_object.pickle,
        )
        identity = (SlaveChatInfo.slave_channel_id == chat_object.module_id) & (SlaveChatInfo.slave_chat_uid == chat_object.uid)
        group_identity = SlaveChatInfo.slave_chat_group_id.is_null(True) if group_id is None else SlaveChatInfo.slave_chat_group_id == group_id
        try:
            SlaveChatInfo.create(**values)
        except IntegrityError:
            SlaveChatInfo.update(**values).where(identity & group_identity).execute()
        chat_info = self.get_slave_chat_info(chat_object.module_id, chat_object.uid, group_id)
        if chat_info is None:
            raise RuntimeError("Slave chat info write did not produce a canonical row")
        return chat_info

    @observe_database_method("delete_slave_chat_info")
    def delete_slave_chat_info(self, slave_channel_id: ModuleID, slave_chat_uid: ChatID, slave_chat_group_id: Optional[ChatID] = None):
        return (
            SlaveChatInfo.delete()
            .where(
                (SlaveChatInfo.slave_channel_id == slave_channel_id)
                & (SlaveChatInfo.slave_chat_uid == slave_chat_uid)
                & (SlaveChatInfo.slave_chat_group_id.is_null(True) if slave_chat_group_id is None else SlaveChatInfo.slave_chat_group_id == slave_chat_group_id)
            )
            .execute()
        )
