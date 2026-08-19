"""Values exchanged by slave message routing and delivery."""

from dataclasses import dataclass
from typing import Optional

from efb_telegram_master.core.utils import TelegramChatID, TelegramTopicID


@dataclass(frozen=True)
class DeliveryPlan:
    message_template: str
    destination: TelegramChatID
    thread_id: Optional[TelegramTopicID]
