from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Optional

from telegram.error import RetryAfter

from efb_telegram_master.outbound.outbound_types import QueuedCall, SenderDecision, SenderSelection
from efb_telegram_master.runtime.rate_limiter import SlidingWindowRateLimiter

if TYPE_CHECKING:
    from efb_telegram_master.runtime.bot_pool import BotPool


CooldownKey = tuple[Optional[str], int]
CooldownHeap = list[CooldownKey]
CooldownHeapIndices = dict[CooldownKey, int]


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
        self._cooldowns: dict[CooldownKey, float] = {}
        self._cooldown_expiry_heap: CooldownHeap = []
        self._cooldown_expiry_indices: CooldownHeapIndices = {}
        self._cooldown_max_heaps: dict[str, CooldownHeap] = {"main": [], "auxiliary": []}
        self._cooldown_max_indices: dict[str, CooldownHeapIndices] = {"main": {}, "auxiliary": {}}

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
            self._cleanup_expired_cooldowns(now)
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
        now = time.monotonic()
        deadline = now + retry_after_seconds(error)
        with self._cooldowns_lock:
            self._cleanup_expired_cooldowns(now)
            if deadline > self._cooldowns.get(key, 0.0):
                self._set_cooldown(key, deadline)

    def record_send_failure(self, call: QueuedCall, selection: SenderSelection) -> None:
        if selection.sender_bot_id is not None and self._bot_pool is not None:
            self._bot_pool.record_possible_membership_failure(call.slave_id, selection.sender_bot_id, call.telegram_chat_id)

    def cooldown_snapshot(self) -> dict[str, float]:
        now = time.monotonic()
        with self._cooldowns_lock:
            self._cleanup_expired_cooldowns(now)
            return {kind: self._remaining_cooldown(kind, now) for kind in self._cooldown_max_heaps}

    @staticmethod
    def _cooldown_kind(key: CooldownKey) -> str:
        return "main" if key[0] is None else "auxiliary"

    @staticmethod
    def _cooldown_key_order(key: CooldownKey) -> tuple[bool, str, int]:
        return key[0] is not None, key[0] or "", key[1]

    def _set_cooldown(self, key: CooldownKey, deadline: float) -> None:
        if key in self._cooldowns:
            self._cooldowns[key] = deadline
            self._fix_cooldown_heap(self._cooldown_expiry_heap, self._cooldown_expiry_indices, self._cooldown_expiry_indices[key], reverse=False)
            kind = self._cooldown_kind(key)
            self._fix_cooldown_heap(self._cooldown_max_heaps[kind], self._cooldown_max_indices[kind], self._cooldown_max_indices[kind][key], reverse=True)
            return

        self._cooldowns[key] = deadline
        self._push_cooldown_heap(self._cooldown_expiry_heap, self._cooldown_expiry_indices, key, reverse=False)
        kind = self._cooldown_kind(key)
        self._push_cooldown_heap(self._cooldown_max_heaps[kind], self._cooldown_max_indices[kind], key, reverse=True)

    def _cleanup_expired_cooldowns(self, now: float) -> None:
        while self._cooldown_expiry_heap and self._cooldowns[self._cooldown_expiry_heap[0]] <= now:
            self._remove_cooldown(self._cooldown_expiry_heap[0])

    def _remaining_cooldown(self, kind: str, now: float) -> float:
        heap = self._cooldown_max_heaps[kind]
        return 0.0 if not heap else self._cooldowns[heap[0]] - now

    def _remove_cooldown(self, key: CooldownKey) -> None:
        self._remove_cooldown_heap(self._cooldown_expiry_heap, self._cooldown_expiry_indices, key, reverse=False)
        kind = self._cooldown_kind(key)
        self._remove_cooldown_heap(self._cooldown_max_heaps[kind], self._cooldown_max_indices[kind], key, reverse=True)
        del self._cooldowns[key]

    def _push_cooldown_heap(self, heap: CooldownHeap, indices: CooldownHeapIndices, key: CooldownKey, *, reverse: bool) -> None:
        indices[key] = len(heap)
        heap.append(key)
        self._sift_cooldown_up(heap, indices, len(heap) - 1, reverse=reverse)

    def _remove_cooldown_heap(self, heap: CooldownHeap, indices: CooldownHeapIndices, key: CooldownKey, *, reverse: bool) -> None:
        index = indices.pop(key)
        replacement = heap.pop()
        if index == len(heap):
            return
        heap[index] = replacement
        indices[replacement] = index
        self._fix_cooldown_heap(heap, indices, index, reverse=reverse)

    def _fix_cooldown_heap(self, heap: CooldownHeap, indices: CooldownHeapIndices, index: int, *, reverse: bool) -> None:
        index = self._sift_cooldown_up(heap, indices, index, reverse=reverse)
        self._sift_cooldown_down(heap, indices, index, reverse=reverse)

    def _sift_cooldown_up(self, heap: CooldownHeap, indices: CooldownHeapIndices, index: int, *, reverse: bool) -> int:
        while index:
            parent = (index - 1) // 2
            if not self._cooldown_precedes(heap[index], heap[parent], reverse=reverse):
                break
            self._swap_cooldown_heap_entries(heap, indices, index, parent)
            index = parent
        return index

    def _sift_cooldown_down(self, heap: CooldownHeap, indices: CooldownHeapIndices, index: int, *, reverse: bool) -> None:
        while (child := index * 2 + 1) < len(heap):
            right = child + 1
            if right < len(heap) and self._cooldown_precedes(heap[right], heap[child], reverse=reverse):
                child = right
            if not self._cooldown_precedes(heap[child], heap[index], reverse=reverse):
                return
            self._swap_cooldown_heap_entries(heap, indices, index, child)
            index = child

    def _cooldown_precedes(self, first: CooldownKey, second: CooldownKey, *, reverse: bool) -> bool:
        first_deadline = self._cooldowns[first]
        second_deadline = self._cooldowns[second]
        if first_deadline == second_deadline:
            return self._cooldown_key_order(first) < self._cooldown_key_order(second)
        return first_deadline > second_deadline if reverse else first_deadline < second_deadline

    @staticmethod
    def _swap_cooldown_heap_entries(heap: CooldownHeap, indices: CooldownHeapIndices, first: int, second: int) -> None:
        heap[first], heap[second] = heap[second], heap[first]
        indices[heap[first]] = first
        indices[heap[second]] = second

    def rate_limit_occupancy_snapshot(self) -> dict[str, float]:
        occupancy = self._main_rate_limiter.occupancy_snapshot()
        if self._bot_pool:
            for scope, value in self._bot_pool.rate_limit_occupancy_snapshot().items():
                occupancy[scope] = max(occupancy[scope], value)
        return occupancy
