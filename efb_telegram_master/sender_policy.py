from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Optional

from telegram.error import RetryAfter

from .outbound_types import QueuedCall, SenderDecision, SenderSelection
from .rate_limiter import SlidingWindowRateLimiter

if TYPE_CHECKING:
    from .bot_pool import BotPool


def retry_after_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


class SenderPolicy:
    MEMBERSHIP_RECHECK_SECONDS = 0.25

    def __init__(self, main_bot: object, bot_pool: Optional[BotPool], main_rate_limiter: SlidingWindowRateLimiter) -> None:
        self._main_bot = main_bot
        self._bot_pool = bot_pool
        self._main_rate_limiter = main_rate_limiter
        self._cooldowns_lock = threading.Lock()
        self._cooldowns: dict[tuple[Optional[str], int], float] = {}

    def select(self, call: QueuedCall, now: float) -> SenderDecision:
        required = call.required_sender_bot_id
        if required == "__main__":
            return self._available(SenderSelection(self._main_bot, None), call.telegram_chat_id, now)
        if required is not None:
            auxiliary = self._bot_pool.get_bot_by_id(required) if self._bot_pool else None
            if auxiliary is None or auxiliary.disabled:
                return SenderDecision(None, error="required_sender_unavailable")
            membership = auxiliary.check_membership_tri(call.telegram_chat_id)
            if membership is None:
                return SenderDecision(None, now + self.MEMBERSHIP_RECHECK_SECONDS)
            if not membership:
                return SenderDecision(None, error="required_sender_unavailable")
            return self._available(SenderSelection(auxiliary.bot, str(auxiliary.bot_id)), call.telegram_chat_id, now)

        candidates: list[tuple[int, str, SenderDecision]] = [(2, "", self._available(SenderSelection(self._main_bot, None), call.telegram_chat_id, now))]
        membership_retry_at: Optional[float] = None
        if self._bot_pool:
            preferred = self._bot_pool.preferred_sender(call.slave_id)
            for auxiliary, membership in self._bot_pool.candidate_bots(call.telegram_chat_id):
                if membership is None:
                    deadline = now + self.MEMBERSHIP_RECHECK_SECONDS
                    membership_retry_at = deadline if membership_retry_at is None else min(membership_retry_at, deadline)
                elif membership:
                    selection = SenderSelection(auxiliary.bot, str(auxiliary.bot_id))
                    candidates.append((0 if preferred is auxiliary else 1, str(auxiliary.bot_id), self._available(selection, call.telegram_chat_id, now)))
        selectable = [candidate for candidate in candidates if candidate[2].selection is not None]
        if selectable:
            return min(selectable, key=lambda candidate: candidate[:2])[2]
        if membership_retry_at is not None:
            return SenderDecision(None, membership_retry_at)
        retries = [candidate[2].retry_at for candidate in candidates if candidate[2].retry_at is not None]
        return SenderDecision(None, min(retries) if retries else now + self.MEMBERSHIP_RECHECK_SECONDS)

    def _available(self, selection: SenderSelection, chat_id: int, now: float) -> SenderDecision:
        with self._cooldowns_lock:
            cooldown = self._cooldowns.get((selection.sender_bot_id, chat_id), 0.0)
        retry_at = max(cooldown, now + self._limiter_delay(selection, chat_id))
        return SenderDecision(selection) if retry_at <= now else SenderDecision(None, retry_at)

    def _limiter_delay(self, selection: SenderSelection, chat_id: int) -> float:
        if selection.sender_bot_id is None:
            return self._main_rate_limiter.peek_delay(chat_id)
        auxiliary = self._bot_pool.get_bot_by_id(selection.sender_bot_id) if self._bot_pool else None
        return 0.0 if auxiliary is None else auxiliary.peek_delay(chat_id)

    def acquire(self, selection: SenderSelection, chat_id: int) -> bool:
        if selection.sender_bot_id is None:
            return self._main_rate_limiter.try_acquire(chat_id)
        auxiliary = self._bot_pool.get_bot_by_id(selection.sender_bot_id) if self._bot_pool else None
        return auxiliary is not None and auxiliary.try_acquire_limits(chat_id)

    def record_retry_after(self, call: QueuedCall, error: RetryAfter, selection: SenderSelection) -> None:
        key = (selection.sender_bot_id, call.telegram_chat_id)
        deadline = time.monotonic() + retry_after_seconds(error)
        with self._cooldowns_lock:
            self._cooldowns[key] = max(self._cooldowns.get(key, 0.0), deadline)

    def record_send_failure(self, call: QueuedCall, selection: SenderSelection) -> None:
        if selection.sender_bot_id is not None and self._bot_pool is not None:
            self._bot_pool.record_possible_membership_failure(call.slave_id, selection.sender_bot_id, call.telegram_chat_id)

    def cooldown_snapshot(self) -> dict[str, float]:
        now = time.monotonic()
        cooldowns = {"main": 0.0, "auxiliary": 0.0}
        with self._cooldowns_lock:
            cooldown_entries = tuple(self._cooldowns.items())
        for (sender_bot_id, _chat_id), deadline in cooldown_entries:
            kind = "main" if sender_bot_id is None else "auxiliary"
            cooldowns[kind] = max(cooldowns[kind], max(0.0, deadline - now))
        return cooldowns

    def rate_limit_occupancy_snapshot(self) -> dict[str, float]:
        occupancy = self._main_rate_limiter.occupancy_snapshot()
        if self._bot_pool:
            for scope, value in self._bot_pool.rate_limit_occupancy_snapshot().items():
                occupancy[scope] = max(occupancy[scope], value)
        return occupancy
