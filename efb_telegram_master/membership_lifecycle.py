# coding=utf-8
"""Membership cache, bounded probes, and worker lifecycle for one auxiliary bot."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Protocol

import telegram.error

logger = logging.getLogger(__name__)


class MembershipProbeMetrics(Protocol):
    def record_membership_probe(self, outcome: str) -> None: ...


class MembershipLifecycle:
    """Own membership state and bounded workers for a single auxiliary identity."""

    def __init__(
        self,
        *,
        bot_id: Callable[[], int],
        probe_member: Callable[[int], object],
        probe_workers: int,
        max_pending_probes: int,
        max_cache_entries: int,
        member_ttl: float,
        not_member_ttl: float,
        probe_timeout: float,
    ) -> None:
        self._bot_id = bot_id
        self._probe_member = probe_member
        self._probe_workers = probe_workers
        self._max_cache_entries = max_cache_entries
        self._member_ttl = member_ttl
        self._not_member_ttl = not_member_ttl
        self._probe_timeout = probe_timeout
        self._membership_cache: OrderedDict[int, tuple[bool, float]] = OrderedDict()
        self._membership_revisions: dict[int, int] = {}
        self._membership_lock = threading.Lock()
        self._pending_probes: set[int] = set()
        self._membership_probe_slots = threading.BoundedSemaphore(max_pending_probes)
        self._membership_probe_queue: queue.Queue[tuple[int, int] | None] = queue.Queue()
        self._membership_probe_workers: set[threading.Thread] = set()
        self._membership_probe_workers_started = False
        self._membership_stopping = False
        self._metrics: MembershipProbeMetrics | None = None
        self._membership_changed_callback: Callable[[int, bool], None] | None = None

    def bind_metrics(self, metrics: MembershipProbeMetrics) -> None:
        self._metrics = metrics

    def set_membership_changed_callback(self, callback: Callable[[int, bool], None] | None) -> None:
        with self._membership_lock:
            self._membership_changed_callback = callback

    def get_known_member_chat_ids(self) -> set[int]:
        with self._membership_lock:
            self._purge_expired_membership_cache_locked(time.monotonic())
            return {chat_id for chat_id, (is_member, _timestamp) in self._membership_cache.items() if is_member}

    def get_cache_snapshot(self) -> dict[str, int]:
        with self._membership_lock:
            self._purge_expired_membership_cache_locked(time.monotonic())
            member_count = sum(1 for is_member, _timestamp in self._membership_cache.values() if is_member)
            not_member_count = sum(1 for is_member, _timestamp in self._membership_cache.values() if not is_member)
            pending_count = len(self._pending_probes)
        return {"member": member_count, "not_member": not_member_count, "unknown_probe_pending": pending_count}

    def check(self, chat_id: int) -> bool | None:
        with self._membership_lock:
            entry = self._membership_cache.get(chat_id)
            if entry is not None:
                is_member, timestamp = entry
                ttl = self._member_ttl if is_member else self._not_member_ttl
                if time.monotonic() - timestamp < ttl:
                    self._membership_cache.move_to_end(chat_id)
                    return is_member
                del self._membership_cache[chat_id]
        self._start_probe(chat_id)
        return None

    def update(self, chat_id: int, is_member: bool) -> None:
        with self._membership_lock:
            self._membership_revisions[chat_id] = self._membership_revisions.get(chat_id, 0) + 1
            callback = self._store_membership_locked(chat_id, is_member)
        if callback is not None:
            callback(chat_id, is_member)

    def recheck(self, chat_id: int) -> None:
        with self._membership_lock:
            cached_membership = self._membership_cache.get(chat_id)
            if cached_membership is not None and not cached_membership[0]:
                return
            if chat_id in self._pending_probes:
                return
            self._membership_cache.pop(chat_id, None)
            self._membership_revisions[chat_id] = self._membership_revisions.get(chat_id, 0) + 1
        self._start_probe(chat_id)

    def _start_probe(self, chat_id: int) -> None:
        with self._membership_lock:
            if self._membership_stopping or chat_id in self._pending_probes:
                return
            if not self._membership_probe_slots.acquire(blocking=False):
                self._record_probe("queue_full")
                return
            self._pending_probes.add(chat_id)
            revision = self._membership_revisions.get(chat_id, 0)
            self._start_workers_locked()
            self._membership_probe_queue.put((chat_id, revision))

    def _start_workers_locked(self) -> None:
        if self._membership_probe_workers_started:
            return
        self._membership_probe_workers_started = True
        for worker_index in range(self._probe_workers):
            worker = threading.Thread(target=self._run_worker, name=f"ETM-membership-{worker_index}")
            self._membership_probe_workers.add(worker)
            worker.start()

    def _run_worker(self) -> None:
        while True:
            probe = self._membership_probe_queue.get()
            if probe is None:
                return
            chat_id, revision = probe
            try:
                self._probe(chat_id, revision)
            finally:
                self._finish_probe(chat_id)

    def _probe(self, chat_id: int, revision: int | None = None) -> None:
        if revision is None:
            with self._membership_lock:
                revision = self._membership_revisions.get(chat_id, 0)
        try:
            member = self._probe_member(chat_id)
            is_member = getattr(member, "status") in ("member", "administrator", "creator", "restricted")
            if not self._apply_probe_membership(chat_id, is_member, revision):
                self._record_probe("stale")
                logger.debug("Discarded stale membership probe for bot %d in chat %d", self._bot_id(), chat_id)
                return
            self._record_probe("ok_member" if is_member else "ok_not_member")
            logger.debug("Membership probe for bot %d in chat %d: %s (status=%s)", self._bot_id(), chat_id, is_member, getattr(member, "status"))
        except telegram.error.Forbidden:
            outcome = "forbidden" if self._apply_probe_membership(chat_id, False, revision) else "stale"
            self._record_probe(outcome)
            logger.warning("Membership probe for bot %d in chat %d got Forbidden", self._bot_id(), chat_id)
        except telegram.error.BadRequest as error:
            outcome = "bad_request" if self._apply_probe_membership(chat_id, False, revision) else "stale"
            self._record_probe(outcome)
            logger.debug("Membership probe for bot %d in chat %d failed: %s", self._bot_id(), chat_id, error)
        except FutureTimeoutError:
            self._record_probe("timeout")
            logger.warning("Membership probe for bot %d in chat %d timed out after %.1fs", self._bot_id(), chat_id, self._probe_timeout)
        except Exception as error:
            self._record_probe("error")
            logger.warning("Membership probe failed for bot %d in chat %d: %s", self._bot_id(), chat_id, error)

    def _record_probe(self, outcome: str) -> None:
        if self._metrics:
            self._metrics.record_membership_probe(outcome)

    def _finish_probe(self, chat_id: int) -> None:
        with self._membership_lock:
            if chat_id not in self._pending_probes:
                return
            self._pending_probes.remove(chat_id)
            self._membership_probe_slots.release()
            self._discard_unused_revision_locked(chat_id)

    def _store_membership_locked(self, chat_id: int, is_member: bool) -> Callable[[int, bool], None] | None:
        self._membership_cache[chat_id] = (is_member, time.monotonic())
        self._membership_cache.move_to_end(chat_id)
        while len(self._membership_cache) > self._max_cache_entries:
            evicted_chat_id, _entry = self._membership_cache.popitem(last=False)
            self._discard_unused_revision_locked(evicted_chat_id)
        return self._membership_changed_callback

    def _apply_probe_membership(self, chat_id: int, is_member: bool, revision: int) -> bool:
        with self._membership_lock:
            if self._membership_stopping or self._membership_revisions.get(chat_id, 0) != revision:
                return False
            callback = self._store_membership_locked(chat_id, is_member)
        if callback is not None:
            callback(chat_id, is_member)
        return True

    def _purge_expired_membership_cache_locked(self, now: float) -> None:
        expired_chat_ids = [chat_id for chat_id, (is_member, timestamp) in self._membership_cache.items() if now - timestamp >= (self._member_ttl if is_member else self._not_member_ttl)]
        for chat_id in expired_chat_ids:
            del self._membership_cache[chat_id]
            self._discard_unused_revision_locked(chat_id)

    def _discard_unused_revision_locked(self, chat_id: int) -> None:
        if chat_id not in self._membership_cache and chat_id not in self._pending_probes:
            self._membership_revisions.pop(chat_id, None)

    def begin_shutdown(self) -> None:
        with self._membership_lock:
            if self._membership_stopping:
                return
            self._membership_stopping = True
            self._membership_changed_callback = None
            self._cancel_queued_probes_locked()
            for _worker in self._membership_probe_workers:
                self._membership_probe_queue.put(None)

    def _cancel_queued_probes_locked(self) -> None:
        while True:
            try:
                probe = self._membership_probe_queue.get_nowait()
            except queue.Empty:
                return
            if probe is not None:
                chat_id, _revision = probe
                if chat_id in self._pending_probes:
                    self._pending_probes.remove(chat_id)
                    self._membership_probe_slots.release()
                    self._discard_unused_revision_locked(chat_id)

    def wait_for_shutdown(self, deadline: float) -> bool:
        self.begin_shutdown()
        while self.has_pending_probes():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        for worker in tuple(self._membership_probe_workers):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            worker.join(remaining)
        return not any(worker.is_alive() for worker in self._membership_probe_workers)

    def has_pending_probes(self) -> bool:
        with self._membership_lock:
            return bool(self._pending_probes)
