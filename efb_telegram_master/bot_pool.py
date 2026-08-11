# coding=utf-8
"""Auxiliary bot membership and affinity state for outbound sender selection."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Optional, Protocol, cast

from .auxiliary_bot import AuxiliaryBot, MembershipProbeShutdownTimeout
from .telegram_runtime import TelegramPollingRuntime
from .telegram_sync_bridge import AsyncTelegramRuntime
from .utils import normalize_request_kwargs

logger = logging.getLogger(__name__)


class ChannelFlags(Protocol):
    def flag(self, name: str) -> object: ...


def build_bot_pool(
    auxiliary_configs: object,
    config: Mapping[str, object],
    channel: ChannelFlags,
    runtime: AsyncTelegramRuntime,
    logger: logging.Logger,
) -> "BotPool | None":
    """Construct the valid, available auxiliary bots declared in configuration."""
    if not isinstance(auxiliary_configs, list):
        return None
    request_config: dict[str, object] = {
        "read_timeout": 15.0,
        "connection_pool_size": TelegramPollingRuntime._default_connection_pool_size(config),
    }
    configured_request_kwargs = config.get("request_kwargs")
    if isinstance(configured_request_kwargs, Mapping):
        request_config.update(configured_request_kwargs)
    request_kwargs = normalize_request_kwargs(request_config)
    main_token = config["token"]
    seen_tokens = {main_token}
    bots: list[AuxiliaryBot] = []
    for entry in auxiliary_configs:
        if not isinstance(entry, dict) or not isinstance(entry.get("token"), str):
            logger.warning("Skipping invalid auxiliary bot configuration", extra={"event": "telegram_bot.auxiliary_configuration_invalid", "entry_type": type(entry).__name__})
            continue
        token = entry["token"]
        if token in seen_tokens:
            logger.warning("Skipping duplicate auxiliary bot", extra={"event": "telegram_bot.auxiliary_duplicate"})
            continue
        seen_tokens.add(token)
        bot = AuxiliaryBot(
            token=token,
            request_kwargs=request_kwargs,
            base_url=cast(str | None, channel.flag("api_base_url") or None),
            base_file_url=cast(str | None, channel.flag("api_base_file_url") or None),
            local_mode=bool(channel.flag("local_tdlib_api")),
        )
        bot.bind_runtime(runtime)
        if bot.initialize():
            bots.append(bot)
        else:
            logger.error("Skipping unavailable auxiliary bot", extra={"event": "telegram_bot.auxiliary_unavailable"})
    if not bots:
        return None
    logger.info("Initialized auxiliary bot pool", extra={"event": "telegram_bot.auxiliary_initialized", "bot_count": len(bots)})
    return BotPool(bots)


class BotPool:
    """Keep auxiliary membership probes and best-effort slave affinity in memory."""

    def __init__(self, aux_bots: list[AuxiliaryBot]) -> None:
        self._bots = aux_bots
        self._bot_by_id = {bot.bot_id: bot for bot in aux_bots}
        self._lock = threading.Lock()
        self._membership_stopping = False
        self._preferred_sender_by_slave_id: dict[str, int] = {}
        self._membership_failure_slaves: dict[tuple[int, int], set[str]] = {}
        for bot in aux_bots:
            bot._membership_changed_callback = self._membership_changed

    def _membership_changed(self, bot: AuxiliaryBot, chat_id: int, is_member: bool) -> None:
        key = (bot.bot_id, chat_id)
        with self._lock:
            if self._membership_stopping:
                return
            slave_ids = self._membership_failure_slaves.pop(key, set())
            if is_member:
                return
            for slave_id in slave_ids:
                if self._preferred_sender_by_slave_id.get(slave_id) == bot.bot_id:
                    del self._preferred_sender_by_slave_id[slave_id]

    @property
    def bots(self) -> list[AuxiliaryBot]:
        return self._bots

    def get_bot_by_id(self, bot_id: str | int | None) -> Optional[AuxiliaryBot]:
        if bot_id is None:
            return None
        try:
            return self._bot_by_id.get(int(bot_id))
        except (TypeError, ValueError):
            return None

    def candidate_bots(self, chat_id: int) -> list[tuple[AuxiliaryBot, Optional[bool]]]:
        """Probe membership and return enabled auxiliaries in string-ID tie order."""
        candidates: list[tuple[AuxiliaryBot, Optional[bool]]] = []
        for bot in sorted(self._bots, key=lambda item: str(item.bot_id)):
            if not bot.disabled:
                candidates.append((bot, bot.check_membership_tri(chat_id)))
        return candidates

    def preferred_sender(self, slave_id: Optional[str]) -> Optional[AuxiliaryBot]:
        """Return the stored affinity bot when it remains enabled."""
        if slave_id is None:
            return None
        with self._lock:
            bot_id = self._preferred_sender_by_slave_id.get(slave_id)
        bot = self.get_bot_by_id(bot_id)
        return bot if bot is not None and not bot.disabled else None

    def record_successful_auxiliary_send(self, slave_id: Optional[str], bot_id: str | int) -> None:
        """Associate a slave with an auxiliary only after its send succeeds."""
        if slave_id is None:
            return
        bot = self.get_bot_by_id(bot_id)
        if bot is None or bot.disabled:
            return
        with self._lock:
            if self._membership_stopping:
                return
            self._preferred_sender_by_slave_id[slave_id] = bot.bot_id

    def remove_affinity_for_bot(self, bot_id: str | int) -> None:
        """Remove every affinity entry for a disabled auxiliary bot."""
        try:
            normalized_bot_id = int(bot_id)
        except (TypeError, ValueError):
            return
        with self._lock:
            stale_slaves = [slave_id for slave_id, preferred_bot_id in self._preferred_sender_by_slave_id.items() if preferred_bot_id == normalized_bot_id]
            for slave_id in stale_slaves:
                del self._preferred_sender_by_slave_id[slave_id]

    def record_possible_membership_failure(self, slave_id: Optional[str], bot_id: str | int, chat_id: int) -> None:
        """Remember a failed affinity and refresh the auxiliary's membership state."""
        try:
            normalized_bot_id = int(bot_id)
        except (TypeError, ValueError):
            return
        with self._lock:
            if self._membership_stopping:
                return
            if slave_id is not None:
                self._membership_failure_slaves.setdefault((normalized_bot_id, chat_id), set()).add(slave_id)
        bot = self.get_bot_by_id(normalized_bot_id)
        if bot is not None and not bot.disabled:
            bot.recheck_membership(chat_id)

    def disable_bot(self, bot_id: str | int) -> None:
        """Disable an auxiliary and remove every affinity that points to it."""
        bot = self.get_bot_by_id(bot_id)
        if bot is None:
            return
        bot.disabled = True
        self.remove_affinity_for_bot(bot.bot_id)

    def on_bots_joined_chat(self, bot_ids: Sequence[str | int], chat_id: int) -> None:
        for bot_id in bot_ids:
            bot = self.get_bot_by_id(bot_id)
            if bot is not None:
                bot.update_membership(chat_id, True)

    def on_bot_left_chat(self, bot_id: str | int, chat_id: int) -> None:
        bot = self.get_bot_by_id(bot_id)
        if bot is not None:
            bot.update_membership(chat_id, False)

    def begin_shutdown(self) -> None:
        """Reject new membership probes and cancel work that has not started."""
        with self._lock:
            self._membership_stopping = True
        for bot in self._bots:
            bot.begin_membership_shutdown()

    def wait_for_shutdown(self, deadline: float) -> tuple[int, ...]:
        """Join membership workers until the caller-owned absolute deadline."""
        incomplete: list[int] = []
        for bot in self._bots:
            if not bot.wait_for_membership_shutdown(deadline):
                incomplete.append(bot.bot_id)
        return tuple(incomplete)

    def shutdown(self) -> None:
        """Stop membership workers within the five-second delivery shutdown budget."""
        deadline = time.monotonic() + 5.0
        self.begin_shutdown()
        incomplete = self.wait_for_shutdown(deadline)
        if incomplete:
            joined = ", ".join(map(str, incomplete))
            raise MembershipProbeShutdownTimeout(f"Auxiliary membership probes did not stop within 5 seconds for bot IDs: {joined}")

    def auxiliary_count_snapshot(self) -> dict[str, int]:
        """Return configured auxiliary counts grouped by enabled state."""
        enabled = sum(1 for bot in self._bots if not bot.disabled)
        return {"enabled": enabled, "disabled": len(self._bots) - enabled}

    def membership_cache_snapshot(self) -> dict[str, int]:
        """Combine auxiliary membership-cache counts without retaining identities."""
        totals = {"member": 0, "not_member": 0, "unknown_probe_pending": 0}
        for bot in self._bots:
            for state, count in bot.get_membership_cache_snapshot().items():
                totals[state] += count
        return totals

    def rate_limit_occupancy_snapshot(self) -> dict[str, float]:
        """Return the highest current auxiliary occupancy for each limiter scope."""
        occupancy = {"global": 0.0, "chat": 0.0}
        for bot in self._bots:
            for scope, value in bot.rate_limit_occupancy_snapshot().items():
                occupancy[scope] = max(occupancy[scope], value)
        return occupancy

    def __bool__(self) -> bool:
        return bool(self._bots)

    def __len__(self) -> int:
        return len(self._bots)
