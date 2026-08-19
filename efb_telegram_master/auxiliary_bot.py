# coding=utf-8

import logging
import threading
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Callable, Optional, TypeVar, overload

import telegram
import telegram.error

from .membership_lifecycle import MembershipLifecycle, MembershipProbeMetrics

if TYPE_CHECKING:
    from .transport.telegram_sync_bridge import AsyncTelegramRuntime, SyncBotFacade

logger = logging.getLogger(__name__)

T = TypeVar("T")


@overload
def _resolve_bot_result(result: Coroutine[object, object, T], runtime: Optional["AsyncTelegramRuntime"], *, timeout: float | None = None) -> T: ...


@overload
def _resolve_bot_result(result: T, runtime: Optional["AsyncTelegramRuntime"], *, timeout: float | None = None) -> T: ...


def _resolve_bot_result(result: object, runtime: Optional["AsyncTelegramRuntime"], *, timeout: float | None = None) -> object:
    if isinstance(result, Coroutine):
        if runtime is None:
            raise RuntimeError("Auxiliary bot runtime is not bound.")
        return runtime.call(result, timeout=timeout)
    return result


class MembershipProbeShutdownTimeout(RuntimeError):
    """Membership probe workers did not finish before their shutdown deadline."""


class AuxiliaryBot:
    """Telegram identity and transport for a send-only auxiliary bot."""

    MEMBERSHIP_TTL_MEMBER = 1800.0
    MEMBERSHIP_TTL_NOT_MEMBER = 300.0
    MEMBERSHIP_PROBE_WORKERS = 2
    MAX_PENDING_MEMBERSHIP_PROBES = 32
    MAX_MEMBERSHIP_CACHE_ENTRIES = 512
    MEMBERSHIP_PROBE_TIMEOUT = 4.0

    def __init__(self, token: str, *, request_kwargs: Optional[dict[str, object]] = None, base_url: Optional[str] = None, base_file_url: Optional[str] = None, local_mode: bool = False):
        self._token = token
        self._request_kwargs = dict(request_kwargs or {})
        self._base_kwargs: dict[str, str] = {}
        if base_url:
            self._base_kwargs["base_url"] = base_url
        if base_file_url:
            self._base_kwargs["base_file_url"] = base_file_url
        self._local_mode = local_mode
        self.async_bot: telegram.Bot = self._create_bot()
        self.bot: telegram.Bot | "SyncBotFacade" = self.async_bot
        self.bot_id = 0
        self.username = ""
        self.disabled = False
        self._runtime: Optional["AsyncTelegramRuntime"] = None

        from .runtime.rate_limiter import SlidingWindowRateLimiter

        self._rate_limiter = SlidingWindowRateLimiter()
        self._membership_lifecycle = MembershipLifecycle(
            bot_id=lambda: self.bot_id,
            probe_member=self._get_chat_member_for_probe,
            probe_workers=self.MEMBERSHIP_PROBE_WORKERS,
            max_pending_probes=self.MAX_PENDING_MEMBERSHIP_PROBES,
            max_cache_entries=self.MAX_MEMBERSHIP_CACHE_ENTRIES,
            member_ttl=self.MEMBERSHIP_TTL_MEMBER,
            not_member_ttl=self.MEMBERSHIP_TTL_NOT_MEMBER,
            probe_timeout=self.MEMBERSHIP_PROBE_TIMEOUT,
        )
        self._membership_callback: Callable[["AuxiliaryBot", int, bool], None] | None = None

    def _create_bot(self) -> telegram.Bot:
        from .transport.telegram_runtime import build_request

        request = build_request(self._request_kwargs) if self._request_kwargs else None
        get_updates_request = build_request(self._request_kwargs) if self._request_kwargs else None
        base_url = self._base_kwargs.get("base_url")
        base_file_url = self._base_kwargs.get("base_file_url")
        if base_url is not None and base_file_url is not None:
            return telegram.Bot(self._token, base_url=base_url, base_file_url=base_file_url, request=request, get_updates_request=get_updates_request, local_mode=self._local_mode)
        if base_url is not None:
            return telegram.Bot(self._token, base_url=base_url, request=request, get_updates_request=get_updates_request, local_mode=self._local_mode)
        if base_file_url is not None:
            return telegram.Bot(self._token, base_file_url=base_file_url, request=request, get_updates_request=get_updates_request, local_mode=self._local_mode)
        return telegram.Bot(self._token, request=request, get_updates_request=get_updates_request, local_mode=self._local_mode)

    def initialize(self) -> bool:
        try:
            validation_bot = self._create_bot()
            me = _resolve_bot_result(validation_bot.get_me(), self._runtime)
            self.bot_id = me.id
            self.username = me.username or ""
            logger.info("Auxiliary bot initialized: @%s (id=%d)", self.username, self.bot_id)
            return True
        except telegram.error.Forbidden as error:
            self.disabled = True
            logger.error("Failed to initialize auxiliary bot: %s", error)
            return False
        except Exception as error:
            self.disabled = True
            logger.error("Failed to initialize auxiliary bot: %s", error)
            return False

    def _get_chat_member_for_probe(self, chat_id: int) -> object:
        return _resolve_bot_result(self.async_bot.get_chat_member(chat_id, self.bot_id), self._runtime, timeout=self.MEMBERSHIP_PROBE_TIMEOUT)

    def peek_delay(self, chat_id: int) -> float:
        return self._rate_limiter.peek_delay(chat_id)

    def try_acquire_limits(self, chat_id: int) -> bool:
        return self._rate_limiter.try_acquire(chat_id)

    def get_chat_send_count(self, chat_id: int) -> int:
        chat_count, _global_count = self._rate_limiter.get_counts(chat_id)
        return chat_count

    def rate_limit_occupancy_snapshot(self) -> dict[str, float]:
        return self._rate_limiter.occupancy_snapshot()

    def get_known_member_chat_ids(self) -> set[int]:
        return self._membership_lifecycle.get_known_member_chat_ids()

    def get_membership_cache_snapshot(self) -> dict[str, int]:
        return self._membership_lifecycle.get_cache_snapshot()

    def bind_metrics(self, metrics: MembershipProbeMetrics) -> None:
        self._membership_lifecycle.bind_metrics(metrics)

    def check_membership_tri(self, chat_id: int) -> bool | None:
        return self._membership_lifecycle.check(chat_id)

    def update_membership(self, chat_id: int, is_member: bool) -> None:
        self._membership_lifecycle.update(chat_id, is_member)

    def recheck_membership(self, chat_id: int) -> None:
        self._membership_lifecycle.recheck(chat_id)

    def begin_membership_shutdown(self) -> None:
        self._membership_lifecycle.begin_shutdown()

    def wait_for_membership_shutdown(self, deadline: float) -> bool:
        return self._membership_lifecycle.wait_for_shutdown(deadline)

    def has_pending_probes(self) -> bool:
        return self._membership_lifecycle.has_pending_probes()

    @property
    def _membership_changed_callback(self) -> Callable[["AuxiliaryBot", int, bool], None] | None:
        return self._membership_callback

    @_membership_changed_callback.setter
    def _membership_changed_callback(self, callback: Callable[["AuxiliaryBot", int, bool], None] | None) -> None:
        self._membership_callback = callback
        if callback is None:
            self._membership_lifecycle.set_membership_changed_callback(None)
        else:
            self._membership_lifecycle.set_membership_changed_callback(lambda chat_id, is_member: callback(self, chat_id, is_member))

    @property
    def _membership_probe_workers(self) -> set[threading.Thread]:
        return self._membership_lifecycle._membership_probe_workers

    def bind_runtime(self, runtime: "AsyncTelegramRuntime") -> None:
        from .transport.telegram_sync_bridge import SyncBotFacade

        self._runtime = runtime
        self.bot = SyncBotFacade(self.async_bot, runtime)

    def __repr__(self) -> str:
        return f"AuxiliaryBot(@{self.username}, id={self.bot_id}, disabled={self.disabled})"
