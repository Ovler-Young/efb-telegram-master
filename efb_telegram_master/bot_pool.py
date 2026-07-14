# coding=utf-8
"""Auxiliary bot membership and affinity state for outbound sender selection."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from .auxiliary_bot import AuxiliaryBot

if TYPE_CHECKING:
    from .bot_manager import TelegramBotManager

logger = logging.getLogger(__name__)


class BotPool:
    """Keep auxiliary membership probes and best-effort slave affinity in memory."""

    def __init__(self, aux_bots: list[AuxiliaryBot], bot_manager: 'TelegramBotManager') -> None:
        self._bots = aux_bots
        self._bot_by_id = {bot.bot_id: bot for bot in aux_bots}
        self._bot_manager = bot_manager
        self._lock = threading.Lock()
        self._preferred_sender_by_slave_id: dict[str, int] = {}
        for bot in self._bots:
            bot._membership_changed_callback = self._wake_scheduler

    def _wake_scheduler(self) -> None:
        scheduler = getattr(self._bot_manager, "_outbound_scheduler", None)
        wake_event = getattr(scheduler, "wake_event", None)
        if wake_event is not None:
            wake_event.set()

    @property
    def bots(self) -> list[AuxiliaryBot]:
        return self._bots

    def get_bot_by_id(self, bot_id: object) -> Optional[AuxiliaryBot]:
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

    def record_successful_auxiliary_send(self, slave_id: Optional[str], bot_id: object) -> None:
        """Associate a slave with an auxiliary only after its send succeeds."""
        if slave_id is None:
            return
        bot = self.get_bot_by_id(bot_id)
        if bot is None or bot.disabled:
            return
        with self._lock:
            self._preferred_sender_by_slave_id[slave_id] = bot.bot_id

    def remove_affinity_for_bot(self, bot_id: object) -> None:
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

    def remove_failed_membership_affinity(self, slave_id: Optional[str], bot_id: object) -> None:
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

    def disable_bot(self, bot_id: object) -> None:
        """Disable an auxiliary and remove every affinity that points to it."""
        bot = self.get_bot_by_id(bot_id)
        if bot is None:
            return
        bot.disabled = True
        self.remove_affinity_for_bot(bot.bot_id)

    def on_bots_joined_chat(self, bot_ids: list[object], chat_id: int) -> None:
        for bot_id in bot_ids:
            bot = self.get_bot_by_id(bot_id)
            if bot is not None:
                bot.update_membership(chat_id, True)

    def on_bot_left_chat(self, bot_id: object, chat_id: int) -> None:
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
