# coding=utf-8

import logging
from typing import TYPE_CHECKING, Optional

from ehforwarderbot import utils
from peewee import PostgresqlDatabase, SqliteDatabase

from .legacy_outbound_retirement import LegacyOutboundRetirement
from .models import bind_models_to_proxy, database
from .persistence.database_observability import DatabaseMetrics, observe_database_method
from .persistence.repository_registry import Repositories
from .persistence.schema_migration import DatabaseSchemaMigrator
from .persistence.sqlite_postgresql_import import SQLitePostgresqlImportCoordinator

if TYPE_CHECKING:
    from . import TelegramChannel


class DatabaseManager:
    """Connect one channel database and compose its repositories."""

    logger = logging.getLogger(__name__)

    def __init__(self, channel: "TelegramChannel"):
        self.channel = channel
        self._metrics: Optional[DatabaseMetrics] = None
        base_path = utils.get_data_path(channel.channel_id)
        self.logger.debug("Loading database...")
        self.current_database = self._build_database(channel, base_path)
        database.initialize(self.current_database)
        bind_models_to_proxy()
        repositories = Repositories(self.current_database, channel.channel_id)
        self.chat_associations = repositories.chat_associations
        self.slave_chat_info = repositories.slave_chat_info
        self.slave_message_deliveries = repositories.slave_message_deliveries
        self.msglogs = repositories.msglogs
        self.history_migrations = repositories.history_migrations
        self.msglog_ingestion = repositories.msglog_ingestion
        connected = False
        try:
            database.connect()
            connected = True
            self.logger.debug("Database loaded.")
            self.logger.debug("Checking database migration...")
            schema = DatabaseSchemaMigrator(self.current_database)
            if isinstance(self.current_database, PostgresqlDatabase):
                SQLitePostgresqlImportCoordinator(self.current_database, schema, self.logger).initialize(base_path)
            else:
                schema.create()
            self.logger.debug("Database migration finished...")
            LegacyOutboundRetirement(self.current_database).retire_tables()
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

    @staticmethod
    def _build_database(channel: "TelegramChannel", base_path):
        db_config = channel.config.get("database", {})
        if db_config.get("type", "sqlite") == "postgresql":
            from playhouse.postgres_ext import PooledPostgresqlExtDatabase

            return PooledPostgresqlExtDatabase(
                db_config.get("database", "efb_telegram"),
                host=db_config.get("host", "localhost"),
                port=db_config.get("port", 5432),
                user=db_config.get("user", "postgres"),
                password=db_config.get("password", ""),
                max_connections=db_config.get("max_connections", 8),
                stale_timeout=db_config.get("stale_timeout", 300),
                options=db_config.get("options", "-c timezone=UTC"),
            )
        return SqliteDatabase(str(base_path / "tgdata.db"), pragmas={"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 5000}, check_same_thread=False)

    def set_metrics(self, metrics: DatabaseMetrics) -> None:
        self._metrics = metrics
        for repository in (self.chat_associations, self.slave_chat_info, self.slave_message_deliveries, self.msglogs, self.history_migrations, self.msglog_ingestion):
            repository._metrics = metrics

    @observe_database_method("stop_worker")
    def stop_worker(self) -> None:
        stop = getattr(self.current_database, "stop", None)
        if callable(stop):
            stop()
        self.current_database.close()
