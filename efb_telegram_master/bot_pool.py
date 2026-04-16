# coding=utf-8

import logging
import threading
import time
from typing import Optional, List, Dict, Tuple, TYPE_CHECKING

from .auxiliary_bot import AuxiliaryBot

if TYPE_CHECKING:
    from .bot_manager import TelegramBotManager

logger = logging.getLogger(__name__)


class BotPool:
    """Manages auxiliary bot instances and routes sends to the best available bot.

    Key responsibilities:
    - Atomic slot acquisition across all bots
    - O(1) bot lookup by Telegram user ID
    - Throttled user notifications when aux bots need to be added to groups
    - Background membership probes
    """

    NOTIFICATION_COOLDOWN = 3600.0  # 1 hour per (bot_id, chat_id) pair

    def __init__(self, aux_bots: List[AuxiliaryBot], bot_manager: 'TelegramBotManager'):
        self._bots: List[AuxiliaryBot] = aux_bots
        self._bot_by_id: Dict[int, AuxiliaryBot] = {b.bot_id: b for b in aux_bots}
        self._bot_manager = bot_manager
        self._pool_lock = threading.Lock()

        # Notification throttle: (bot_id, chat_id) -> last_notification_timestamp
        self._notification_timestamps: Dict[Tuple[int, int], float] = {}
        self._notification_lock = threading.Lock()

        logger.info("BotPool initialized with %d auxiliary bot(s): %s",
                     len(aux_bots), [f"@{b.username}" for b in aux_bots])

    @property
    def bots(self) -> List[AuxiliaryBot]:
        return self._bots

    def get_bot_by_id(self, bot_id) -> Optional[AuxiliaryBot]:
        """O(1) lookup by Telegram user ID. Returns None if not found."""
        try:
            return self._bot_by_id.get(int(bot_id))
        except (TypeError, ValueError):
            return None

    def acquire_send_slot(self, chat_id: int, max_delay: float = float('inf')
                          ) -> Optional[Tuple[AuxiliaryBot, float]]:
        """Atomically find the best available auxiliary bot for a chat.

        Iterates aux bots confirmed as members of the chat, picks the one
        with the lowest delay, then reserves a slot.

        Args:
            chat_id: Target Telegram chat.
            max_delay: Upper bound on acceptable delay. Bots whose delay
                       >= max_delay are skipped. Pass the main bot's delay
                       so aux bots are only chosen when they're actually faster.

        Returns:
            (AuxiliaryBot, delay) if a suitable aux bot was found,
            None if no aux bot beats max_delay or none are members.
        """
        with self._pool_lock:
            best_bot: Optional[AuxiliaryBot] = None
            best_delay = float('inf')
            need_member_bots: List[AuxiliaryBot] = []

            for aux_bot in self._bots:
                if aux_bot.disabled:
                    continue
                if not aux_bot.check_membership(chat_id):
                    need_member_bots.append(aux_bot)
                    continue

                delay = aux_bot.peek_delay(chat_id)
                if delay < best_delay:
                    best_bot = aux_bot
                    best_delay = delay
                    if delay == 0.0:
                        break

            if best_bot is not None and best_delay < max_delay:
                best_bot.reserve_slot(chat_id)
                return (best_bot, best_delay)

            if need_member_bots:
                self._maybe_notify_admin(chat_id, need_member_bots)

            return None

    def send_blocking(self, chat_id: int, timeout: float = 60.0) -> Optional[AuxiliaryBot]:
        """Block until any bot in the pool has a free slot for the given chat.
        Used by history migration for higher throughput.

        Returns the AuxiliaryBot to use, or None on timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._pool_lock:
                for aux_bot in self._bots:
                    if aux_bot.disabled:
                        continue
                    if not aux_bot.check_membership(chat_id):
                        continue
                    delay = aux_bot.peek_delay(chat_id)
                    if delay == 0.0:
                        aux_bot.reserve_slot(chat_id)
                        return aux_bot
            time.sleep(0.2)
        return None

    def on_bots_joined_chat(self, bot_ids: list, chat_id: int):
        """Update membership cache when aux bots are added to a group."""
        for bot_id in bot_ids:
            aux_bot = self.get_bot_by_id(bot_id)
            if aux_bot:
                aux_bot.update_membership(chat_id, True)
                logger.info("Auxiliary bot @%s added to chat %d, membership cache updated",
                            aux_bot.username, chat_id)

    def on_bot_left_chat(self, bot_id: int, chat_id: int):
        """Update membership cache when an aux bot is removed from a group."""
        aux_bot = self.get_bot_by_id(bot_id)
        if aux_bot:
            aux_bot.update_membership(chat_id, False)
            logger.info("Auxiliary bot @%s removed from chat %d, membership cache updated",
                        aux_bot.username, chat_id)

    def _maybe_notify_admin(self, chat_id: int, bots_not_in_chat: List[AuxiliaryBot]):
        """Send throttled notification to admin about aux bots not in a group."""
        now = time.time()
        bots_to_notify: List[AuxiliaryBot] = []

        with self._notification_lock:
            # Purge stale entries older than cooldown + 60s margin
            cutoff = now - self.NOTIFICATION_COOLDOWN - 60.0
            stale_keys = [k for k, ts in self._notification_timestamps.items() if ts < cutoff]
            for k in stale_keys:
                del self._notification_timestamps[k]

            for aux_bot in bots_not_in_chat:
                key = (aux_bot.bot_id, chat_id)
                last_notified = self._notification_timestamps.get(key, 0.0)
                if now - last_notified >= self.NOTIFICATION_COOLDOWN:
                    self._notification_timestamps[key] = now
                    bots_to_notify.append(aux_bot)

        if not bots_to_notify:
            return

        # Send notification in background to avoid blocking
        thread = threading.Thread(
            target=self._send_admin_notification,
            args=(chat_id, bots_to_notify),
            daemon=True,
            name=f"BotPoolNotify-{chat_id}"
        )
        thread.start()

    def _send_admin_notification(self, chat_id: int, bots: List[AuxiliaryBot]):
        """Send a message to the first admin about adding aux bots to a group."""
        try:
            admin_id = self._bot_manager.admins[0]
            bot_links = ", ".join(
                f'<a href="https://t.me/{b.username}?startgroup=true">@{b.username}</a>'
                for b in bots if b.username
            )
            if not bot_links:
                return

            str_id = str(chat_id)
            if str_id.startswith("-100"):
                chat_url = f"https://t.me/c/{str_id[4:]}"
            else:
                chat_url = f"tg://openmessage?chat_id={chat_id}"

            text = (
                f'📊 Message rate is high in <a href="{chat_url}">chat {chat_id}</a>. '
                f"To reduce delay, please add {bot_links} to the group."
            )

            self._bot_manager.updater.bot.send_message(
                admin_id, text, parse_mode="HTML"
            )
        except Exception as e:
            logger.warning("Failed to send auxiliary bot notification to admin: %s", e)

    def get_pool_stats(self) -> Dict:
        """Return per-bot status for monitoring/debugging."""
        stats = {
            'total_bots': len(self._bots),
            'active_bots': sum(1 for b in self._bots if not b.disabled),
            'bots': []
        }
        for bot in self._bots:
            bot_stats = {
                'username': bot.username,
                'bot_id': bot.bot_id,
                'disabled': bot.disabled,
                'membership_cache_size': len(bot._membership_cache),
            }
            stats['bots'].append(bot_stats)
        return stats

    def shutdown(self):
        """Clean up resources during shutdown.
        Waits for pending membership probes to finish (with timeout)."""
        logger.info("BotPool shutting down, %d auxiliary bot(s)", len(self._bots))

        deadline = time.time() + 5.0
        for aux_bot in self._bots:
            while time.time() < deadline:
                if not aux_bot.has_pending_probes():
                    break
                time.sleep(0.1)
            else:
                if aux_bot.has_pending_probes():
                    logger.warning("Bot @%s still has pending membership probes at shutdown",
                                   aux_bot.username)

        logger.info("BotPool shutdown complete")

    def __bool__(self):
        return len(self._bots) > 0

    def __len__(self):
        return len(self._bots)
