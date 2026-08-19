import asyncio
from abc import ABC, abstractmethod
from typing import Optional

from ehforwarderbot import Message as EFBMessage
from telethon import TelegramClient
from telethon.tl.custom import Message

TELEGRAM_OPERATION_TIMEOUT = 90


class MessageFactory(ABC):
    """Interface of factory to generate messages."""

    test_quote = True

    @abstractmethod
    async def send_message(self, client: TelegramClient, chat_id: int, target: Message = None) -> Message:
        """Build an initial message to send with."""

    @abstractmethod
    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        """Compare if the Telegram message matches with what is processed by ETM.

        This method should raises ``AssertionError`` if a mismatch is found.
        Otherwise this shall return nothing (i.e. ``None``).
        """

    async def edit_message(self, client: TelegramClient, message: Message) -> Optional[Message]:
        """Issue an edit of the message if applicable.

        Returns the edited message, or none if no edit is needed."""
        return None

    async def edit_message_media(self, client: TelegramClient, message: Message) -> Optional[Message]:
        """Issue a media edit of the message if applicable.

        Returns the edited message, or none if no edit is needed."""
        return None

    async def finalize_message(self, tg_msg: Message, efb_msg: EFBMessage):
        """Finalize the message before discarding if needed."""
        pass

    def __str__(self):
        return self.__class__.__name__


async def run_telegram_operation(factory: MessageFactory, phase: str, operation):
    try:
        return await asyncio.wait_for(operation, timeout=TELEGRAM_OPERATION_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{factory} timed out during {phase} after {TELEGRAM_OPERATION_TIMEOUT} seconds") from exc
