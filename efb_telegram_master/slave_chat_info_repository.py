import logging
from typing import TYPE_CHECKING, Optional

from ehforwarderbot.types import ChatID, ModuleID
from peewee import DoesNotExist

from .database_observability import ObservedRepository, observe_database_method
from .models import SlaveChatInfo

if TYPE_CHECKING:
    from .chat import ETMChatMixin


class SlaveChatInfoRepository(ObservedRepository):
    logger = logging.getLogger(__name__)

    @observe_database_method("get_slave_chat_info")
    def get_slave_chat_info(self, slave_channel_id: Optional[ModuleID] = None, slave_chat_uid: Optional[ChatID] = None, slave_chat_group_id: Optional[ChatID] = None) -> Optional[SlaveChatInfo]:
        if slave_channel_id is None or slave_chat_uid is None:
            raise ValueError("Both slave_channel_id and slave_chat_id should be provided.")
        try:
            return SlaveChatInfo.select().where((SlaveChatInfo.slave_channel_id == slave_channel_id) & (SlaveChatInfo.slave_chat_uid == slave_chat_uid) & (SlaveChatInfo.slave_chat_group_id == slave_chat_group_id)).first()
        except DoesNotExist:
            return None

    @observe_database_method("set_slave_chat_info")
    def set_slave_chat_info(self, chat_object: "ETMChatMixin") -> SlaveChatInfo:
        parent_chat: Optional["ETMChatMixin"] = getattr(chat_object, "chat", None)
        group_id = parent_chat.uid if parent_chat else None
        chat_info = self.get_slave_chat_info(chat_object.module_id, chat_object.uid, group_id)
        if chat_info is not None:
            chat_info.slave_channel_emoji = chat_object.channel_emoji
            chat_info.slave_chat_name = chat_object.name
            chat_info.slave_chat_alias = chat_object.alias
            chat_info.slave_chat_type = chat_object.chat_type_name
            chat_info.pickle = chat_object.pickle
            chat_info.save()
            return chat_info
        return SlaveChatInfo.create(
            slave_channel_id=chat_object.module_id,
            slave_channel_emoji=chat_object.channel_emoji,
            slave_chat_uid=chat_object.uid,
            slave_chat_group_id=group_id,
            slave_chat_name=chat_object.name,
            slave_chat_alias=chat_object.alias,
            slave_chat_type=chat_object.chat_type_name,
            pickle=chat_object.pickle,
        )

    @observe_database_method("delete_slave_chat_info")
    def delete_slave_chat_info(self, slave_channel_id: ModuleID, slave_chat_uid: ChatID, slave_chat_group_id: Optional[ChatID] = None):
        return SlaveChatInfo.delete().where((SlaveChatInfo.slave_channel_id == slave_channel_id) & (SlaveChatInfo.slave_chat_uid == slave_chat_uid) & (SlaveChatInfo.slave_chat_group_id == slave_chat_group_id)).execute()
