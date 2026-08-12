# coding=utf-8

import logging
import re
from typing import TYPE_CHECKING, Optional, Tuple

from ehforwarderbot import utils
from peewee import Model, PostgresqlDatabase, SqliteDatabase, fn

from .chat_association_repository import ChatAssociationRepository
from .database_observability import DatabaseMetrics, observe_database_method
from .history_migration_repository import HistoryMigrationRepository
from .models import ChatAssoc, HistoryMigrationEntry, MsgLog, MsgLogIngestionScan, SlaveChatInfo, TopicAssoc, database
from .msglog_ingestion_repository import MsgLogIngestionRepository
from .msglog_repository import MsgLogRepository
from .slave_chat_info_repository import SlaveChatInfoRepository

if TYPE_CHECKING:
    from . import TelegramChannel


class DatabaseManager:
    logger = logging.getLogger(__name__)
    _LEGACY_OUTBOUND_TABLES = ("outboundworkflow", "outboundtask")
    _LEGACY_OUTBOUND_LOCK_KEY = 681_774_240_616_480_003
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
        for repository in (self.chat_associations, self.slave_chat_info, self.msglogs, self.history_migrations, self.msglog_ingestion):
            repository._metrics = metrics

    @observe_database_method("stop_worker")
    def stop_worker(self) -> None:
        stop = getattr(database.obj, "stop", None)
        if callable(stop):
            stop()
        database.close()

    @staticmethod
    def _create() -> None:
        database.create_tables([ChatAssoc, MsgLog, SlaveChatInfo, TopicAssoc, HistoryMigrationEntry, MsgLogIngestionScan])
        DatabaseManager._ensure_msglog_provenance()

    @staticmethod
    def _ensure_msglog_provenance() -> None:
        current_database = database.obj
        transaction_arguments: Tuple[str, ...] = ()
        if isinstance(current_database, SqliteDatabase):
            transaction_arguments = ("IMMEDIATE",)
        elif not isinstance(current_database, PostgresqlDatabase):
            raise TypeError(f"Unsupported database backend: {type(current_database).__name__}")

        with current_database.atomic(*transaction_arguments):
            if isinstance(current_database, PostgresqlDatabase):
                current_database.execute_sql('LOCK TABLE "msglog" IN ACCESS EXCLUSIVE MODE')
            column_names = {column.name for column in current_database.get_columns(MsgLog._meta.table_name)}
            if "provenance" not in column_names:
                current_database.execute_sql('ALTER TABLE "msglog" ADD COLUMN "provenance" TEXT NOT NULL DEFAULT \'live\'')

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
            legacy_tables = [cls._legacy_table_model(table_name, current_database) for table_name in legacy_table_names]

            row_counts = {table._meta.table_name: int(table.select(fn.COUNT(table._meta.primary_key)).scalar()) for table in legacy_tables}
            cls.logger.warning(
                "Discarding obsolete durable outbound queue rows without resumption: workflows=%d tasks=%d",
                row_counts.get("outboundworkflow", 0),
                row_counts.get("outboundtask", 0),
            )
            for legacy_table in reversed(legacy_tables):
                current_database.drop_tables([legacy_table], safe=False)
