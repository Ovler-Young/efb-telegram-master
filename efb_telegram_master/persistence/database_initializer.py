import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ehforwarderbot import utils
from peewee import PostgresqlDatabase, SqliteDatabase

from ..legacy_outbound_retirement import LegacyOutboundRetirement
from .schema_migration import DatabaseSchemaMigrator
from .sqlite_postgresql_import import SQLitePostgresqlImportCoordinator

if TYPE_CHECKING:
    from .. import TelegramChannel


class DatabaseInitializer:
    """Construct and prepare one channel database before repository use."""

    def __init__(self, channel: "TelegramChannel", logger: logging.Logger) -> None:
        self.channel = channel
        self.logger = logger

    def initialize(self) -> tuple[Any, Path]:
        base_path = utils.get_data_path(self.channel.channel_id)
        database = self._build_database(base_path)
        connected = False
        try:
            database.connect()
            connected = True
            self.logger.debug("Database loaded.")
            self.logger.debug("Checking database migration...")
            schema = DatabaseSchemaMigrator(database)
            if isinstance(database, PostgresqlDatabase):
                SQLitePostgresqlImportCoordinator(database, schema, self.logger).initialize(base_path)
            else:
                schema.create()
            self.logger.debug("Database migration finished...")
            LegacyOutboundRetirement(database).retire_tables()
        except BaseException:
            if connected:
                self._close(database)
            raise
        return database, base_path

    def _build_database(self, base_path: Path) -> Any:
        db_config = self.channel.config.database
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

    def _close(self, database: Any) -> None:
        try:
            stop = getattr(database, "stop", None)
            if callable(stop):
                stop()
            database.close()
        except BaseException:
            try:
                self.logger.exception("Failed to close database after database initialization failed.")
            except BaseException:
                pass
