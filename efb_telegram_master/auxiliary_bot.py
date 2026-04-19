# coding=utf-8

import asyncio
import bisect
import logging
import threading
import time
from collections import defaultdict, deque
from inspect import isawaitable
from typing import Any, Coroutine, Dict, Optional, Tuple, TYPE_CHECKING, cast

import telegram
import telegram.error
from telegram.request import HTTPXRequest

if TYPE_CHECKING:
    from .bot_manager import AsyncTelegramRuntime

logger = logging.getLogger(__name__)

def _resolve_bot_result(result: object) -> Any:
    if isawaitable(result):
        return asyncio.run(cast(Coroutine[Any, Any, Any], result))
    return result


class AuxiliaryBot:
    """Lightweight wrapper around telegram.Bot for send-only auxiliary bots.

    Each instance has its own independent sliding-window rate limiter
    and a non-blocking group membership cache with TTL-based refresh.
    """

    MEMBERSHIP_TTL_MEMBER = 1800.0      # 30 min for confirmed member
    MEMBERSHIP_TTL_NOT_MEMBER = 300.0   # 5 min for non-member (re-check sooner)

    def __init__(self, token: str, *,
                 request_kwargs: Optional[dict] = None,
                 base_url: Optional[str] = None,
                 base_file_url: Optional[str] = None,
                 global_limit: int = 30,
                 global_window: float = 1.0,
                 chat_limit: int = 20,
                 chat_window: float = 60.0):
        self._token = token
        self._request_kwargs = dict(request_kwargs or {})
        self._base_kwargs: Dict[str, Any] = {}
        if base_url:
            self._base_kwargs['base_url'] = base_url
        if base_file_url:
            self._base_kwargs['base_file_url'] = base_file_url

        self.async_bot: telegram.Bot = self._create_bot()
        self.bot: Any = self.async_bot

        # Identity (populated by initialize())
        self.bot_id: int = 0
        self.username: str = ""
        self.disabled: bool = False
        self._disable_reason: str = ""
        self._runtime: Optional['AsyncTelegramRuntime'] = None

        # Rate limiting (same structure as TelegramBotManager)
        self._rate_limit_lock = threading.Lock()
        self._global_timestamps: list = []
        self._chat_timestamps: defaultdict = defaultdict(deque)
        self.GLOBAL_LIMIT = global_limit
        self.GLOBAL_WINDOW = global_window
        self.CHAT_LIMIT = chat_limit
        self.CHAT_WINDOW = chat_window

        # Membership cache: chat_id -> (is_member, timestamp)
        self._membership_cache: Dict[int, Tuple[bool, float]] = {}
        self._membership_lock = threading.Lock()
        self._pending_probes: set = set()

    def _create_bot(self) -> telegram.Bot:
        kwargs: Dict[str, Any] = {"token": self._token, **self._base_kwargs}
        if self._request_kwargs:
            kwargs['request'] = HTTPXRequest(**self._request_kwargs)
            kwargs['get_updates_request'] = HTTPXRequest(**self._request_kwargs)
        return telegram.Bot(**kwargs)

    def initialize(self) -> bool:
        """Call get_me() to validate token and cache identity.
        Returns True on success, False on failure (bot is disabled).
        """
        try:
            validation_bot = self.async_bot if not isinstance(self.async_bot, telegram.Bot) else self._create_bot()
            me: telegram.User = cast(telegram.User, _resolve_bot_result(validation_bot.get_me()))
            self.bot_id = me.id
            self.username = me.username or ""
            logger.info("Auxiliary bot initialized: @%s (id=%d)", self.username, self.bot_id)
            return True
        except telegram.error.Forbidden as e:
            self.disabled = True
            self._disable_reason = str(e)
            logger.error("Failed to initialize auxiliary bot: %s", e)
            return False
        except Exception as e:
            self.disabled = True
            self._disable_reason = str(e)
            logger.error("Failed to initialize auxiliary bot: %s", e)
            return False

    def _cleanup_old_timestamps(self):
        """Remove timestamps older than the time window (called under lock)."""
        current_time = time.time()
        while self._global_timestamps and self._global_timestamps[0] <= current_time - self.GLOBAL_WINDOW:
            self._global_timestamps.pop(0)
        for _chat_id, timestamps in self._chat_timestamps.items():
            while timestamps and timestamps[0] <= current_time - self.CHAT_WINDOW:
                timestamps.popleft()

    def peek_delay(self, chat_id: int) -> float:
        """Check rate limit delay without reserving a slot. Thread-safe."""
        with self._rate_limit_lock:
            current_time = time.time()
            self._cleanup_old_timestamps()

            # Chat-specific check
            chat_delay = 0.0
            chat_timestamps = self._chat_timestamps[chat_id]
            if len(chat_timestamps) >= self.CHAT_LIMIT - 2:
                safe_index = len(chat_timestamps) - (self.CHAT_LIMIT - 2)
                reference_timestamp = chat_timestamps[safe_index]
                chat_delay = max(0.0, (reference_timestamp + self.CHAT_WINDOW) - current_time)

            candidate_time = current_time + chat_delay

            # Global limit check — use bisect_right for left bound so the
            # window is half-open (left_bound, candidate_time], preventing an
            # infinite loop when timestamps cluster on the boundary.
            while True:
                left_bound = candidate_time - self.GLOBAL_WINDOW
                idx = bisect.bisect_right(self._global_timestamps, left_bound)
                right_idx = bisect.bisect_right(self._global_timestamps, candidate_time)
                in_window = right_idx - idx
                if in_window < self.GLOBAL_LIMIT - 2:
                    break
                candidate_time = self._global_timestamps[idx] + self.GLOBAL_WINDOW

            return max(0.0, candidate_time - current_time)

    def reserve_slot(self, chat_id: int) -> float:
        """Reserve a send slot and return the delay. Thread-safe."""
        with self._rate_limit_lock:
            current_time = time.time()
            self._cleanup_old_timestamps()

            chat_delay = 0.0
            chat_timestamps = self._chat_timestamps[chat_id]
            if len(chat_timestamps) >= self.CHAT_LIMIT - 2:
                safe_index = len(chat_timestamps) - (self.CHAT_LIMIT - 2)
                reference_timestamp = chat_timestamps[safe_index]
                chat_delay = max(0.0, (reference_timestamp + self.CHAT_WINDOW) - current_time)

            candidate_time = current_time + chat_delay

            while True:
                left_bound = candidate_time - self.GLOBAL_WINDOW
                idx = bisect.bisect_right(self._global_timestamps, left_bound)
                right_idx = bisect.bisect_right(self._global_timestamps, candidate_time)
                in_window = right_idx - idx
                if in_window < self.GLOBAL_LIMIT - 2:
                    break
                candidate_time = self._global_timestamps[idx] + self.GLOBAL_WINDOW

            delay = max(0.0, candidate_time - current_time)
            bisect.insort(self._global_timestamps, candidate_time)
            chat_timestamps.append(candidate_time)
            return delay

    # Tri-state membership results
    MEMBERSHIP_MEMBER = True
    MEMBERSHIP_NOT_MEMBER = False
    MEMBERSHIP_UNKNOWN = None

    def check_membership(self, chat_id: int) -> bool:
        """Return cached membership status. On cache miss, trigger a
        background probe and return False (non-blocking).

        Uses stale-while-revalidate: if cached value exists but is expired,
        return the stale value while refreshing in the background. This avoids
        false "not a member" results when all bots' caches expire simultaneously.
        """
        result = self.check_membership_tri(chat_id)
        if result is None:
            return False
        return result

    def check_membership_tri(self, chat_id: int):
        """Tri-state membership check: True (member), False (confirmed not member),
        None (unknown / probe in progress).
        """
        stale_value = None
        need_probe = False
        with self._membership_lock:
            entry = self._membership_cache.get(chat_id)
            if entry is not None:
                is_member, timestamp = entry
                ttl = self.MEMBERSHIP_TTL_MEMBER if is_member else self.MEMBERSHIP_TTL_NOT_MEMBER
                age = time.time() - timestamp
                if age < ttl:
                    return is_member
                need_probe = True
                stale_value = is_member

        if need_probe:
            self._start_membership_probe(chat_id)
            return stale_value

        self._start_membership_probe(chat_id)
        return None

    def check_membership_sync(self, chat_id: int, timeout: float = 5.0) -> bool:
        """Blocking membership check. Waits for a pending probe to finish,
        or runs one synchronously if no cache entry exists."""
        with self._membership_lock:
            entry = self._membership_cache.get(chat_id)
            if entry is not None:
                is_member, timestamp = entry
                ttl = self.MEMBERSHIP_TTL_MEMBER if is_member else self.MEMBERSHIP_TTL_NOT_MEMBER
                if time.time() - timestamp < ttl:
                    return is_member

        # Trigger probe if not already running
        self._start_membership_probe(chat_id)

        # Wait for the probe to finish
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._membership_lock:
                if chat_id not in self._pending_probes:
                    entry = self._membership_cache.get(chat_id)
                    if entry is not None:
                        return entry[0]
                    return False
            time.sleep(0.05)

        logger.warning("Membership sync check timed out for bot %d in chat %d", self.bot_id, chat_id)
        return False

    def update_membership(self, chat_id: int, is_member: bool):
        """Update the membership cache directly (e.g. from chat_left handler)."""
        with self._membership_lock:
            self._membership_cache[chat_id] = (is_member, time.time())

    def _start_membership_probe(self, chat_id: int):
        """Start a background thread to check membership via get_chat_member API."""
        with self._membership_lock:
            if chat_id in self._pending_probes:
                return
            self._pending_probes.add(chat_id)

        thread = threading.Thread(
            target=self._probe_membership,
            args=(chat_id,),
            daemon=True,
            name=f"AuxBotMemberProbe-{self.bot_id}-{chat_id}"
        )
        thread.start()

    def _probe_membership(self, chat_id: int):
        """Background probe: call get_chat_member and update cache."""
        try:
            member: telegram.ChatMember = cast(
                telegram.ChatMember,
                _resolve_bot_result(self.async_bot.get_chat_member(chat_id, self.bot_id)),
            )
            is_member = member.status in ('member', 'administrator', 'creator', 'restricted')
            self.update_membership(chat_id, is_member)
            logger.debug("Membership probe for bot %d in chat %d: %s (status=%s)",
                         self.bot_id, chat_id, is_member, member.status)
        except telegram.error.Forbidden:
            self.disabled = True
            self._disable_reason = "Forbidden during membership probe"
            logger.error("Auxiliary bot %d got Forbidden during membership probe", self.bot_id)
        except telegram.error.BadRequest as e:
            self.update_membership(chat_id, False)
            logger.debug("Membership probe for bot %d in chat %d failed: %s", self.bot_id, chat_id, e)
        except Exception as e:
            self.update_membership(chat_id, False)
            logger.warning("Membership probe failed for bot %d in chat %d: %s", self.bot_id, chat_id, e)
        finally:
            with self._membership_lock:
                self._pending_probes.discard(chat_id)

    def has_pending_probes(self) -> bool:
        """Check if there are any pending membership probes."""
        with self._membership_lock:
            return bool(self._pending_probes)

    def mark_disabled(self, reason: str = ""):
        """Mark this bot as permanently disabled for this session."""
        self.disabled = True
        self._disable_reason = reason
        logger.error("Auxiliary bot @%s (id=%d) disabled: %s", self.username, self.bot_id, reason)

    def bind_runtime(self, runtime: 'AsyncTelegramRuntime'):
        """Bind the runtime-backed sync facade used by the rest of ETM."""
        from .bot_manager import SyncBotFacade

        self._runtime = runtime
        self.bot = SyncBotFacade(self.async_bot, runtime)

    def __repr__(self):
        return f"AuxiliaryBot(@{self.username}, id={self.bot_id}, disabled={self.disabled})"
