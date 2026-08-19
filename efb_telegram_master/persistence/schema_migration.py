from threading import RLock
from typing import Any

from peewee import Model, PostgresqlDatabase, SqliteDatabase, TextField
from playhouse.migrate import Operation, PostgresqlMigrator, SqliteMigrator, migrate

from ..models import DATABASE_MODELS, ChatAssoc, HistoryMigrationEntry, MsgLog, MsgLogIngestionScan, SlaveChatInfo, SlaveMessageDelivery, TopicAssoc


class DatabaseSchemaMigrator:
    """Create current tables and upgrade supported historical schemas."""

    HISTORIC_SCHEMA_LOCK_KEY = 681_774_240_616_480_002
    MSGLOG_REPLAY_SOURCE_INDEX = "msglog_slave_origin_uid_time_master_msg_id"
    CHAT_ASSOC_SLAVE_INDEX = "chatassoc_slave_uid"
    TOPIC_ASSOC_SLAVE_INDEX = "topicassoc_slave_uid"
    TOPIC_ASSOC_TOPIC_THREAD_INDEX = "topicassoc_topic_chat_id_message_thread_id"
    SLAVE_CHAT_INFO_IDENTITY_WITHOUT_GROUP_INDEX = "slavechatinfo_identity_without_group_unique"
    SLAVE_CHAT_INFO_IDENTITY_WITH_GROUP_INDEX = "slavechatinfo_identity_with_group_unique"
    HISTORY_TARGET_POSITION_WITHOUT_THREAD_INDEX = "historymigrationentry_target_position_without_thread_unique"
    HISTORY_TARGET_POSITION_WITH_THREAD_INDEX = "historymigrationentry_target_position_with_thread_unique"
    _model_binding_lock = RLock()

    def __init__(self, database: Any) -> None:
        self.database = database

    def create(self) -> None:
        existing_tables = set(self.database.get_tables())
        if {"chatassoc", "topicassoc", "historymigrationentry", "slavechatinfo", "slavemessagedelivery"} & existing_tables:
            self.ensure_historic_schema_columns()
        with self._model_binding_lock:
            with self.database.bind_ctx(DATABASE_MODELS):
                self.database.create_tables(DATABASE_MODELS)
        self.ensure_historic_schema_columns()

    def ensure_historic_schema_columns(self) -> None:
        with self._model_binding_lock:
            with self.database.bind_ctx(DATABASE_MODELS):
                self._ensure_historic_schema_columns_bound()

    def _ensure_historic_schema_columns_bound(self) -> None:
        transaction_arguments = ("IMMEDIATE",) if isinstance(self.database, SqliteDatabase) else ()
        if not isinstance(self.database, (SqliteDatabase, PostgresqlDatabase)):
            raise TypeError(f"Unsupported database backend: {type(self.database).__name__}")
        with self.database.atomic(*transaction_arguments):
            if isinstance(self.database, PostgresqlDatabase):
                self.database.execute_sql("SELECT pg_advisory_xact_lock(%s)", (self.HISTORIC_SCHEMA_LOCK_KEY,))
            tables = set(self.database.get_tables())
            migrator = SqliteMigrator(self.database) if isinstance(self.database, SqliteDatabase) else PostgresqlMigrator(self.database)
            steps: list[Operation] = []
            self._append_columns(
                steps,
                migrator,
                tables,
                "msglog",
                (
                    ("file_id", MsgLog.file_id),
                    ("media_type", MsgLog.media_type),
                    ("mime", MsgLog.mime),
                    ("master_msg_id_alt", MsgLog.master_msg_id_alt),
                    ("pickle", MsgLog.pickle),
                    ("file_unique_id", MsgLog.file_unique_id),
                    ("sender_bot_id", MsgLog.sender_bot_id),
                    ("provenance", MsgLog.provenance),
                    ("time", MsgLog.time),
                ),
            )
            scan_steps: list[Operation] = []
            self._append_columns(scan_steps, migrator, tables, "msglogingestionscan", (("rescan_requested", MsgLogIngestionScan.rescan_requested), ("lease_clock", TextField(null=True))))
            if scan_steps:
                migrate(*scan_steps)
            self._append_columns(steps, migrator, tables, "slavechatinfo", (("pickle", SlaveChatInfo.pickle), ("slave_chat_group_id", SlaveChatInfo.slave_chat_group_id)))
            if steps:
                migrate(*steps)
            delivery_steps: list[Operation] = []
            self._append_columns(
                delivery_steps,
                migrator,
                tables,
                "slavemessagedelivery",
                (
                    ("state", SlaveMessageDelivery.state),
                    ("lease_expires_at", SlaveMessageDelivery.lease_expires_at),
                    ("owner_token", SlaveMessageDelivery.owner_token),
                    ("lease_clock", TextField(null=True)),
                ),
            )
            if delivery_steps:
                migrate(*delivery_steps)
            self._create_historic_indexes(tables)

    def _append_columns(self, steps: list[Operation], migrator: Any, tables: set[str], table: str, columns: tuple[tuple[str, Any], ...]) -> None:
        if table not in tables:
            return
        existing_columns = {column.name for column in self.database.get_columns(table)}
        steps.extend(migrator.add_column(table, name, field) for name, field in columns if name not in existing_columns)

    def _create_historic_indexes(self, tables: set[str]) -> None:
        if "slavechatinfo" in tables:
            self._deduplicate_by_key(SlaveChatInfo, (SlaveChatInfo.slave_channel_id, SlaveChatInfo.slave_chat_uid, SlaveChatInfo.slave_chat_group_id))
            self.database.execute_sql(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {self.SLAVE_CHAT_INFO_IDENTITY_WITHOUT_GROUP_INDEX} ON slavechatinfo (slave_channel_id, slave_chat_uid) WHERE slave_chat_group_id IS NULL"
            )
            self.database.execute_sql(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {self.SLAVE_CHAT_INFO_IDENTITY_WITH_GROUP_INDEX} ON slavechatinfo (slave_channel_id, slave_chat_uid, slave_chat_group_id) WHERE slave_chat_group_id IS NOT NULL"
            )
        if "msglog" in tables:
            self.database.execute_sql(f"CREATE INDEX IF NOT EXISTS {self.MSGLOG_REPLAY_SOURCE_INDEX} ON msglog (slave_origin_uid, time, master_msg_id)")
        if "chatassoc" in tables:
            self._deduplicate_by_key(ChatAssoc, (ChatAssoc.slave_uid,))
            self.database.execute_sql(f"CREATE UNIQUE INDEX IF NOT EXISTS {self.CHAT_ASSOC_SLAVE_INDEX} ON chatassoc (slave_uid)")
        if "topicassoc" in tables:
            self._deduplicate_by_key(TopicAssoc, (TopicAssoc.slave_uid,))
            self._deduplicate_by_key(TopicAssoc, (TopicAssoc.topic_chat_id, TopicAssoc.message_thread_id))
            self.database.execute_sql(f"CREATE UNIQUE INDEX IF NOT EXISTS {self.TOPIC_ASSOC_SLAVE_INDEX} ON topicassoc (slave_uid)")
            self.database.execute_sql(f"CREATE UNIQUE INDEX IF NOT EXISTS {self.TOPIC_ASSOC_TOPIC_THREAD_INDEX} ON topicassoc (topic_chat_id, message_thread_id)")
        if "historymigrationentry" in tables:
            self._deduplicate_by_key(
                HistoryMigrationEntry, (HistoryMigrationEntry.slave_chat_id, HistoryMigrationEntry.target_chat_id, HistoryMigrationEntry.message_thread_id, HistoryMigrationEntry.position)
            )
            self.database.execute_sql(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {self.HISTORY_TARGET_POSITION_WITHOUT_THREAD_INDEX} ON historymigrationentry (slave_chat_id, target_chat_id, position) WHERE message_thread_id IS NULL"
            )
            self.database.execute_sql(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {self.HISTORY_TARGET_POSITION_WITH_THREAD_INDEX} ON historymigrationentry (slave_chat_id, target_chat_id, message_thread_id, position) WHERE message_thread_id IS NOT NULL"
            )

    @staticmethod
    def _deduplicate_by_key(model: type[Model], fields: tuple[Any, ...]) -> None:
        primary_key = model._meta.primary_key
        if primary_key is False:
            raise ValueError(f"{model.__name__} has no primary key")
        candidate_ids = {getattr(row, primary_key.name) for row in model.select(primary_key)}
        seen: set[tuple[Any, ...]] = set()
        canonical_ids: set[object] = set()
        for row in model.select(primary_key, *fields).where(primary_key.in_(candidate_ids)).order_by(primary_key.desc()):
            key = tuple(getattr(row, field.name) for field in fields)
            if key not in seen:
                seen.add(key)
                canonical_ids.add(getattr(row, primary_key.name))
        duplicate_ids = candidate_ids - canonical_ids
        if duplicate_ids:
            model.delete().where(primary_key.in_(duplicate_ids)).execute()

    @classmethod
    def canonical_historic_primary_key_values(cls, model: type[Model]) -> set[object] | None:
        identities: tuple[tuple[Any, ...], ...]
        if model is ChatAssoc:
            identities = ((ChatAssoc.slave_uid,),)
        elif model is TopicAssoc:
            identities = ((TopicAssoc.slave_uid,), (TopicAssoc.topic_chat_id, TopicAssoc.message_thread_id))
        elif model is HistoryMigrationEntry:
            identities = ((HistoryMigrationEntry.slave_chat_id, HistoryMigrationEntry.target_chat_id, HistoryMigrationEntry.message_thread_id, HistoryMigrationEntry.position),)
        else:
            return None
        primary_key = model._meta.primary_key
        if primary_key is False:
            raise ValueError(f"{model.__name__} has no primary key")
        candidate_ids = {getattr(row, primary_key.name) for row in model.select(primary_key)}
        for fields in identities:
            seen: set[tuple[Any, ...]] = set()
            canonical_ids: set[object] = set()
            for row in model.select(primary_key, *fields).where(primary_key.in_(candidate_ids)).order_by(primary_key.desc()):
                key = tuple(getattr(row, field.name) for field in fields)
                if key not in seen:
                    seen.add(key)
                    canonical_ids.add(getattr(row, primary_key.name))
            candidate_ids = canonical_ids
        return candidate_ids
