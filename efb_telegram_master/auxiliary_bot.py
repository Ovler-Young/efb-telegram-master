# coding=utf-8

import logging
import threading
import time
from collections.abc import Coroutine
from inspect import isawaitable
from typing import Any, Callable, Dict, Optional, Tuple, TYPE_CHECKING, TypeVar, cast, overload, Literal

from .rate_limiter import ReservationOutcome, SlotReservation

import telegram
import telegram.error
from telegram.request import HTTPXRequest

if TYPE_CHECKING:
    from .bot_manager import AsyncTelegramRuntime, SyncBotFacade

logger = logging.getLogger(__name__)

T = TypeVar("T")


@overload
def _resolve_bot_result(result: Coroutine[Any, Any, T], runtime: 'AsyncTelegramRuntime') -> T:
    ...


@overload
def _resolve_bot_result(result: T, runtime: Optional['AsyncTelegramRuntime']) -> T:
    ...


def _resolve_bot_result(result: object, runtime: Optional['AsyncTelegramRuntime']) -> object:
    if isawaitable(result):
        if runtime is None:
            raise RuntimeError("Auxiliary bot runtime is not bound.")
        return runtime.call(cast(Coroutine[Any, Any, object], result))
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
                 local_mode: bool = False,
                 global_limit: int = 30,
                 global_window: float = 1.0,
                 chat_limit: int = 20,
                 chat_window: float = 60.0):
        self._token = token
        self._request_kwargs = dict(request_kwargs or {})
        self._base_kwargs: Dict[str, str] = {}
        if base_url:
            self._base_kwargs['base_url'] = base_url
        if base_file_url:
            self._base_kwargs['base_file_url'] = base_file_url
        self._local_mode = local_mode

        self.async_bot: telegram.Bot = self._create_bot()
        self.bot: telegram.Bot | 'SyncBotFacade' = self.async_bot

        # Identity (populated by initialize())
        self.bot_id: int = 0
        self.username: str = ""
        self.disabled: bool = False
        self._runtime: Optional['AsyncTelegramRuntime'] = None

        # Rate limiting — delegates to shared SlidingWindowRateLimiter
        from .rate_limiter import SlidingWindowRateLimiter
        self._rate_limiter = SlidingWindowRateLimiter(
            global_limit=global_limit,
            global_window=global_window,
            chat_limit=chat_limit,
            chat_window=chat_window,
        )

        # Membership cache: chat_id -> (is_member, timestamp)
        self._membership_cache: Dict[int, Tuple[bool, float]] = {}
        self._membership_lock = threading.Lock()
        self._pending_probes: set = set()
        self._metrics = None
        self._membership_changed_callback: Optional[Callable[[], None]] = None

    def _create_bot(self) -> telegram.Bot:
        request = self._build_request() if self._request_kwargs else None
        get_updates_request = self._build_request() if self._request_kwargs else None
        base_url = self._base_kwargs.get('base_url')
        base_file_url = self._base_kwargs.get('base_file_url')
        if base_url is not None and base_file_url is not None:
            return telegram.Bot(
                token=self._token,
                base_url=base_url,
                base_file_url=base_file_url,
                local_mode=self._local_mode,
                request=request,
                get_updates_request=get_updates_request,
            )
        if base_url is not None:
            return telegram.Bot(
                token=self._token,
                base_url=base_url,
                local_mode=self._local_mode,
                request=request,
                get_updates_request=get_updates_request,
            )
        if base_file_url is not None:
            return telegram.Bot(
                token=self._token,
                base_file_url=base_file_url,
                local_mode=self._local_mode,
                request=request,
                get_updates_request=get_updates_request,
            )
        return telegram.Bot(
            token=self._token,
            local_mode=self._local_mode,
            request=request,
            get_updates_request=get_updates_request,
        )

    def _build_request(self) -> HTTPXRequest:
        return HTTPXRequest(
            read_timeout=cast(Optional[float], self._request_kwargs.get('read_timeout')),
            write_timeout=cast(Optional[float], self._request_kwargs.get('write_timeout')),
            connect_timeout=cast(Optional[float], self._request_kwargs.get('connect_timeout')),
            pool_timeout=cast(Optional[float], self._request_kwargs.get('pool_timeout')),
            media_write_timeout=cast(Optional[float], self._request_kwargs.get('media_write_timeout')),
            connection_pool_size=cast(int, self._request_kwargs.get('connection_pool_size', 1)),
            proxy=cast(Optional[str], self._request_kwargs.get('proxy')),
            httpx_kwargs=cast(Optional[dict[str, object]], self._request_kwargs.get('httpx_kwargs')),
            http_version=cast(Literal['1.1', '2.0', '2'], self._request_kwargs.get('http_version') or '1.1'),
        )

    def initialize(self) -> bool:
        """Call get_me() to validate token and cache identity.
        Returns True on success, False on failure (bot is disabled).
        """
        try:
            validation_bot = self._create_bot()
            me: telegram.User = cast(telegram.User, _resolve_bot_result(validation_bot.get_me(), self._runtime))
            self.bot_id = me.id
            self.username = me.username or ""
            self._rate_limiter.set_owner_id(str(self.bot_id))
            logger.info("Auxiliary bot initialized: @%s (id=%d)", self.username, self.bot_id)
            return True
        except telegram.error.Forbidden as e:
            self.disabled = True
            logger.error("Failed to initialize auxiliary bot: %s", e)
            return False
        except Exception as e:
            self.disabled = True
            logger.error("Failed to initialize auxiliary bot: %s", e)
            return False

    def peek_delay(self, chat_id: int) -> float:
        """Check rate limit delay without reserving a slot. Thread-safe."""
        return self._rate_limiter.peek_delay(chat_id)

    def reserve_slot(self, chat_id: int) -> ReservationOutcome:
        """Reserve a send slot and return its exact token. Thread-safe."""
        return self._rate_limiter.reserve_slot(chat_id)

    def release_slot(self, reservation: Optional[SlotReservation]) -> None:
        """Release an exact reservation when a send did not happen."""
        self._rate_limiter.release_slot(reservation)

    def get_chat_send_count(self, chat_id: int) -> int:
        """Return this bot's current per-chat sliding-window send count."""
        chat_count, _global_count = self._rate_limiter.get_counts(chat_id)
        return chat_count

    def get_chat_count_snapshot(self) -> Tuple[Dict[int, int], int]:
        """Return current per-chat rate-limit occupancy for metrics."""
        return self._rate_limiter.get_chat_count_snapshot()

    def get_reserved_slot_count(self) -> int:
        """Return current global sliding-window reservations for metrics."""
        return self._rate_limiter.get_reserved_slot_count()

    def get_known_member_chat_ids(self) -> set[int]:
        """Return chat IDs where this bot is currently cached as a member."""
        with self._membership_lock:
            return {
                chat_id
                for chat_id, (is_member, _timestamp) in self._membership_cache.items()
                if is_member
            }

    def get_membership_cache_snapshot(self) -> Dict[str, int]:
        """Return membership cache counts without exposing chat IDs."""
        with self._membership_lock:
            member_count = sum(1 for is_member, _timestamp in self._membership_cache.values() if is_member)
            not_member_count = sum(1 for is_member, _timestamp in self._membership_cache.values() if not is_member)
            pending_count = len(self._pending_probes)
        return {
            "member": member_count,
            "not_member": not_member_count,
            "unknown_probe_pending": pending_count,
        }

    def bind_metrics(self, metrics) -> None:
        self._metrics = metrics

    def _record_membership_probe(self, outcome: str) -> None:
        metrics = self._metrics
        if metrics:
            metrics.membership_probe(self.bot_id, self.username, outcome)

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

    def update_membership(self, chat_id: int, is_member: bool):
        """Update the membership cache directly (e.g. from chat_left handler)."""
        with self._membership_lock:
            self._membership_cache[chat_id] = (is_member, time.time())
        if self._membership_changed_callback is not None:
            self._membership_changed_callback()

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
                _resolve_bot_result(self.async_bot.get_chat_member(chat_id, self.bot_id), self._runtime),
            )
            is_member = member.status in ('member', 'administrator', 'creator', 'restricted')
            self.update_membership(chat_id, is_member)
            self._record_membership_probe("ok_member" if is_member else "ok_not_member")
            logger.debug("Membership probe for bot %d in chat %d: %s (status=%s)",
                         self.bot_id, chat_id, is_member, member.status)
        except telegram.error.Forbidden:
            self.update_membership(chat_id, False)
            self._record_membership_probe("forbidden")
            logger.warning("Membership probe for bot %d in chat %d got Forbidden", self.bot_id, chat_id)
        except telegram.error.BadRequest as e:
            self.update_membership(chat_id, False)
            self._record_membership_probe("bad_request")
            logger.debug("Membership probe for bot %d in chat %d failed: %s", self.bot_id, chat_id, e)
        except Exception as e:
            self.update_membership(chat_id, False)
            self._record_membership_probe("error")
            logger.warning("Membership probe failed for bot %d in chat %d: %s", self.bot_id, chat_id, e)
        finally:
            with self._membership_lock:
                self._pending_probes.discard(chat_id)

    def has_pending_probes(self) -> bool:
        """Check if there are any pending membership probes."""
        with self._membership_lock:
            return bool(self._pending_probes)

    def bind_runtime(self, runtime: 'AsyncTelegramRuntime'):
        """Bind the runtime-backed sync facade used by the rest of ETM."""
        from .bot_manager import SyncBotFacade

        self._runtime = runtime
        self.bot = SyncBotFacade(self.async_bot, runtime)

    def __repr__(self):
        return f"AuxiliaryBot(@{self.username}, id={self.bot_id}, disabled={self.disabled})"
