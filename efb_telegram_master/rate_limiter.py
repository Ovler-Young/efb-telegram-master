# coding=utf-8
"""Monotonic outbound acquisition limits for one Telegram bot."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from pyrate_limiter import Duration, InMemoryBucket, Limiter, Rate, SingleBucketFactory
from pyrate_limiter.clocks import AbstractClock


GLOBAL_LIMIT = 28
GLOBAL_WINDOW_SECONDS = 1.0
CHAT_LIMIT = 18
CHAT_WINDOW_SECONDS = 60.0


class _CallableMonotonicClock(AbstractClock):
    """Adapt a monotonic seconds callable to pyrate-limiter milliseconds."""

    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now

    def now(self) -> int:
        # Truncation cannot advance the current rate-limit window. Rounding
        # would make a time just below a millisecond boundary appear later.
        return int(self._now() * 1000)


class SlidingWindowRateLimiter:
    """Per-bot global and bot-chat limits consumed by non-blocking acquisition."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = _CallableMonotonicClock(clock)
        self._lock = threading.Lock()
        self._global_bucket = self._new_bucket(GLOBAL_LIMIT, GLOBAL_WINDOW_SECONDS)
        self._global_limiter = self._new_limiter(self._global_bucket)
        self._chat_buckets: dict[int, InMemoryBucket] = {}
        self._chat_limiters: dict[int, Limiter] = {}

    def _new_bucket(self, limit: int, seconds: float) -> InMemoryBucket:
        bucket = InMemoryBucket([Rate(limit, int(seconds * Duration.SECOND))])
        bucket._clock = self._clock
        return bucket

    @staticmethod
    def _new_limiter(bucket: InMemoryBucket) -> Limiter:
        return Limiter(SingleBucketFactory(bucket, schedule_leak=False), buffer_ms=0)

    def _chat_limiter(self, chat_id: int) -> tuple[InMemoryBucket, Limiter]:
        limiter = self._chat_limiters.get(chat_id)
        if limiter is None:
            bucket = self._new_bucket(CHAT_LIMIT, CHAT_WINDOW_SECONDS)
            limiter = self._new_limiter(bucket)
            self._chat_buckets[chat_id] = bucket
            self._chat_limiters[chat_id] = limiter
        return self._chat_buckets[chat_id], limiter

    def global_delay(self) -> float:
        """Return the non-consuming delay before this bot's global key is free."""
        with self._lock:
            return self._delay(self._global_bucket, GLOBAL_LIMIT, GLOBAL_WINDOW_SECONDS)

    def chat_delay(self, chat_id: int) -> float:
        """Return the non-consuming delay before this bot-chat key is free."""
        with self._lock:
            bucket, _limiter = self._chat_limiter(chat_id)
            return self._delay(bucket, CHAT_LIMIT, CHAT_WINDOW_SECONDS)

    def peek_delay(self, chat_id: int) -> float:
        """Return the later of the global and bot-chat availability times."""
        with self._lock:
            bucket, _limiter = self._chat_limiter(chat_id)
            return max(
                self._delay(self._global_bucket, GLOBAL_LIMIT, GLOBAL_WINDOW_SECONDS),
                self._delay(bucket, CHAT_LIMIT, CHAT_WINDOW_SECONDS),
            )

    def try_acquire_global(self) -> bool:
        """Consume one global acquisition without waiting for capacity."""
        with self._lock:
            return bool(self._global_limiter.try_acquire("global", blocking=False))

    def try_acquire_chat(self, chat_id: int) -> bool:
        """Consume one bot-chat acquisition without waiting for capacity."""
        with self._lock:
            _bucket, limiter = self._chat_limiter(chat_id)
            return bool(limiter.try_acquire(str(chat_id), blocking=False))

    def try_acquire(self, chat_id: int) -> bool:
        """Acquire global capacity before bot-chat capacity without rollback."""
        if not self.try_acquire_global():
            return False
        return self.try_acquire_chat(chat_id)

    def get_counts(self, chat_id: int) -> tuple[int, int]:
        """Return active bot-chat and global acquisition counts for diagnostics."""
        with self._lock:
            chat_bucket, _limiter = self._chat_limiter(chat_id)
            self._leak(self._global_bucket)
            self._leak(chat_bucket)
            return chat_bucket.count(), self._global_bucket.count()

    def occupancy_snapshot(self) -> dict[str, float]:
        """Return aggregate limiter occupancy without exposing chat identities."""
        with self._lock:
            self._leak(self._global_bucket)
            global_occupancy = self._global_bucket.count() / GLOBAL_LIMIT
            chat_occupancy = 0.0
            for bucket in self._chat_buckets.values():
                self._leak(bucket)
                chat_occupancy = max(chat_occupancy, bucket.count() / CHAT_LIMIT)
            return {"global": global_occupancy, "chat": chat_occupancy}

    def _delay(self, bucket: InMemoryBucket, limit: int, seconds: float) -> float:
        self._leak(bucket)
        if bucket.count() < limit:
            return 0.0
        boundary_item = bucket.peek(limit - 1)
        if boundary_item is None:
            return 0.0
        available_at_ms = boundary_item.timestamp + int(seconds * Duration.SECOND) + 1
        return max(0.0, (available_at_ms - self._clock.now()) / 1000)

    def _leak(self, bucket: InMemoryBucket) -> None:
        bucket.leak(self._clock.now())
