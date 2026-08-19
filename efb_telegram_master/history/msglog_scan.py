"""Thread scheduling for durable MTProto MsgLog ingestion."""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError

from efb_telegram_master.core.models import UTC_LEASE_CLOCK, utc_now_naive
from efb_telegram_master.core.utils import TelegramChatID
from efb_telegram_master.history.msglog_ingestion import MsgLogIngestionService


class MsgLogScanShutdownTimeout(RuntimeError):
    """One or more ingestion scans retained MTProto work after shutdown began."""


class MsgLogScanScheduler:
    """Run a bounded FIFO set of MTProto scan workers."""

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
        self._pending: deque[tuple[int, str, float]] = deque()
        self._pending_source_chat_ids: set[int] = set()
        self._wake_timers: set[threading.Timer] = set()

    def schedule(self, source_chat_id: int) -> str:
        with self._lock:
            return self._schedule_locked(source_chat_id)

    def schedule_for_association(self, source_chat_id: int) -> str:
        """Rescan a completed group after a topic becomes eligible for ingestion."""
        with self._lock:
            if self._stopping:
                return "stopping"
            scan_status = self.ingestion.request_association_rescan(source_chat_id)
            if scan_status is None:
                return "unchanged"
            if scan_status == "running":
                admission = self._schedule_locked(source_chat_id, queue_after_active=True)
                return admission if admission in {"stopping", "unavailable"} else "queued"
            return self._schedule_locked(source_chat_id, queue_after_active=scan_status == "pending")

    def _schedule_locked(self, source_chat_id: int, *, queue_after_active: bool = False) -> str:
        if self._stopping:
            return "stopping"
        self._reap_threads_locked()
        if not self.mtproto.enabled:
            return "unavailable"
        if source_chat_id in self._pending_source_chat_ids:
            return "queued" if queue_after_active else "already running"
        if source_chat_id in self._threads:
            if not queue_after_active:
                return "already running"
            self._enqueue_locked(source_chat_id)
            return "queued"
        scan = self.ingestion.get_or_create_scan(source_chat_id, self.mtproto.config.scan_ceiling)
        if scan.status == "complete":
            return "already complete"
        admitted = len(self._retiring_threads) < self._scan_concurrency()
        self._enqueue_locked(source_chat_id)
        if not admitted:
            return "queued"
        return "resumed" if scan.scanned_count else "started"

    def _enqueue_locked(self, source_chat_id: int, *, not_before: float = 0.0) -> None:
        self._pending.append((source_chat_id, str(uuid.uuid4()), not_before))
        self._pending_source_chat_ids.add(source_chat_id)
        if not_before:
            self._schedule_wakeup_locked(not_before)
        self._admit_workers_locked()

    def _schedule_wakeup_locked(self, not_before: float) -> None:
        delay = max(0.0, not_before - time.monotonic())
        if delay == 0.0:
            return

        def wake() -> None:
            with self._lock:
                self._wake_timers.discard(threading.current_thread())
                if self._stopping:
                    return
                self._reap_threads_locked()
                self._admit_workers_locked()

        timer = threading.Timer(delay, wake)
        timer.daemon = True
        self._wake_timers.add(timer)
        timer.start()

    def stop(self, join_timeout: float = DEFAULT_JOIN_TIMEOUT) -> tuple[BaseException, ...]:
        deadline = time.monotonic() + join_timeout
        with self._lock:
            admission_fence_needed = not self._stopping
            self._stopping = True
            self._stop_event.set()
            self._pending.clear()
            self._pending_source_chat_ids.clear()
            for timer in self._wake_timers:
                timer.cancel()
            self._wake_timers.clear()
            workers = tuple(self._retiring_threads)
        if admission_fence_needed:
            try:
                self._call_runtime(self._wait_for_connect_admission(), timeout=max(0.0, deadline - time.monotonic()))
            except (FutureTimeoutError, RuntimeError):
                pass
        for thread in workers:
            if thread is not threading.current_thread():
                thread.join(max(0.0, deadline - time.monotonic()))
        alive = tuple(thread.name.removeprefix("MsgLogIngestion-") for thread in workers if thread.is_alive())
        with self._lock:
            self._reap_threads_locked()
        if alive:
            return (MsgLogScanShutdownTimeout(f"MsgLog ingestion workers did not stop within {join_timeout:g}s (groups: {', '.join(alive)})."),)
        return ()

    def _run(self, source_chat_id: int, lease_owner: str) -> None:
        interrupted = False
        result: object = None
        try:
            result = self._call_runtime(self._run_ingestion(source_chat_id, lease_owner))
        except BaseException as error:
            interrupted = True
            self.logger.exception("MsgLog ingestion worker failed for group %s", source_chat_id, exc_info=error)
        finally:
            if interrupted and self._stopping:
                try:
                    self.ingestion.release_scan(source_chat_id, lease_owner)
                except Exception:
                    self.logger.exception("Failed to release MsgLog ingestion lease for group %s", source_chat_id)
            with self._lock:
                if result is False and not self._stopping:
                    self._defer_unclaimed_scan_locked(source_chat_id)
                if self._owners.get(source_chat_id) == lease_owner:
                    self._threads.pop(source_chat_id, None)
                    self._owners.pop(source_chat_id, None)

    def _defer_unclaimed_scan_locked(self, source_chat_id: int) -> None:
        scan = self.ingestion.get_or_create_scan(source_chat_id, self.mtproto.config.scan_ceiling)
        if scan.status == "complete":
            return
        lease_expires_at = getattr(scan, "lease_expires_at", None)
        delay = 0.0
        if isinstance(lease_expires_at, datetime.datetime):
            now = utc_now_naive() if getattr(scan, "lease_clock", None) == UTC_LEASE_CLOCK else datetime.datetime.now()
            delay = max(0.0, (lease_expires_at - now).total_seconds())
        not_before = time.monotonic() + delay
        if source_chat_id in self._pending_source_chat_ids:
            self._pending = deque(
                (pending_source_chat_id, owner, max(pending_not_before, not_before) if pending_source_chat_id == source_chat_id else pending_not_before)
                for pending_source_chat_id, owner, pending_not_before in self._pending
            )
            if delay:
                self._schedule_wakeup_locked(not_before)
            return
        self._enqueue_locked(source_chat_id, not_before=not_before)

    def _worker(self) -> None:
        while True:
            with self._lock:
                pending = self._next_runnable_pending_locked()
                if self._stopping:
                    return
                if pending is None:
                    return
                source_chat_id, lease_owner = pending
                self._pending_source_chat_ids.remove(source_chat_id)
                self._threads[source_chat_id] = threading.current_thread()
                self._owners[source_chat_id] = lease_owner
                threading.current_thread().name = f"MsgLogIngestion-{source_chat_id}"
            self._run(source_chat_id, lease_owner)

    def _next_runnable_pending_locked(self) -> tuple[int, str] | None:
        now = time.monotonic()
        for _ in range(len(self._pending)):
            source_chat_id, lease_owner, not_before = self._pending.popleft()
            if source_chat_id not in self._threads and not_before <= now:
                return source_chat_id, lease_owner
            self._pending.append((source_chat_id, lease_owner, not_before))
        return None

    def _admit_workers_locked(self) -> None:
        max_workers = self._scan_concurrency()
        while self._pending and len(self._retiring_threads) < max_workers:
            thread = threading.Thread(target=self._worker, name="MsgLogIngestion-worker")
            self._retiring_threads.add(thread)
            thread.start()

    def _scan_concurrency(self) -> int:
        value = getattr(self.mtproto.config, "scan_concurrency", 1)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("MTProto scan concurrency must be a positive integer")
        return value

    def _reap_threads_locked(self) -> None:
        self._retiring_threads = {thread for thread in self._retiring_threads if thread.is_alive()}

    def _call_runtime(self, coroutine: Coroutine[object, object, object], timeout: float | None = None) -> object:
        try:
            if timeout is None:
                return self.runtime.async_runtime.call(coroutine)
            return self.runtime.async_runtime.call(coroutine, timeout=timeout)
        except BaseException:
            try:
                coroutine.close()
            except (RuntimeError, ValueError):
                pass
            raise

    async def _run_ingestion(self, source_chat_id: int, lease_owner: str) -> bool | None:
        async with self._get_connect_lock():
            if self._stop_event.is_set():
                return None
            await self.mtproto.connect()
        if self._stop_event.is_set():
            return None
        return await MsgLogIngestionService(self.ingestion, self.chat_associations, self.mtproto).run(
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
