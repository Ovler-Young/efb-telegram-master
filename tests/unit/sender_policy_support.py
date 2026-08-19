from __future__ import annotations

from unittest.mock import Mock

from efb_telegram_master.outbound_types import QueuedCall
from efb_telegram_master.runtime.bot_pool import BotPool
from efb_telegram_master.sender_policy import SenderPolicy


class Limiter:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.acquisitions = 0

    def peek_delay(self, _chat_id: int) -> float:
        return self.delay

    def try_acquire(self, _chat_id: int) -> bool:
        self.acquisitions += 1
        return True

    def occupancy_snapshot(self) -> dict[str, float]:
        return {"global": 0.0, "chat": 0.0}


def auxiliary(bot_id: int, *, disabled: bool = False, membership: bool | None = True, delay: float = 0.0) -> Mock:
    result = Mock()
    result.bot_id = bot_id
    result.bot = object()
    result.disabled = disabled
    result.check_membership_tri.return_value = membership
    result.peek_delay.return_value = delay
    result.try_acquire_limits.return_value = True
    return result


def policy(*auxiliaries: Mock, main_delay: float = 0.0, main_bot: object | None = None) -> tuple[SenderPolicy, BotPool | None, object, Limiter]:
    main_bot = main_bot or object()
    limiter = Limiter(main_delay)
    pool = BotPool(list(auxiliaries)) if auxiliaries else None
    return SenderPolicy(main_bot, pool, limiter), pool, main_bot, limiter


def call(*, required_sender_bot_id: str | None = None, slave_id: str | None = None, chat_id: int = 100) -> QueuedCall:
    return QueuedCall("send_message", (), {}, chat_id, slave_id, required_sender_bot_id)
