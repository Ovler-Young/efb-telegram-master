# coding=utf-8

import logging
from typing import TYPE_CHECKING, Optional, Tuple

from ehforwarderbot import utils
from peewee import PostgresqlDatabase, SqliteDatabase

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
    _LEGACY_OUTBOUND_TABLES = ("outbound_workflow", "outbound_task")
    _LEGACY_OUTBOUND_STATES = (
        "waiting_dependency",
        "queued",
        "leased",
        "in_flight",
        "sent_pending_log",
        "completed",
        "skipped",
        "dead",
    )

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
        database.connect()
        self.logger.debug("Database loaded.")
        self.logger.debug("Checking database migration...")
        self._create()
        self.logger.debug("Database migration finished...")
        self._observe_legacy_outbound_rows()

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

    def _observe_legacy_outbound_rows(self) -> None:
        table_names = set(database.get_tables())
        workflow_table, task_table = self._LEGACY_OUTBOUND_TABLES
        workflow_count = 0
        task_count = 0
        state_counts = {state: 0 for state in self._LEGACY_OUTBOUND_STATES}
        if workflow_table in table_names:
            workflow_count = int(database.execute_sql(f'SELECT COUNT(*) FROM "{workflow_table}"').fetchone()[0])
        if task_table in table_names:
            task_rows = database.execute_sql(f'SELECT state, COUNT(*) FROM "{task_table}" GROUP BY state').fetchall()
            for state, count in task_rows:
                if state in state_counts:
                    state_counts[state] = int(count)
            task_count = sum(int(count) for _state, count in task_rows)
        if workflow_count or task_count:
            state_summary = ", ".join(f"{state}={state_counts[state]}" for state in self._LEGACY_OUTBOUND_STATES)
            self.logger.warning("Retained legacy outbound rows: workflows=%d tasks=%d %s", workflow_count, task_count, state_summary)
