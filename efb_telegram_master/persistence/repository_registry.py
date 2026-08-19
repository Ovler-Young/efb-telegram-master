from .chat_association_repository import ChatAssociationRepository
from .history_migration_repository import HistoryMigrationRepository
from .msglog_ingestion_repository import MsgLogIngestionRepository
from .msglog_repository import MsgLogRepository
from .slave_chat_info_repository import SlaveChatInfoRepository
from .slave_message_delivery_repository import SlaveMessageDeliveryRepository


class Repositories:
    """Database-bound repositories owned by one channel instance."""

    def __init__(self, database, channel_id: str) -> None:
        self.chat_associations = ChatAssociationRepository(database)
        self.slave_chat_info = SlaveChatInfoRepository(database)
        self.slave_message_deliveries = SlaveMessageDeliveryRepository(database)
        self.msglogs = MsgLogRepository(database)
        self.history_migrations = HistoryMigrationRepository(database)
        self.msglog_ingestion = MsgLogIngestionRepository(channel_id, database)
