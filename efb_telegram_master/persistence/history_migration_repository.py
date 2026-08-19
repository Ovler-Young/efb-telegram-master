import pickle
import sqlite3
from contextlib import contextmanager
from itertools import islice
from tempfile import TemporaryDirectory
from typing import Dict, Iterable, List, Optional, Tuple

from peewee import PostgresqlDatabase, SqliteDatabase

from ..models import HistoryMigrationEntry
from ..utils import EFBChannelChatIDStr, TelegramTopicID
from .database_observability import ObservedRepository, observe_database_method


class HistoryMigrationRepository(ObservedRepository):
    INSERT_BATCH_SIZE = 100
    _LOCK_KEY = 681_774_240_616_480_005

    def __init__(self, database) -> None:
        super().__init__(database)

    @contextmanager
    def _replacement_transaction(self):
        current_database = self.database
        transaction = current_database.atomic("IMMEDIATE") if isinstance(current_database, SqliteDatabase) else current_database.atomic()
        with transaction:
            if isinstance(current_database, PostgresqlDatabase):
                current_database.execute_sql("SELECT pg_advisory_xact_lock(%s)", (self._LOCK_KEY,))
            yield

    @contextmanager
    def _staged_entry_batches(self, entries: Iterable[Dict[str, object]]):
        """Stage source entries on disk before replacing target rows."""
        with TemporaryDirectory(prefix="etm-history-migration-") as temporary_directory:
            with sqlite3.connect(f"{temporary_directory}/entries.db") as staging_database:
                staging_database.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
                batch = []
                count = 0
                for entry in entries:
                    batch.append((sqlite3.Binary(pickle.dumps(entry, protocol=pickle.HIGHEST_PROTOCOL)),))
                    if len(batch) == self.INSERT_BATCH_SIZE:
                        staging_database.executemany("INSERT INTO entries (payload) VALUES (?)", batch)
                        staging_database.commit()
                        count += len(batch)
                        batch.clear()
                if batch:
                    staging_database.executemany("INSERT INTO entries (payload) VALUES (?)", batch)
                    staging_database.commit()
                    count += len(batch)

                cursor = staging_database.execute("SELECT payload FROM entries ORDER BY id ASC")

                def staged_batches():
                    while rows := list(islice(cursor, self.INSERT_BATCH_SIZE)):
                        yield [pickle.loads(payload) for (payload,) in rows]

                yield count, staged_batches()

    @staticmethod
    def _target_filter(slave_chat_id: EFBChannelChatIDStr, target_chat_id: int, message_thread_id: Optional[TelegramTopicID] = None):
        thread_value = str(message_thread_id) if message_thread_id is not None else None
        base_filter = (HistoryMigrationEntry.slave_chat_id == str(slave_chat_id)) & (HistoryMigrationEntry.target_chat_id == str(target_chat_id))
        if thread_value is None:
            return base_filter & HistoryMigrationEntry.message_thread_id.is_null(True)
        return base_filter & (HistoryMigrationEntry.message_thread_id == thread_value)

    @observe_database_method("replace_history_migration_entries")
    def replace_entries(self, slave_chat_id: EFBChannelChatIDStr, target_chat_id: int, message_thread_id: Optional[TelegramTopicID], entries: Iterable[Dict[str, object]]) -> int:
        with self._staged_entry_batches(entries) as (count, staged_batches):
            with self._replacement_transaction():
                HistoryMigrationEntry.delete().where(self._target_filter(slave_chat_id, target_chat_id, message_thread_id)).execute()
                for batch in staged_batches:
                    HistoryMigrationEntry.insert_many(batch).execute()
        return count

    @observe_database_method("has_pending_history_migrations")
    def has_pending_entries(self) -> bool:
        return HistoryMigrationEntry.select().exists()

    @observe_database_method("get_next_history_migration_target")
    def get_next_target(self) -> Optional[HistoryMigrationEntry]:
        return HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.id.asc()).first()

    @observe_database_method("get_history_migration_entry_page")
    def get_entries_page(
        self,
        slave_chat_id: EFBChannelChatIDStr,
        target_chat_id: int,
        message_thread_id: Optional[TelegramTopicID],
        after: Optional[Tuple[int, int]],
        page_size: int,
    ) -> List[HistoryMigrationEntry]:
        query = HistoryMigrationEntry.select().where(self._target_filter(slave_chat_id, target_chat_id, message_thread_id))
        if after is not None:
            after_position, after_id = after
            query = query.where((HistoryMigrationEntry.position > after_position) | ((HistoryMigrationEntry.position == after_position) & (HistoryMigrationEntry.id > after_id)))
        return list(query.order_by(HistoryMigrationEntry.position.asc(), HistoryMigrationEntry.id.asc()).limit(page_size))

    @observe_database_method("delete_history_migration_entry")
    def delete_entry(self, entry_id: int) -> int:
        return int(HistoryMigrationEntry.delete().where(HistoryMigrationEntry.id == entry_id).execute())
