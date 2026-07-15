# coding=utf-8
"""Auxiliary bot membership and affinity state for outbound sender selection."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .auxiliary_bot import AuxiliaryBot

if TYPE_CHECKING:
    from .bot_manager import TelegramBotManager

logger = logging.getLogger(__name__)


MEMBERSHIP_RECHECK_SECONDS = 0.25


@dataclass(frozen=True)
class AuxiliarySenderState:
    """Read-only auxiliary sender input for one scheduler arbitration pass.

    The scheduler supplies the monotonic cooldown deadline, because cooldowns
    are owned by its RetryAfter handling. ``tie_key`` orders auxiliaries around
    the main sender: preferred auxiliary (0), main sender (1, supplied by the
    scheduler), then other auxiliaries (2, ascending string bot ID).
    """

    bot: Optional[AuxiliaryBot]
    bot_id: str
    membership: Optional[bool]
    limiter_delay: float
    cooldown_until: float
    affinity_rank: int

    @property
    def is_terminal(self) -> bool:
        """Whether a required-sender task can never use this auxiliary."""
        return self.bot is None or self.bot.disabled or self.membership is False

    @property
    def tie_key(self) -> tuple[int, str]:
        """Return the deterministic auxiliary portion of sender ordering."""
        return (self.affinity_rank, self.bot_id)

    def is_selectable(self, now: float) -> bool:
        """Return whether membership, cooldown, and limiter allow acquisition."""
        return (
            not self.is_terminal
            and self.membership is True
            and self.limiter_delay <= 0.0
            and self.cooldown_until <= now
        )

    def next_deadline(self, now: float) -> float:
        """Return the next monotonic recheck time, or infinity when terminal."""
        if self.is_terminal:
            return float("inf")
        membership_deadline = now + MEMBERSHIP_RECHECK_SECONDS if self.membership is None else now
        return max(membership_deadline, now + self.limiter_delay, self.cooldown_until)


class BotPool:
    """Keep auxiliary membership probes and best-effort slave affinity in memory."""

    def __init__(self, aux_bots: list[AuxiliaryBot], bot_manager: 'TelegramBotManager') -> None:
        self._bots = aux_bots
        self._bot_by_id = {bot.bot_id: bot for bot in aux_bots}
        self._bot_manager = bot_manager
        self._lock = threading.Lock()
        self._preferred_sender_by_slave_id: dict[str, int] = {}
        for bot in self._bots:
            bot._membership_changed_callback = self._membership_changed

    def _membership_changed(self, bot: AuxiliaryBot, chat_id: int, is_member: bool) -> None:
        if not is_member:
            self._bot_manager.remove_confirmed_non_member_affinity_for_sender_chat(
                str(bot.bot_id), chat_id
            )
        scheduler = getattr(self._bot_manager, "_outbound_scheduler", None)
        wake_event = getattr(scheduler, "wake_event", None)
        if wake_event is not None:
            wake_event.set()

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

    def required_sender_state(
        self,
        bot_id: str | int | None,
        chat_id: int,
        *,
        cooldown_until: float,
        now: float,
    ) -> AuxiliarySenderState:
        """Return the required auxiliary's availability and next-deadline inputs.

        Missing and disabled bots are terminal. A membership value of ``None``
        means the existing asynchronous probe has been requested and the
        scheduler must recheck after ``MEMBERSHIP_RECHECK_SECONDS``.
        """
        bot = self.get_bot_by_id(bot_id)
        normalized_bot_id = str(bot_id)
        if bot is None or bot.disabled:
            return AuxiliarySenderState(
                bot=bot,
                bot_id=normalized_bot_id if bot is None else str(bot.bot_id),
                membership=False,
                limiter_delay=0.0,
                cooldown_until=cooldown_until,
                affinity_rank=2,
            )
        membership = bot.check_membership_tri(chat_id)
        return AuxiliarySenderState(
            bot=bot,
            bot_id=str(bot.bot_id),
            membership=membership,
            limiter_delay=bot.peek_delay(chat_id),
            cooldown_until=cooldown_until,
            affinity_rank=2,
        )

    def affinity_sender_states(
        self,
        chat_id: int,
        slave_id: Optional[str],
        *,
        cooldown_until_for_bot: Callable[[str, int], float],
        now: float,
    ) -> list[AuxiliarySenderState]:
        """Return enabled auxiliary state for affinity-only arbitration.

        The result retains unknown-membership auxiliaries so the scheduler can
        wait for their 250 ms deadline. Confirmed non-members and disabled bots
        are omitted because they are not affinity-only candidates.
        """
        preferred = self.preferred_sender(slave_id)
        preferred_bot_id = None if preferred is None else str(preferred.bot_id)
        states: list[AuxiliarySenderState] = []
        for bot, membership in self.candidate_bots(chat_id):
            if membership is False:
                continue
            bot_id = str(bot.bot_id)
            states.append(
                AuxiliarySenderState(
                    bot=bot,
                    bot_id=bot_id,
                    membership=membership,
                    limiter_delay=bot.peek_delay(chat_id),
                    cooldown_until=cooldown_until_for_bot(bot_id, chat_id),
                    affinity_rank=0 if bot_id == preferred_bot_id else 2,
                )
            )
        return states

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
            self._preferred_sender_by_slave_id[slave_id] = bot.bot_id

    def remove_affinity_for_bot(self, bot_id: str | int) -> None:
        """Remove every affinity entry for a disabled auxiliary bot."""
        try:
            normalized_bot_id = int(bot_id)
        except (TypeError, ValueError):
            return
        with self._lock:
            stale_slaves = [
                slave_id
                for slave_id, preferred_bot_id in self._preferred_sender_by_slave_id.items()
                if preferred_bot_id == normalized_bot_id
            ]
            for slave_id in stale_slaves:
                del self._preferred_sender_by_slave_id[slave_id]

    def remove_failed_membership_affinity(self, slave_id: Optional[str], bot_id: str | int) -> None:
        """Remove only the failed task's matching affinity entry."""
        if slave_id is None:
            return
        try:
            normalized_bot_id = int(bot_id)
        except (TypeError, ValueError):
            return
        with self._lock:
            if self._preferred_sender_by_slave_id.get(slave_id) == normalized_bot_id:
                del self._preferred_sender_by_slave_id[slave_id]

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

    def shutdown(self) -> None:
        """Wait up to five seconds for asynchronous membership probes."""
        deadline = time.monotonic() + 5.0
        for bot in self._bots:
            while bot.has_pending_probes() and time.monotonic() < deadline:
                time.sleep(0.1)

    def __bool__(self) -> bool:
        return bool(self._bots)

    def __len__(self) -> int:
        return len(self._bots)
