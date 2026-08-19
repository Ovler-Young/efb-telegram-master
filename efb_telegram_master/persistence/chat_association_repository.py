import logging
from contextlib import contextmanager
from typing import List, Optional, Tuple

from peewee import DoesNotExist, PostgresqlDatabase, SqliteDatabase

from ..models import ChatAssoc, HistoryMigrationEntry, TopicAssoc
from ..utils import EFBChannelChatIDStr, TelegramChatID, TelegramTopicID
from .database_observability import ObservedRepository, observe_database_method


class ChatAssociationRepository(ObservedRepository):
    logger = logging.getLogger(__name__)
    _LOCK_KEY = 681_774_240_616_480_004

    def __init__(self, database=None) -> None:
        super().__init__(database)

    @contextmanager
    def _mutation_transaction(self):
        current_database = self.database
        transaction = current_database.atomic("IMMEDIATE") if isinstance(current_database, SqliteDatabase) else current_database.atomic()
        with transaction:
            if isinstance(current_database, PostgresqlDatabase):
                current_database.execute_sql("SELECT pg_advisory_xact_lock(%s)", (self._LOCK_KEY,))
            yield

    @contextmanager
    def topic_provisioning_transaction(self):
        """Serialize association lookup, remote topic creation, and persistence."""
        with self._bound_models():
            with self._mutation_transaction():
                yield

    @staticmethod
    def _invalidate_history_entries(slave_uids: list[str]) -> None:
        if slave_uids:
            HistoryMigrationEntry.delete().where(HistoryMigrationEntry.slave_chat_id.in_(slave_uids)).execute()

    @observe_database_method("add_chat_assoc")
    def add_chat_assoc(self, master_uid: EFBChannelChatIDStr, slave_uid: EFBChannelChatIDStr, multiple_slave: bool = False):
        with self._mutation_transaction():
            invalidated_slaves = [str(slave_uid)]
            if not multiple_slave:
                previous_slaves = [row.slave_uid for row in ChatAssoc.select(ChatAssoc.slave_uid).where(ChatAssoc.master_uid == master_uid)]
                invalidated_slaves.extend(previous_slaves)
                ChatAssoc.delete().where(ChatAssoc.master_uid == master_uid).execute()
                if previous_slaves:
                    TopicAssoc.delete().where(TopicAssoc.slave_uid.in_(previous_slaves)).execute()
            ChatAssoc.delete().where(ChatAssoc.slave_uid == slave_uid).execute()
            TopicAssoc.delete().where(TopicAssoc.slave_uid == slave_uid).execute()
            self._invalidate_history_entries(invalidated_slaves)
            ChatAssoc.insert(master_uid=master_uid, slave_uid=slave_uid).on_conflict(
                conflict_target=[ChatAssoc.slave_uid],
                update={ChatAssoc.master_uid: master_uid},
            ).execute()
            return ChatAssoc.get(ChatAssoc.slave_uid == slave_uid)

    @observe_database_method("remove_chat_assoc")
    def remove_chat_assoc(self, master_uid: Optional[EFBChannelChatIDStr] = None, slave_uid: Optional[EFBChannelChatIDStr] = None):
        try:
            if bool(master_uid) == bool(slave_uid):
                raise ValueError("Only one parameter is to be provided.")
            with self._mutation_transaction():
                if master_uid:
                    slave_uids = [row.slave_uid for row in ChatAssoc.select(ChatAssoc.slave_uid).where(ChatAssoc.master_uid == master_uid)]
                    result = ChatAssoc.delete().where(ChatAssoc.master_uid == master_uid).execute()
                    if slave_uids:
                        TopicAssoc.delete().where(TopicAssoc.slave_uid.in_(slave_uids)).execute()
                        self._invalidate_history_entries(slave_uids)
                    return result
                result = ChatAssoc.delete().where(ChatAssoc.slave_uid == slave_uid).execute()
                TopicAssoc.delete().where(TopicAssoc.slave_uid == slave_uid).execute()
                self._invalidate_history_entries([str(slave_uid)])
                return result
        except DoesNotExist:
            return 0

    @observe_database_method("get_chat_assoc")
    def get_chat_assoc(self, master_uid: Optional[EFBChannelChatIDStr] = None, slave_uid: Optional[EFBChannelChatIDStr] = None) -> List[EFBChannelChatIDStr]:
        try:
            if bool(master_uid) == bool(slave_uid):
                raise ValueError("Only one parameter is to be provided.")
            if master_uid:
                return [EFBChannelChatIDStr(row.slave_uid) for row in ChatAssoc.select(ChatAssoc.slave_uid).where(ChatAssoc.master_uid == master_uid)]
            return [EFBChannelChatIDStr(row.master_uid) for row in ChatAssoc.select(ChatAssoc.master_uid).where(ChatAssoc.slave_uid == slave_uid)]
        except DoesNotExist:
            return []

    @observe_database_method("add_topic_assoc")
    def add_topic_assoc(self, topic_chat_id: TelegramChatID, message_thread_id: TelegramTopicID, slave_uid: EFBChannelChatIDStr):
        with self._mutation_transaction():
            pair_filter = (TopicAssoc.topic_chat_id == str(topic_chat_id)) & (TopicAssoc.message_thread_id == str(message_thread_id))
            TopicAssoc.delete().where((TopicAssoc.slave_uid == slave_uid) | pair_filter).execute()
            TopicAssoc.insert(topic_chat_id=topic_chat_id, message_thread_id=message_thread_id, slave_uid=slave_uid).execute()
            return TopicAssoc.get(TopicAssoc.slave_uid == slave_uid)

    @observe_database_method("get_topic_thread_id")
    def get_topic_thread_id(self, slave_uid: EFBChannelChatIDStr, topic_chat_id: Optional[TelegramChatID] = None) -> Optional[TelegramTopicID]:
        try:
            query = TopicAssoc.select(TopicAssoc.message_thread_id).where(TopicAssoc.slave_uid == slave_uid)
            if topic_chat_id:
                query = query.where(TopicAssoc.topic_chat_id == topic_chat_id)
            assoc = query.order_by(TopicAssoc.topic_chat_id.desc()).first()
            return TelegramTopicID(int(assoc.message_thread_id)) if assoc else None
        except DoesNotExist:
            return None

    @observe_database_method("get_topic_slave")
    def get_topic_slave(self, topic_chat_id: TelegramChatID, message_thread_id: Optional[TelegramTopicID] = None) -> Optional[EFBChannelChatIDStr]:
        try:
            query = TopicAssoc.select(TopicAssoc.slave_uid).where(TopicAssoc.topic_chat_id == topic_chat_id)
            if message_thread_id:
                query = query.where(TopicAssoc.message_thread_id == message_thread_id)
            assoc = query.first()
            return EFBChannelChatIDStr(assoc.slave_uid) if assoc else None
        except DoesNotExist:
            return None

    def get_topic_assoc_slave_uid(self, source_chat_id: int, topic_id: int) -> Optional[EFBChannelChatIDStr]:
        assoc = TopicAssoc.get_or_none((TopicAssoc.topic_chat_id == str(source_chat_id)) & (TopicAssoc.message_thread_id == str(topic_id)))
        return EFBChannelChatIDStr(assoc.slave_uid) if assoc is not None else None

    @observe_database_method("get_topic_slaves")
    def get_topic_slaves(self, topic_chat_id: TelegramChatID) -> Optional[List[Tuple[EFBChannelChatIDStr, TelegramTopicID]]]:
        try:
            query = TopicAssoc.select(TopicAssoc.slave_uid, TopicAssoc.message_thread_id).where(TopicAssoc.topic_chat_id == topic_chat_id).order_by(TopicAssoc.id.desc())
            return [(EFBChannelChatIDStr(row.slave_uid), TelegramTopicID(int(row.message_thread_id))) for row in query]
        except (DoesNotExist, AttributeError):
            return None

    @observe_database_method("remove_topic_assoc")
    def remove_topic_assoc(self, topic_chat_id: Optional[TelegramChatID] = None, message_thread_id: Optional[TelegramTopicID] = None, slave_uid: Optional[EFBChannelChatIDStr] = None):
        try:
            if bool(topic_chat_id and message_thread_id) == bool(slave_uid):
                raise ValueError("Please provide either topic_chat_id and message_thread_id or slave_uid.")
            with self._mutation_transaction():
                if topic_chat_id and message_thread_id:
                    return TopicAssoc.delete().where((TopicAssoc.topic_chat_id == str(topic_chat_id)) & (TopicAssoc.message_thread_id == str(message_thread_id))).execute()
                return TopicAssoc.delete().where(TopicAssoc.slave_uid == slave_uid).execute()
        except DoesNotExist:
            return 0
