# coding=utf-8

import json
import logging
import os
import re
import sqlite3
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

from ehforwarderbot import utils
from peewee import AutoField, Model, PostgresqlDatabase, SqliteDatabase
from playhouse.migrate import Operation, PostgresqlMigrator, SqliteMigrator, migrate

from .chat_association_repository import ChatAssociationRepository
from .database_observability import DatabaseMetrics, observe_database_method
from .history_migration_repository import HistoryMigrationRepository
from .models import ChatAssoc, HistoryMigrationEntry, MsgLog, MsgLogIngestionScan, SlaveChatInfo, SlaveMessageDelivery, TopicAssoc, database
from .msglog_ingestion_repository import MsgLogIngestionRepository
from .msglog_repository import MsgLogRepository
from .slave_chat_info_repository import SlaveChatInfoRepository
from .slave_message_delivery_repository import SlaveMessageDeliveryRepository

if TYPE_CHECKING:
    from . import TelegramChannel


@dataclass(frozen=True)
class _SQLiteSourceProjection:
    model: type[Model]
    column_names: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class _SQLiteImportSnapshot:
    projections: tuple[_SQLiteSourceProjection, ...]
    identity: str


class DatabaseManager:
    logger = logging.getLogger(__name__)
    _LEGACY_OUTBOUND_TABLES = ("outboundworkflow", "outboundtask")
    _LEGACY_OUTBOUND_LOCK_KEY = 681_774_240_616_480_003
    _HISTORIC_SCHEMA_LOCK_KEY = 681_774_240_616_480_002
    _SQLITE_IMPORT_LOCK_KEY = 681_774_240_616_480_001
    _SQLITE_IMPORT_PROVENANCE_TABLE = "sqliteimportprovenance"
    _MSGLOG_REPLAY_SOURCE_INDEX = "msglog_slave_origin_uid_time_master_msg_id"
    _CHAT_ASSOC_SLAVE_INDEX = "chatassoc_slave_uid"
    _TOPIC_ASSOC_SLAVE_INDEX = "topicassoc_slave_uid"
    _TOPIC_ASSOC_TOPIC_THREAD_INDEX = "topicassoc_topic_chat_id_message_thread_id"
    _SLAVE_CHAT_INFO_IDENTITY_WITHOUT_GROUP_INDEX = "slavechatinfo_identity_without_group_unique"
    _SLAVE_CHAT_INFO_IDENTITY_WITH_GROUP_INDEX = "slavechatinfo_identity_with_group_unique"
    _HISTORY_TARGET_POSITION_WITHOUT_THREAD_INDEX = "historymigrationentry_target_position_without_thread_unique"
    _HISTORY_TARGET_POSITION_WITH_THREAD_INDEX = "historymigrationentry_target_position_with_thread_unique"
    _LEGACY_OUTBOUND_COLUMNS = {
        "outboundworkflow": (
            ("id", "integer", False, True),
            ("state", "text", False, False),
            ("result_task_id", "integer", True, False),
            ("error_class", "text", True, False),
            ("created_at", "datetime", False, False),
            ("completed_at", "datetime", True, False),
        ),
        "outboundtask": (
            ("id", "integer", False, True),
            ("source_key", "text", False, False),
            ("slave_id", "text", True, False),
            ("priority", "boolean", False, False),
            ("target_chat_id", "integer", False, False),
            ("message_thread_id", "integer", True, False),
            ("operation", "text", False, False),
            ("payload", "text", False, False),
            ("media_ref", "text", True, False),
            ("workflow_id", "integer", False, False),
            ("step_index", "integer", False, False),
            ("depends_on_task_id", "integer", True, False),
            ("run_condition", "text", False, False),
            ("result_payload", "text", True, False),
            ("log_payload", "text", True, False),
            ("required_sender_bot_id", "text", True, False),
            ("state", "text", False, False),
            ("available_at", "datetime", True, False),
            ("lease_owner", "text", True, False),
            ("lease_until", "datetime", True, False),
            ("lease_heartbeat_at", "datetime", True, False),
            ("submitted_at", "datetime", True, False),
            ("attempt_count", "integer", False, False),
            ("accepted_at", "datetime", False, False),
            ("error_class", "text", True, False),
            ("last_error", "text", True, False),
        ),
    }
    _LEGACY_OUTBOUND_TASK_INDEXES = (
        ("outboundtask_workflow_id", ("workflow_id",), False),
        ("outboundtask_source_key_priority_accepted_at_id", ("source_key", "priority", "accepted_at", "id"), False),
        ("outboundtask_state_available_at", ("state", "available_at"), False),
        ("outboundtask_workflow_id_step_index", ("workflow_id", "step_index"), True),
    )
    _LEGACY_OUTBOUND_DEFAULT_CATEGORIES = {
        "outboundworkflow": {"id": "auto_pk", "created_at": "current_timestamp"},
        "outboundtask": {"id": "auto_pk", "accepted_at": "current_timestamp"},
    }

    def __init__(self, channel: "TelegramChannel"):
        self.channel: "TelegramChannel" = channel
        self._metrics: Optional[DatabaseMetrics] = None
        self.chat_associations = ChatAssociationRepository()
        self.slave_chat_info = SlaveChatInfoRepository()
        self.slave_message_deliveries = SlaveMessageDeliveryRepository()
        self.msglogs = MsgLogRepository()
        self.history_migrations = HistoryMigrationRepository()
        self.msglog_ingestion = MsgLogIngestionRepository(channel.channel_id)
        base_path = utils.get_data_path(channel.channel_id)
        self._base_path = base_path

        self.logger.debug("Loading database...")
        db_config = channel.config.get("database", {})
        db_type = db_config.get("type", "sqlite")
        if db_type == "postgresql":
            from playhouse.postgres_ext import PooledPostgresqlExtDatabase

            actual_db = PooledPostgresqlExtDatabase(
                db_config.get("database", "efb_telegram"),
                host=db_config.get("host", "localhost"),
                port=db_config.get("port", 5432),
                user=db_config.get("user", "postgres"),
                password=db_config.get("password", ""),
                max_connections=db_config.get("max_connections", 8),
                stale_timeout=db_config.get("stale_timeout", 300),
                options=db_config.get("options", "-c timezone=UTC"),
            )
        else:
            actual_db = SqliteDatabase(
                str(base_path / "tgdata.db"),
                pragmas={"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 5000},
                check_same_thread=False,
            )

        database.initialize(actual_db)
        connected = False
        try:
            database.connect()
            connected = True
            self.logger.debug("Database loaded.")
            self.logger.debug("Checking database migration...")
            if isinstance(actual_db, PostgresqlDatabase):
                self._initialize_postgresql(base_path)
            else:
                self._create()
            self.logger.debug("Database migration finished...")
            self._retire_legacy_outbound_tables()
        except BaseException:
            if connected:
                try:
                    self.stop_worker()
                except BaseException:
                    try:
                        self.logger.exception("Failed to close database after database initialization failed.")
                    except BaseException:
                        pass
            raise

    def set_metrics(self, metrics: DatabaseMetrics) -> None:
        self._metrics = metrics
        for repository in (self.chat_associations, self.slave_chat_info, self.slave_message_deliveries, self.msglogs, self.history_migrations, self.msglog_ingestion):
            repository._metrics = metrics

    @observe_database_method("stop_worker")
    def stop_worker(self) -> None:
        stop = getattr(database.obj, "stop", None)
        if callable(stop):
            stop()
        database.close()

    @staticmethod
    def _create() -> None:
        existing_tables = set(database.get_tables())
        if {"chatassoc", "topicassoc", "historymigrationentry", "slavechatinfo", "slavemessagedelivery"} & existing_tables:
            DatabaseManager._ensure_historic_schema_columns(database.obj)
        database.create_tables([ChatAssoc, MsgLog, SlaveChatInfo, TopicAssoc, HistoryMigrationEntry, MsgLogIngestionScan, SlaveMessageDelivery])
        DatabaseManager._ensure_historic_schema_columns(database.obj)

    @staticmethod
    def _ensure_historic_schema_columns(current_database) -> None:
        transaction_arguments: Tuple[str, ...] = ()
        if isinstance(current_database, SqliteDatabase):
            transaction_arguments = ("IMMEDIATE",)
        elif not isinstance(current_database, PostgresqlDatabase):
            raise TypeError(f"Unsupported database backend: {type(current_database).__name__}")

        with current_database.atomic(*transaction_arguments):
            if isinstance(current_database, PostgresqlDatabase):
                current_database.execute_sql("SELECT pg_advisory_xact_lock(%s)", (DatabaseManager._HISTORIC_SCHEMA_LOCK_KEY,))
            table_names = set(current_database.get_tables())
            migrator = SqliteMigrator(current_database) if isinstance(current_database, SqliteDatabase) else PostgresqlMigrator(current_database)
            migration_steps: list[Operation] = []
            if "msglog" in table_names:
                msglog_columns = {column.name for column in current_database.get_columns("msglog")}
                migration_steps.extend(
                    migrator.add_column("msglog", column_name, field)
                    for column_name, field in (
                        ("file_id", MsgLog.file_id),
                        ("media_type", MsgLog.media_type),
                        ("mime", MsgLog.mime),
                        ("master_msg_id_alt", MsgLog.master_msg_id_alt),
                        ("pickle", MsgLog.pickle),
                        ("file_unique_id", MsgLog.file_unique_id),
                        ("sender_bot_id", MsgLog.sender_bot_id),
                        ("provenance", MsgLog.provenance),
                        ("time", MsgLog.time),
                    )
                    if column_name not in msglog_columns
                )
            if "msglogingestionscan" in table_names:
                scan_columns = {column.name for column in current_database.get_columns("msglogingestionscan")}
                scan_migrations = tuple(
                    migrator.add_column("msglogingestionscan", column_name, field)
                    for column_name, field in (("rescan_requested", MsgLogIngestionScan.rescan_requested),)
                    if column_name not in scan_columns
                )
                if scan_migrations:
                    migrate(*scan_migrations)
            if "slavechatinfo" in table_names:
                slave_chat_info_columns = {column.name for column in current_database.get_columns("slavechatinfo")}
                migration_steps.extend(
                    migrator.add_column("slavechatinfo", column_name, field)
                    for column_name, field in (
                        ("pickle", SlaveChatInfo.pickle),
                        ("slave_chat_group_id", SlaveChatInfo.slave_chat_group_id),
                    )
                    if column_name not in slave_chat_info_columns
                )
            if migration_steps:
                migrate(*migration_steps)
            if "slavechatinfo" in table_names:
                DatabaseManager._deduplicate_slave_chat_info()
                current_database.execute_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {DatabaseManager._SLAVE_CHAT_INFO_IDENTITY_WITHOUT_GROUP_INDEX} "
                    "ON slavechatinfo (slave_channel_id, slave_chat_uid) WHERE slave_chat_group_id IS NULL"
                )
                current_database.execute_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {DatabaseManager._SLAVE_CHAT_INFO_IDENTITY_WITH_GROUP_INDEX} "
                    "ON slavechatinfo (slave_channel_id, slave_chat_uid, slave_chat_group_id) WHERE slave_chat_group_id IS NOT NULL"
                )
            if "slavemessagedelivery" in table_names:
                delivery_columns = {column.name for column in current_database.get_columns("slavemessagedelivery")}
                delivery_migrations = tuple(
                    migrator.add_column("slavemessagedelivery", column_name, field)
                    for column_name, field in (("state", SlaveMessageDelivery.state), ("lease_expires_at", SlaveMessageDelivery.lease_expires_at), ("owner_token", SlaveMessageDelivery.owner_token))
                    if column_name not in delivery_columns
                )
                if delivery_migrations:
                    migrate(*delivery_migrations)
            if "msglog" in table_names:
                current_database.execute_sql(f"CREATE INDEX IF NOT EXISTS {DatabaseManager._MSGLOG_REPLAY_SOURCE_INDEX} ON msglog (slave_origin_uid, time, master_msg_id)")
            if "chatassoc" in table_names:
                DatabaseManager._deduplicate_by_key(ChatAssoc, (ChatAssoc.slave_uid,))
                current_database.execute_sql(f"CREATE UNIQUE INDEX IF NOT EXISTS {DatabaseManager._CHAT_ASSOC_SLAVE_INDEX} ON chatassoc (slave_uid)")
            if "topicassoc" in table_names:
                DatabaseManager._deduplicate_by_key(TopicAssoc, (TopicAssoc.slave_uid,))
                DatabaseManager._deduplicate_by_key(TopicAssoc, (TopicAssoc.topic_chat_id, TopicAssoc.message_thread_id))
                current_database.execute_sql(f"CREATE UNIQUE INDEX IF NOT EXISTS {DatabaseManager._TOPIC_ASSOC_SLAVE_INDEX} ON topicassoc (slave_uid)")
                current_database.execute_sql(f"CREATE UNIQUE INDEX IF NOT EXISTS {DatabaseManager._TOPIC_ASSOC_TOPIC_THREAD_INDEX} ON topicassoc (topic_chat_id, message_thread_id)")
            if "historymigrationentry" in table_names:
                DatabaseManager._deduplicate_history_positions()
                current_database.execute_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {DatabaseManager._HISTORY_TARGET_POSITION_WITHOUT_THREAD_INDEX} "
                    "ON historymigrationentry (slave_chat_id, target_chat_id, position) WHERE message_thread_id IS NULL"
                )
                current_database.execute_sql(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {DatabaseManager._HISTORY_TARGET_POSITION_WITH_THREAD_INDEX} "
                    "ON historymigrationentry (slave_chat_id, target_chat_id, message_thread_id, position) "
                    "WHERE message_thread_id IS NOT NULL"
                )

    @staticmethod
    def _deduplicate_by_key(model: type[Model], fields: tuple) -> None:
        primary_key = model._meta.primary_key
        if primary_key is False:
            raise ValueError(f"{model.__name__} has no primary key")
        candidate_ids = {getattr(row, primary_key.name) for row in model.select(primary_key)}
        duplicate_ids = candidate_ids - DatabaseManager._newest_primary_key_values(model, fields, candidate_ids)
        if duplicate_ids:
            model.delete().where(primary_key.in_(duplicate_ids)).execute()

    @staticmethod
    def _deduplicate_slave_chat_info() -> None:
        DatabaseManager._deduplicate_by_key(
            SlaveChatInfo,
            (SlaveChatInfo.slave_channel_id, SlaveChatInfo.slave_chat_uid, SlaveChatInfo.slave_chat_group_id),
        )

    @staticmethod
    def _historic_identity_keys(model: type[Model]) -> tuple[tuple, ...]:
        if model is ChatAssoc:
            return ((ChatAssoc.slave_uid,),)
        if model is TopicAssoc:
            return (
                (TopicAssoc.slave_uid,),
                (TopicAssoc.topic_chat_id, TopicAssoc.message_thread_id),
            )
        if model is HistoryMigrationEntry:
            return (
                (
                    HistoryMigrationEntry.slave_chat_id,
                    HistoryMigrationEntry.target_chat_id,
                    HistoryMigrationEntry.message_thread_id,
                    HistoryMigrationEntry.position,
                ),
            )
        return ()

    @staticmethod
    def _newest_primary_key_values(model: type[Model], fields: tuple, candidates: set[object]) -> set[object]:
        primary_key = model._meta.primary_key
        if primary_key is False:
            raise ValueError(f"{model.__name__} has no primary key")
        seen = set()
        canonical_ids = set()
        for row in model.select(primary_key, *fields).where(primary_key.in_(candidates)).order_by(primary_key.desc()):
            key = tuple(getattr(row, field.name) for field in fields)
            if key not in seen:
                seen.add(key)
                canonical_ids.add(getattr(row, primary_key.name))
        return canonical_ids

    @classmethod
    def _canonical_historic_primary_key_values(cls, model: type[Model]) -> set[object] | None:
        identity_keys = cls._historic_identity_keys(model)
        if not identity_keys:
            return None
        primary_key = model._meta.primary_key
        if primary_key is False:
            raise ValueError(f"{model.__name__} has no primary key")
        canonical_ids = {getattr(row, primary_key.name) for row in model.select(primary_key)}
        for fields in identity_keys:
            canonical_ids = cls._newest_primary_key_values(model, fields, canonical_ids)
        return canonical_ids

    @staticmethod
    def _deduplicate_history_positions() -> None:
        DatabaseManager._deduplicate_by_key(
            HistoryMigrationEntry,
            (
                HistoryMigrationEntry.slave_chat_id,
                HistoryMigrationEntry.target_chat_id,
                HistoryMigrationEntry.message_thread_id,
                HistoryMigrationEntry.position,
            ),
        )

    @classmethod
    @contextmanager
    def _sqlite_import_lifecycle_lock(cls, current_database):
        current_database.execute_sql("SELECT pg_advisory_lock(%s)", (cls._SQLITE_IMPORT_LOCK_KEY,))
        try:
            yield
        finally:
            current_database.execute_sql("SELECT pg_advisory_unlock(%s)", (cls._SQLITE_IMPORT_LOCK_KEY,))

    def _initialize_postgresql(self, base_path: Path) -> None:
        current_database = database.obj
        with self._sqlite_import_lifecycle_lock(current_database):
            sqlite_path = base_path / "tgdata.db"
            migrated_path = sqlite_path.with_suffix(".db.migrated")
            if sqlite_path.exists() and migrated_path.exists() and not os.path.samefile(sqlite_path, migrated_path):
                raise RuntimeError(
                    "SQLite-to-PostgreSQL migration finalization collision: both tgdata.db and tgdata.db.migrated exist with different contents; "
                    "preserving both files. Resolve the conflict before restarting."
                )

            target_initialized = ChatAssoc.table_exists()
            if sqlite_path.exists() or migrated_path.exists():
                self._reject_legacy_outbound_target_data(current_database)
            if not target_initialized and sqlite_path.exists():
                self._migrate_from_sqlite(sqlite_path, finalize_source=True)
            elif not target_initialized and migrated_path.exists():
                self._migrate_from_sqlite(migrated_path, finalize_source=False)
            elif target_initialized and sqlite_path.exists():
                self._finalize_completed_sqlite_import(sqlite_path)
                self._create()
            else:
                self._create()

    @staticmethod
    def _sqlite_snapshot_value(value: object) -> object:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        isoformat = getattr(value, "isoformat", None)
        if callable(isoformat):
            return {"isoformat": isoformat()}
        return {"string": str(value)}

    @staticmethod
    def _sqlite_dict_row_values(row: object, column_names: tuple[str, ...]) -> tuple[object, ...]:
        if not isinstance(row, Mapping):
            raise TypeError("Peewee dictionary query returned a non-mapping row.")
        return tuple(row[column_name] for column_name in column_names)

    @classmethod
    def _sqlite_source_snapshot(cls, source_database, models: tuple[type[Model], ...]) -> _SQLiteImportSnapshot:
        table_names = set(source_database.get_tables())
        projections = []
        serialized_projections = []
        for model in models:
            if model._meta.table_name not in table_names:
                column_names: tuple[str, ...] = ()
                rows: tuple[tuple[object, ...], ...] = ()
            else:
                source_columns = {column.name for column in source_database.get_columns(model._meta.table_name)}
                fields = tuple(field for field in model._meta.sorted_fields if field.column_name in source_columns)
                column_names = tuple(field.column_name for field in fields)
                canonical_primary_keys = cls._canonical_historic_primary_key_values(model)
                primary_key = model._meta.primary_key
                query = model.select(*fields)
                if canonical_primary_keys is not None:
                    if primary_key is False:
                        raise ValueError(f"{model.__name__} has no primary key")
                    query = query.where(primary_key.in_(canonical_primary_keys))
                rows = tuple(DatabaseManager._sqlite_dict_row_values(row, column_names) for row in query.dicts())
            projections.append(_SQLiteSourceProjection(model, column_names, rows))
            serialized_projections.append(
                {
                    "table": model._meta.table_name,
                    "columns": column_names,
                    "rows": sorted(
                        (tuple(cls._sqlite_snapshot_value(value) for value in row) for row in rows),
                        key=lambda row: json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                    ),
                }
            )
        serialized_snapshot = json.dumps(serialized_projections, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return _SQLiteImportSnapshot(tuple(projections), sha256(serialized_snapshot.encode()).hexdigest())

    @classmethod
    @contextmanager
    def _sqlite_source_fence(cls, sqlite_path: Path, models: tuple[type[Model], ...]):
        source_database = SqliteDatabase(str(sqlite_path))
        source_database.connect()
        try:
            # A reserved lock holds one immutable source view until finalization.
            with source_database.atomic("IMMEDIATE"):
                with source_database.bind_ctx(models):
                    cls._reject_legacy_outbound_source_data(source_database)
                    yield cls._sqlite_source_snapshot(source_database, models), source_database
        finally:
            source_database.close()

    @classmethod
    def _target_matches_sqlite_snapshot(cls, snapshot: _SQLiteImportSnapshot) -> bool:
        def serialized_rows(rows: tuple[tuple[object, ...], ...]) -> list[str]:
            return sorted(
                json.dumps(
                    tuple(cls._sqlite_snapshot_value(value) for value in row),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                for row in rows
            )

        for projection in snapshot.projections:
            if not projection.column_names:
                if projection.model.select().count() != len(projection.rows):
                    return False
                continue
            fields = [field for field in projection.model._meta.sorted_fields if field.column_name in projection.column_names]
            target_rows = tuple(DatabaseManager._sqlite_dict_row_values(row, projection.column_names) for row in projection.model.select(*fields).dicts())
            if serialized_rows(target_rows) != serialized_rows(projection.rows):
                return False
        return True

    @classmethod
    def _reconcile_postgresql_import_sequences(cls, models: tuple[type[Model], ...]) -> None:
        for model in models:
            primary_key = model._meta.primary_key
            if not isinstance(primary_key, AutoField):
                continue
            database.execute_sql(
                f"SELECT setval(pg_get_serial_sequence({database.obj.param}, {database.obj.param}), "
                f'COALESCE(MAX("{primary_key.column_name}"), 1), MAX("{primary_key.column_name}") IS NOT NULL) '
                f'FROM "{model._meta.table_name}"',
                (model._meta.table_name, primary_key.column_name),
            )

    @classmethod
    def _record_sqlite_import_provenance(cls, snapshot: _SQLiteImportSnapshot) -> None:
        placeholder = database.obj.param
        database.execute_sql(f'CREATE TABLE IF NOT EXISTS "{cls._SQLITE_IMPORT_PROVENANCE_TABLE}" (snapshot_identity TEXT PRIMARY KEY)')
        database.execute_sql(
            f'INSERT INTO "{cls._SQLITE_IMPORT_PROVENANCE_TABLE}" (snapshot_identity) VALUES ({placeholder})',
            (snapshot.identity,),
        )

    @classmethod
    def _has_sqlite_import_provenance(cls, snapshot: _SQLiteImportSnapshot) -> bool:
        if cls._SQLITE_IMPORT_PROVENANCE_TABLE not in database.get_tables():
            return False
        placeholder = database.obj.param
        return (
            database.execute_sql(
                f' SELECT 1 FROM "{cls._SQLITE_IMPORT_PROVENANCE_TABLE}" WHERE snapshot_identity = {placeholder}',
                (snapshot.identity,),
            ).fetchone()
            is not None
        )

    @classmethod
    def _reject_legacy_outbound_source_data(cls, source_database) -> None:
        table_names = set(source_database.get_tables())
        legacy_table_names = cls._validate_legacy_outbound_schema(source_database, table_names)
        row_counts = {table_name: int(source_database.execute_sql(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]) for table_name in legacy_table_names}
        if any(row_counts.values()):
            raise RuntimeError(
                "Legacy durable outbound data detected in SQLite import source: automatic replay is disabled. "
                "Import aborted before target initialization or source migration; "
                f"workflows={row_counts.get('outboundworkflow', 0)} tasks={row_counts.get('outboundtask', 0)}."
            )

    @classmethod
    def _reject_legacy_outbound_target_data(cls, target_database) -> None:
        table_names = set(target_database.get_tables())
        legacy_table_names = cls._validate_legacy_outbound_schema(target_database, table_names)
        row_counts = {table_name: int(target_database.execute_sql(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]) for table_name in legacy_table_names}
        if any(row_counts.values()):
            raise RuntimeError(
                "Legacy durable outbound data detected in PostgreSQL import target: automatic replay is disabled. "
                "Import aborted before target initialization or source migration; "
                f"workflows={row_counts.get('outboundworkflow', 0)} tasks={row_counts.get('outboundtask', 0)}."
            )

    @staticmethod
    def _create_sqlite_archive(source_database, archive_path: Path) -> None:
        source_connection = sqlite3.connect(source_database.database)
        archive_connection = sqlite3.connect(str(archive_path))
        try:
            source_connection.backup(archive_connection)
            if archive_connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("SQLite archive integrity check failed")
        finally:
            archive_connection.close()
            source_connection.close()

    @classmethod
    def _finalize_sqlite_source(cls, source_database, sqlite_path: Path) -> None:
        migrated_path = sqlite_path.with_suffix(".db.migrated")
        if migrated_path.exists():
            raise RuntimeError("SQLite-to-PostgreSQL migration finalization collision: tgdata.db.migrated already exists; preserving both files. Resolve the conflict before restarting.")
        descriptor, archive_name = tempfile.mkstemp(prefix=f".{migrated_path.name}.", suffix=".tmp", dir=sqlite_path.parent)
        os.close(descriptor)
        archive_path = Path(archive_name)
        try:
            try:
                cls._create_sqlite_archive(source_database, archive_path)
            except (OSError, RuntimeError, sqlite3.Error) as error:
                raise RuntimeError(f"SQLite-to-PostgreSQL migration finalization failed; source remains at {sqlite_path}") from error
            try:
                os.link(archive_path, migrated_path)
            except FileExistsError as error:
                raise RuntimeError(
                    "SQLite-to-PostgreSQL migration finalization collision: tgdata.db.migrated already exists; preserving both files. Resolve the conflict before restarting."
                ) from error
            except OSError as error:
                raise RuntimeError(f"SQLite-to-PostgreSQL migration finalization failed; source remains at {sqlite_path}") from error
        finally:
            archive_path.unlink(missing_ok=True)
        for source_path in (sqlite_path, sqlite_path.with_name(f"{sqlite_path.name}-wal"), sqlite_path.with_name(f"{sqlite_path.name}-shm")):
            source_path.unlink(missing_ok=True)

    def _finalize_completed_sqlite_import(self, sqlite_path: Path) -> None:
        models = (ChatAssoc, TopicAssoc, SlaveChatInfo, MsgLog, HistoryMigrationEntry, MsgLogIngestionScan, SlaveMessageDelivery)
        with self._sqlite_source_fence(sqlite_path, models) as (snapshot, source_database):
            with database.obj.bind_ctx(models):
                if not self._has_sqlite_import_provenance(snapshot):
                    raise RuntimeError(
                        "SQLite-to-PostgreSQL migration restart conflict: target import provenance does not match tgdata.db; "
                        "preserving the source. Resolve the target/source conflict before restarting."
                    )
                if not self._target_matches_sqlite_snapshot(snapshot):
                    raise RuntimeError(
                        "SQLite-to-PostgreSQL migration restart conflict: target data does not exactly match tgdata.db; preserving the source. Resolve the target/source conflict before restarting."
                    )
                self._finalize_sqlite_source(source_database, sqlite_path)
        self.logger.info("SQLite-to-PostgreSQL migration source finalization completed; source renamed to %s", sqlite_path.with_suffix(".db.migrated"))

    def _migrate_from_sqlite(self, sqlite_path: Path, *, finalize_source: bool) -> None:
        from peewee import chunked

        self.logger.info("Detected existing SQLite database. Migrating to PostgreSQL.")
        models = (ChatAssoc, TopicAssoc, SlaveChatInfo, MsgLog, HistoryMigrationEntry, MsgLogIngestionScan, SlaveMessageDelivery)
        with self._sqlite_source_fence(sqlite_path, models) as (snapshot, source_database):
            with database.obj.bind_ctx(models):
                with database.atomic():
                    self._create()
                    for projection in snapshot.projections:
                        rows = [dict(zip(projection.column_names, row)) for row in projection.rows]
                        for batch in chunked(rows, 500):
                            projection.model.insert_many(batch).execute()
                    self._reconcile_postgresql_import_sequences(models)
                    self._record_sqlite_import_provenance(snapshot)
                    if not self._target_matches_sqlite_snapshot(snapshot):
                        raise RuntimeError("SQLite-to-PostgreSQL migration verification failed: target content differs from the source snapshot")

            if finalize_source:
                self._finalize_sqlite_source(source_database, sqlite_path)
        if finalize_source:
            self.logger.info("SQLite-to-PostgreSQL migration completed; source renamed to %s", sqlite_path.with_suffix(".db.migrated"))
        else:
            self.logger.info("SQLite-to-PostgreSQL migration completed from preserved source %s", sqlite_path)

    @staticmethod
    def _legacy_table_model(table_name: str, current_database) -> type[Model]:
        meta = type("Meta", (), {"database": current_database, "table_name": table_name})
        return type("LegacyOutboundTable", (Model,), {"Meta": meta})

    @staticmethod
    def _legacy_column_type(data_type: str) -> str:
        normalized = data_type.lower()
        if "int" in normalized or normalized in {"serial", "bigserial"}:
            return "integer"
        if "bool" in normalized:
            return "boolean"
        if "date" in normalized or "time" in normalized:
            return "datetime"
        if "char" in normalized or "text" in normalized:
            return "text"
        return normalized

    @staticmethod
    def _legacy_column_default(default) -> Optional[str]:
        if default is None:
            return None
        normalized = str(default).strip().lower()
        while normalized.startswith("(") and normalized.endswith(")"):
            normalized = normalized[1:-1].strip()
        normalized = normalized.replace("::timestamp without time zone", "").replace("::timestamp with time zone", "")
        return normalized.replace("::text", "").strip()

    @staticmethod
    def _legacy_auto_pk_default(default) -> str:
        raw_default = str(default).strip().lower()
        while raw_default.startswith("(") and raw_default.endswith(")"):
            raw_default = raw_default[1:-1].strip()
        return raw_default

    @classmethod
    def _legacy_auto_pk_sequence(cls, default, table_name: str) -> bool:
        raw_default = cls._legacy_auto_pk_default(default)
        identifier = r'(?:(?P<schema>"[^"]+"|[a-z_][a-z0-9_]*)\s*\.\s*)?(?P<sequence>"[^"]+"|[a-z_][a-z0-9_]*)'
        match = re.fullmatch(rf"nextval\s*\(\s*'{identifier}'\s*::\s*regclass\s*\)", raw_default, flags=re.IGNORECASE)
        if match is None:
            return False
        schema = match.group("schema")
        if schema is not None and schema.strip('"').lower() != "public":
            return False
        return match.group("sequence").strip('"').lower() == f"{table_name}_id_seq"

    @classmethod
    def _legacy_default_category(cls, current_database, table_name: str, column_name: str, data_type: str, primary_key: bool, default) -> str:
        expected_category = cls._LEGACY_OUTBOUND_DEFAULT_CATEGORIES[table_name].get(column_name, "none")
        if expected_category == "auto_pk" and column_name == "id" and primary_key and data_type == "integer":
            if default is None:
                return "auto_pk" if isinstance(current_database, SqliteDatabase) else "none"
            if isinstance(current_database, PostgresqlDatabase) and cls._legacy_auto_pk_sequence(default, table_name):
                return "auto_pk"
            return f"invalid:{cls._legacy_auto_pk_default(default)}"
        normalized_default = cls._legacy_column_default(default)
        if normalized_default is None:
            return "none"
        if normalized_default == "current_timestamp":
            return "current_timestamp"
        if expected_category != "auto_pk" or column_name != "id" or not primary_key or data_type != "integer":
            return f"invalid:{normalized_default}"
        return f"invalid:{normalized_default}"

    @classmethod
    def _legacy_outbound_schema_error(cls, current_database, table_name: str) -> Optional[str]:
        expected_columns = tuple(
            (
                name,
                "integer" if isinstance(current_database, SqliteDatabase) and data_type == "boolean" else data_type,
                null,
                primary_key,
                cls._LEGACY_OUTBOUND_DEFAULT_CATEGORIES[table_name].get(name, "none"),
            )
            for name, data_type, null, primary_key in cls._LEGACY_OUTBOUND_COLUMNS[table_name]
        )
        actual_columns = tuple(
            (
                column.name,
                cls._legacy_column_type(column.data_type),
                column.null,
                column.primary_key,
                cls._legacy_default_category(current_database, table_name, column.name, cls._legacy_column_type(column.data_type), column.primary_key, column.default),
            )
            for column in current_database.get_columns(table_name)
        )
        if actual_columns != expected_columns:
            return f"column signature for {table_name} does not match the historical durable outbound schema"
        if table_name != "outboundtask":
            return None
        actual_indexes = cls._legacy_outbound_task_indexes(current_database)
        if set(actual_indexes) != set(cls._LEGACY_OUTBOUND_TASK_INDEXES):
            return "index signature for outboundtask does not match the historical durable outbound schema"
        return None

    @staticmethod
    def _legacy_outbound_task_indexes(current_database) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
        indexes = tuple((index.name, tuple(index.columns), index.unique) for index in current_database.get_indexes("outboundtask"))
        if isinstance(current_database, PostgresqlDatabase):
            # PostgreSQL exposes the primary-key constraint as an index; SQLite does not.
            indexes = tuple(index for index in indexes if index != ("outboundtask_pkey", ("id",), True))
        return indexes

    @classmethod
    def _validate_legacy_outbound_schema(cls, current_database, table_names: set[str]) -> tuple[str, ...]:
        legacy_tables = tuple(table_name for table_name in cls._LEGACY_OUTBOUND_TABLES if table_name in table_names)
        if legacy_tables and len(legacy_tables) != len(cls._LEGACY_OUTBOUND_TABLES):
            raise RuntimeError("Legacy outbound partial-schema collision: historical workflow and task tables must be present together; refusing to discard table")
        for table_name in legacy_tables:
            error = cls._legacy_outbound_schema_error(current_database, table_name)
            if error is not None:
                raise RuntimeError(f"Legacy outbound schema collision: {error}; refusing to discard table")
        return legacy_tables

    @classmethod
    def _acquire_legacy_outbound_lock(cls, current_database) -> None:
        if isinstance(current_database, PostgresqlDatabase):
            current_database.execute_sql("SELECT pg_advisory_xact_lock(%s)", (cls._LEGACY_OUTBOUND_LOCK_KEY,))

    def _retire_legacy_outbound_tables(self) -> None:
        self._retire_legacy_outbound_tables_for_database(database.obj)

    @classmethod
    def _retire_legacy_outbound_tables_for_database(cls, current_database) -> None:
        transaction_arguments: Tuple[str, ...] = ()
        if isinstance(current_database, SqliteDatabase):
            transaction_arguments = ("IMMEDIATE",)
        elif not isinstance(current_database, PostgresqlDatabase):
            raise TypeError(f"Unsupported database backend: {type(current_database).__name__}")

        with current_database.atomic(*transaction_arguments):
            cls._acquire_legacy_outbound_lock(current_database)
            table_names = set(current_database.get_tables())
            legacy_table_names = cls._validate_legacy_outbound_schema(current_database, table_names)
            if not legacy_table_names:
                return
            row_counts = {table_name: int(current_database.execute_sql(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]) for table_name in legacy_table_names}
            if any(row_counts.values()):
                raise RuntimeError(
                    "Legacy durable outbound data detected: automatic replay is disabled. "
                    "Export or explicitly retire outboundworkflow/outboundtask rows before restarting; "
                    f"workflows={row_counts.get('outboundworkflow', 0)} tasks={row_counts.get('outboundtask', 0)}."
                )
            legacy_tables = [cls._legacy_table_model(table_name, current_database) for table_name in legacy_table_names]
            for legacy_table in reversed(legacy_tables):
                current_database.drop_tables([legacy_table], safe=False)
