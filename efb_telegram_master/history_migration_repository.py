from typing import Dict, List, Optional

from .database_observability import ObservedRepository, observe_database_method
from .models import HistoryMigrationEntry, database
from .utils import EFBChannelChatIDStr, TelegramTopicID


class HistoryMigrationRepository(ObservedRepository):
    @staticmethod
    def _target_filter(slave_chat_id: EFBChannelChatIDStr, target_chat_id: int, message_thread_id: Optional[TelegramTopicID] = None):
        thread_value = str(message_thread_id) if message_thread_id is not None else None
        base_filter = (HistoryMigrationEntry.slave_chat_id == str(slave_chat_id)) & (HistoryMigrationEntry.target_chat_id == str(target_chat_id))
        if thread_value is None:
            return base_filter & HistoryMigrationEntry.message_thread_id.is_null(True)
        return base_filter & (HistoryMigrationEntry.message_thread_id == thread_value)

    @observe_database_method("replace_history_migration_entries")
    def replace_entries(self, slave_chat_id: EFBChannelChatIDStr, target_chat_id: int, message_thread_id: Optional[TelegramTopicID], entries: List[Dict[str, object]]) -> int:
        with database.atomic():
            HistoryMigrationEntry.delete().where(self._target_filter(slave_chat_id, target_chat_id, message_thread_id)).execute()
            if entries:
                HistoryMigrationEntry.insert_many(entries).execute()
        return len(entries)

    @observe_database_method("has_pending_history_migrations")
    def has_pending_entries(self) -> bool:
        return HistoryMigrationEntry.select().exists()

    @observe_database_method("get_next_history_migration_target")
    def get_next_target(self) -> Optional[HistoryMigrationEntry]:
        return HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.id.asc()).first()

    @observe_database_method("get_history_migration_entries")
    def get_entries(self, slave_chat_id: EFBChannelChatIDStr, target_chat_id: int, message_thread_id: Optional[TelegramTopicID] = None) -> List[HistoryMigrationEntry]:
        return list(
            HistoryMigrationEntry.select().where(self._target_filter(slave_chat_id, target_chat_id, message_thread_id)).order_by(HistoryMigrationEntry.position.asc(), HistoryMigrationEntry.id.asc())
        )

    @observe_database_method("delete_history_migration_entry")
    def delete_entry(self, entry_id: int) -> int:
        return int(HistoryMigrationEntry.delete().where(HistoryMigrationEntry.id == entry_id).execute())
