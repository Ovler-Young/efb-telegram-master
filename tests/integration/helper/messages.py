import asyncio
import time
from typing import Callable

from telethon import TelegramClient
from telethon.tl.custom import Message
from telethon.tl.types import MessageEmpty

MESSAGE_STATE_POLL_INTERVAL_SECONDS = 1.0
NEW_MESSAGE_PAGE_SIZE = 20
NEW_MESSAGE_MAX_PAGES_PER_POLL = 3


async def wait_for_message_state(
    client: TelegramClient,
    chat_id: int,
    message_id: int,
    expected: Callable[[Message], bool],
    *,
    timeout: float = 20.0,
) -> Message:
    """Observe one known Telegram message until its state satisfies ``expected``."""
    deadline = time.monotonic() + timeout
    last_state = "missing"
    first_attempt = True
    while True:
        if not first_attempt and time.monotonic() >= deadline:
            raise TimeoutError(f"Telegram message {chat_id}.{message_id} did not reach the expected state within {timeout:g} seconds; last_state={last_state}")
        first_attempt = False
        result = await client.get_messages(chat_id, ids=message_id)
        if isinstance(result, Message):
            last_state = repr(result.to_dict())
            if expected(result):
                return result
        elif result is not None and not isinstance(result, MessageEmpty):
            raise TypeError(f"Telegram get_messages returned {type(result).__name__} for exact message {chat_id}.{message_id}")
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError(f"Telegram message {chat_id}.{message_id} did not reach the expected state within {timeout:g} seconds; last_state={last_state}")
        await asyncio.sleep(min(MESSAGE_STATE_POLL_INTERVAL_SECONDS, remaining))


async def wait_for_new_message_after(
    client: TelegramClient,
    chat_id: int,
    after_message_id: int,
    expected: Callable[[Message], bool],
    *,
    timeout: float = 20.0,
) -> Message:
    """Observe the earliest matching message after a known boundary with bounded pagination."""
    deadline = time.monotonic() + timeout
    last_state = "missing"
    scan_after_message_id = after_message_id
    while True:
        page_after_message_id = scan_after_message_id
        exhausted = False
        for _ in range(NEW_MESSAGE_MAX_PAGES_PER_POLL):
            result = await client.get_messages(
                chat_id,
                min_id=after_message_id,
                offset_id=page_after_message_id,
                limit=NEW_MESSAGE_PAGE_SIZE,
                reverse=True,
            )
            if not isinstance(result, list):
                if result is not None:
                    raise TypeError(f"Telegram get_messages returned {type(result).__name__} for messages after {chat_id}.{after_message_id}")
                exhausted = True
                break
            page = [current for current in result if isinstance(current, Message) and current.id > page_after_message_id]
            if not page:
                exhausted = True
                break
            for current in page:
                last_state = repr(current.to_dict())
                if expected(current):
                    return current
            page_after_message_id = page[-1].id
            if len(page) < NEW_MESSAGE_PAGE_SIZE:
                exhausted = True
                break
        if not exhausted:
            scan_after_message_id = page_after_message_id
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError(f"Telegram did not create the expected message after {chat_id}.{after_message_id} within {timeout:g} seconds; last_state={last_state}")
        await asyncio.sleep(min(MESSAGE_STATE_POLL_INTERVAL_SECONDS, remaining))
