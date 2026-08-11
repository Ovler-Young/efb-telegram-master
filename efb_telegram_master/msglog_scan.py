"""Thread scheduling for durable MTProto MsgLog ingestion."""

from __future__ import annotations

import logging
import threading
import uuid

from .msglog_ingestion import MsgLogIngestionService
from .utils import TelegramChatID


class MsgLogScanScheduler:
    """Start and resume one lease-protected scan per bound source group."""

    def __init__(self, runtime, mtproto, ingestion, chat_associations, logger: logging.Logger) -> None:
        self.runtime = runtime
        self.mtproto = mtproto
        self.ingestion = ingestion
        self.chat_associations = chat_associations
        self.logger = logger
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}

    def schedule(self, source_chat_id: int) -> str:
        if not self.mtproto.enabled or not self.mtproto.connected:
            return "unavailable"
        with self._lock:
            existing = self._threads.get(source_chat_id)
            if existing is not None and existing.is_alive():
                return "already running"
            scan = self.ingestion.get_or_create_scan(source_chat_id, self.mtproto.config.scan_ceiling)
            if scan.status == "complete":
                return "already complete"
            thread = threading.Thread(target=self._run, args=(source_chat_id,), daemon=True, name=f"MsgLogIngestion-{source_chat_id}")
            self._threads[source_chat_id] = thread
            thread.start()
            return "resumed" if scan.scanned_count else "started"

    def _run(self, source_chat_id: int) -> None:
        try:
            self.runtime.async_runtime.call(MsgLogIngestionService(self.ingestion, self.chat_associations, self.mtproto).run(source_chat_id, lease_owner=str(uuid.uuid4())))
        except Exception:
            self.logger.exception("MsgLog ingestion worker failed for group %s", source_chat_id)

    def resume(self) -> None:
        try:
            scans = self.ingestion.get_resumable_scans()
        except Exception as error:
            self.logger.warning("Failed to load resumable MsgLog ingestions (%s).", type(error).__name__)
            return
        for scan in scans:
            source_chat_id = int(scan.source_chat_id)
            if self.chat_associations.get_topic_slaves(TelegramChatID(source_chat_id)):
                self.schedule(source_chat_id)
