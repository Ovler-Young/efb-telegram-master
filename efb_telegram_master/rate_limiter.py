# coding=utf-8
"""Shared sliding-window rate limiter for Telegram bot send slots.

Both the main bot (TelegramBotManager) and each AuxiliaryBot maintain
independent rate-limit state, but the *algorithm* is identical:

* Global limit:  N sends per W-second window across all chats.
* Per-chat limit: M sends per V-second window for a single chat.

This module provides a single, tested implementation so the logic is
not duplicated.

Thread-safety: every public method acquires ``_lock`` internally.
"""

import bisect
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class SlotReservation:
    """Exact reservation identity shared by the global and per-chat windows."""

    token_id: int
    owner_id: Optional[str]
    chat_id: int
    reserved_at: float


@dataclass(frozen=True)
class ReservationOutcome:
    delay: float
    reservation: SlotReservation


class SlidingWindowRateLimiter:
    """Two-dimensional (global + per-chat) sliding-window rate limiter.

    Parameters
    ----------
    global_limit : int
        Max sends within *global_window* (across all chats).
    global_window : float
        Length of the global window in seconds.
    chat_limit : int
        Max sends within *chat_window* for a single chat.
    chat_window : float
        Length of the per-chat window in seconds.
    safety_margin : int
        Subtracted from both limits to leave headroom for Telegram's own
        accounting (which may differ from ours by one or two).  Default 2.
    """

    def __init__(
        self,
        global_limit: int = 30,
        global_window: float = 1.0,
        chat_limit: int = 20,
        chat_window: float = 60.0,
        safety_margin: int = 2,
        owner_id: Optional[str] = None,
    ):
        self.global_limit = global_limit
        self.global_window = global_window
        self.chat_limit = chat_limit
        self.chat_window = chat_window
        self._margin = safety_margin
        self._owner_id = owner_id

        self._lock = threading.Lock()
        self._next_token_id = 1
        self._global_timestamps: list[tuple[float, int]] = []
        self._chat_timestamps: Dict[int, deque] = defaultdict(deque)

    # Public API

    def peek_delay(self, chat_id: int) -> float:
        """Return the delay (seconds) before a send to *chat_id* is allowed.

        Does **not** reserve a slot – the caller can use this to compare
        multiple bots and pick the fastest one.
        """
        with self._lock:
            return self._compute_delay(chat_id)

    def set_owner_id(self, owner_id: Optional[str]) -> None:
        """Set the bot identity copied into future reservation tokens."""
        with self._lock:
            self._owner_id = owner_id

    def reserve_slot(self, chat_id: int) -> ReservationOutcome:
        """Reserve a send slot for *chat_id* and return its exact token.

        The slot is recorded at ``now + delay`` in both the global and
        per-chat timestamp lists.
        """
        with self._lock:
            delay = self._compute_delay(chat_id)
            candidate_time = time.time() + delay
            token_id = self._next_token_id
            self._next_token_id += 1
            reservation = SlotReservation(
                token_id=token_id,
                owner_id=self._owner_id,
                chat_id=chat_id,
                reserved_at=candidate_time,
            )
            timestamp_entry = (candidate_time, token_id)
            bisect.insort(self._global_timestamps, timestamp_entry)
            self._chat_timestamps[chat_id].append(timestamp_entry)
            return ReservationOutcome(delay=delay, reservation=reservation)

    def release_slot(self, reservation: Optional[SlotReservation]) -> None:
        """Undo one exact reservation when its Telegram call did not happen."""
        with self._lock:
            if reservation is None:
                self._cleanup()
                return

            entry = (reservation.reserved_at, reservation.token_id)
            chat_ts = self._chat_timestamps.get(reservation.chat_id)
            if chat_ts:
                try:
                    chat_ts.remove(entry)
                except ValueError:
                    pass
            idx = bisect.bisect_left(self._global_timestamps, entry)
            if idx < len(self._global_timestamps) and self._global_timestamps[idx] == entry:
                self._global_timestamps.pop(idx)
            self._cleanup()

    def has_reservation(self, reservation: SlotReservation) -> bool:
        """Return whether an exact token is still present in both windows."""
        with self._lock:
            self._cleanup()
            entry = (reservation.reserved_at, reservation.token_id)
            return (
                entry in self._global_timestamps
                and entry in self._chat_timestamps.get(reservation.chat_id, ())
            )

    def get_counts(self, chat_id: int) -> Tuple[int, int]:
        """Return ``(chat_count, global_count)`` for diagnostics."""
        with self._lock:
            self._cleanup()
            return len(self._chat_timestamps.get(chat_id, ())), len(self._global_timestamps)

    def get_chat_count_snapshot(self) -> Tuple[Dict[int, int], int]:
        """Return current per-chat occupancy and the effective per-chat limit."""
        with self._lock:
            self._cleanup()
            return (
                {chat_id: len(timestamps) for chat_id, timestamps in self._chat_timestamps.items() if timestamps},
                max(0, self.chat_limit - self._margin),
            )

    def get_reserved_slot_count(self) -> int:
        """Return current global sliding-window reservations for diagnostics."""
        with self._lock:
            self._cleanup()
            return len(self._global_timestamps)

    # Internals

    def _compute_delay(self, chat_id: int) -> float:
        """Delay computation shared by peek / reserve (caller holds lock)."""
        current_time = time.time()
        self._cleanup()

        effective_chat_limit = self.chat_limit - self._margin
        effective_global_limit = self.global_limit - self._margin

        # Per-chat window
        chat_delay = 0.0
        chat_ts = self._chat_timestamps.get(chat_id)
        if chat_ts and len(chat_ts) >= effective_chat_limit:
            safe_index = len(chat_ts) - effective_chat_limit
            chat_delay = max(0.0, (chat_ts[safe_index][0] + self.chat_window) - current_time)

        candidate_time = current_time + chat_delay

        # Global window
        while True:
            left_bound = (candidate_time - self.global_window, float('inf'))
            right_bound = (candidate_time, float('inf'))
            idx = bisect.bisect_right(self._global_timestamps, left_bound)
            right_idx = bisect.bisect_right(self._global_timestamps, right_bound)
            in_window = right_idx - idx
            if in_window < effective_global_limit:
                break
            candidate_time = self._global_timestamps[idx][0] + self.global_window

        return max(0.0, candidate_time - current_time)

    def _cleanup(self):
        """Remove timestamps older than their respective windows."""
        current_time = time.time()
        global_cutoff = current_time - self.global_window
        while self._global_timestamps and self._global_timestamps[0][0] <= global_cutoff:
            self._global_timestamps.pop(0)

        chat_cutoff = current_time - self.chat_window
        empty_chat_ids = []
        for cid, ts in self._chat_timestamps.items():
            while ts and ts[0][0] <= chat_cutoff:
                ts.popleft()
            if not ts:
                empty_chat_ids.append(cid)
        for cid in empty_chat_ids:
            del self._chat_timestamps[cid]
