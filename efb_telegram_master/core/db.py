# coding=utf-8

import logging
from typing import TYPE_CHECKING, Optional

from efb_telegram_master.persistence.database_initializer import DatabaseInitializer
from efb_telegram_master.persistence.database_observability import DatabaseMetrics, observe_database_method
from efb_telegram_master.persistence.repository_registry import Repositories

if TYPE_CHECKING:
    from efb_telegram_master import TelegramChannel


class DatabaseManager:
    """Own one prepared channel database and expose its repositories."""

    logger = logging.getLogger(__name__)

    def __init__(self, channel: "TelegramChannel"):
        self.channel = channel
        self._metrics: Optional[DatabaseMetrics] = None
        self.logger.debug("Loading database...")
        self.current_database, self._base_path = DatabaseInitializer(channel, self.logger).initialize()
        repositories = Repositories(self.current_database, channel.channel_id)
        self.chat_associations = repositories.chat_associations
        self.slave_chat_info = repositories.slave_chat_info
        self.slave_message_deliveries = repositories.slave_message_deliveries
        self.msglogs = repositories.msglogs
        self.history_migrations = repositories.history_migrations
        self.msglog_ingestion = repositories.msglog_ingestion

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
