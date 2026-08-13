"""Thread scheduling for durable MTProto MsgLog ingestion."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeoutError

from .msglog_ingestion import MsgLogIngestionService
from .utils import TelegramChatID


class MsgLogScanShutdownTimeout(RuntimeError):
    """One or more ingestion scans retained MTProto work after shutdown began."""


class MsgLogScanScheduler:
    """Own one non-daemon scan worker per lease-protected source group."""

    DEFAULT_JOIN_TIMEOUT = 5.0

    def __init__(self, runtime, mtproto, ingestion, chat_associations, logger: logging.Logger) -> None:
        self.runtime, self.mtproto, self.ingestion, self.chat_associations, self.logger = runtime, mtproto, ingestion, chat_associations, logger
        self._lock = threading.Lock()
        self._connect_lock: asyncio.Lock | None = None
        self._stopping = False
        self._stop_event = threading.Event()
        self._threads: dict[int, threading.Thread] = {}
        self._retiring_threads: set[threading.Thread] = set()
        self._owners: dict[int, str] = {}

    def schedule(self, source_chat_id: int) -> str:
        with self._lock:
            if self._stopping:
                return "stopping"
            self._retiring_threads = {thread for thread in self._retiring_threads if thread.is_alive()}
            if not self.mtproto.enabled:
                return "unavailable"
            existing = self._threads.get(source_chat_id)
            if existing is not None and existing.is_alive():
                return "already running"
            scan = self.ingestion.get_or_create_scan(source_chat_id, self.mtproto.config.scan_ceiling)
            if scan.status == "complete":
                return "already complete"
            owner = str(uuid.uuid4())
            thread = threading.Thread(target=self._run, args=(source_chat_id, owner), name=f"MsgLogIngestion-{source_chat_id}")
            self._threads[source_chat_id], self._owners[source_chat_id] = thread, owner
            self._retiring_threads.add(thread)
            thread.start()
            return "resumed" if scan.scanned_count else "started"

    def stop(self, join_timeout: float = DEFAULT_JOIN_TIMEOUT) -> tuple[BaseException, ...]:
        deadline = time.monotonic() + join_timeout
        with self._lock:
            admission_fence_needed = not self._stopping
            self._stopping = True
            self._stop_event.set()
            active_workers = tuple(self._threads.values())
            workers = active_workers + tuple(thread for thread in self._retiring_threads if thread not in active_workers)
        if admission_fence_needed:
            try:
                self.runtime.async_runtime.call(self._wait_for_connect_admission(), timeout=max(0.0, deadline - time.monotonic()))
            except (FutureTimeoutError, RuntimeError):
                pass
        for thread in workers:
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        alive = tuple(thread.name.removeprefix("MsgLogIngestion-") for thread in workers if thread.is_alive())
        with self._lock:
            self._retiring_threads = {thread for thread in self._retiring_threads if thread.is_alive()}
        if alive:
            return (MsgLogScanShutdownTimeout(f"MsgLog ingestion workers did not stop within {join_timeout:g}s (groups: {', '.join(alive)})."),)
        return ()

    def _run(self, source_chat_id: int, lease_owner: str) -> None:
        try:
            self.runtime.async_runtime.call(self._run_ingestion(source_chat_id, lease_owner))
        except BaseException as error:
            self.logger.exception("MsgLog ingestion worker failed for group %s", source_chat_id, exc_info=error)
        finally:
            if self._stopping:
                try:
                    self.ingestion.release_scan(source_chat_id, lease_owner)
                except Exception:
                    self.logger.exception("Failed to release MsgLog ingestion lease for group %s", source_chat_id)
            with self._lock:
                if self._owners.get(source_chat_id) == lease_owner:
                    self._threads.pop(source_chat_id, None)
                    self._owners.pop(source_chat_id, None)

    async def _run_ingestion(self, source_chat_id: int, lease_owner: str) -> None:
        async with self._get_connect_lock():
            if self._stop_event.is_set():
                return
            await self.mtproto.connect()
        if self._stop_event.is_set():
            return
        await MsgLogIngestionService(self.ingestion, self.chat_associations, self.mtproto).run(
            source_chat_id,
            lease_owner=lease_owner,
            stop_requested=self._stop_event.is_set,
        )

    def _get_connect_lock(self) -> asyncio.Lock:
        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        return self._connect_lock

    async def _wait_for_connect_admission(self) -> None:
        async with self._get_connect_lock():
            return

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
