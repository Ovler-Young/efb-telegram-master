import asyncio
import re
import threading
import time
from typing import List, Set

from efb_telegram_master.core import utils as etm_utils

STREAM_INTERVAL_SECONDS = 0.5
STREAM_DURATION_SECONDS = 60.0
STREAM_MESSAGE_COUNT = int(STREAM_DURATION_SECONDS / STREAM_INTERVAL_SECONDS)
STREAM_SETTLE_TIMEOUT = 8 * 60.0
POLL_INTERVAL_SECONDS = 2.0


async def messages_since_id(client, chat_id: int, min_message_id: int, *, limit: int = 200):
    messages = []
    async for message in client.iter_messages(chat_id, limit=limit):
        if message.id <= min_message_id:
            break
        messages.append(message)
    return messages


async def messages_with_prefix(client, chat_id: int, prefix: str, *, min_message_id: int = 0, limit: int = 300):
    messages = []
    async for message in client.iter_messages(chat_id, limit=limit):
        if message.id <= min_message_id:
            break
        if prefix in (message.raw_text or ""):
            messages.append(message)
    return messages


def start_mock_stream(slave, chat, prefix: str, *, expected_count: int = STREAM_MESSAGE_COUNT):
    sent_texts: List[str] = []
    errors: List[BaseException] = []

    def runner():
        try:
            for index in range(expected_count):
                text = f"{prefix} {index:03d}"
                slave.send_text_message(chat, chat.other, text=text)
                sent_texts.append(text)
                if index + 1 < expected_count:
                    time.sleep(STREAM_INTERVAL_SECONDS)
        except BaseException as exc:  # pragma: no cover - thread safety guard
            errors.append(exc)

    thread = threading.Thread(
        target=runner,
        name=f"mock-stream-{prefix}",
        daemon=True,
    )
    thread.start()
    return thread, sent_texts, errors


def extract_stream_indices(text: str, prefix: str) -> List[int]:
    pattern = re.compile(re.escape(prefix) + r"\s+(\d{3})")
    return [int(match.group(1)) for match in pattern.finditer(text)]


def expected_stream_indices(expected_count: int) -> Set[int]:
    return set(range(expected_count))


def logs_with_prefix(channel_with_auxiliary_bots, chat, prefix: str):
    slave_chat_id = etm_utils.chat_id_to_str(chat=chat)
    return [log for log in channel_with_auxiliary_bots.msglogs.get_recent_messages(slave_chat_id, limit=0) if (log.text or "").startswith(prefix)]


async def wait_for_stream_stable(channel_with_auxiliary_bots, client, *, tg_chat_id: int, chat, prefix: str, expected_count: int, min_message_id: int):
    expected = expected_stream_indices(expected_count)
    deadline = time.time() + STREAM_SETTLE_TIMEOUT

    last_debug = ""
    while time.time() < deadline:
        db_logs = logs_with_prefix(channel_with_auxiliary_bots, chat, prefix)
        db_indices = [idx for log in db_logs for idx in extract_stream_indices(log.text or "", prefix)]

        group_messages = await messages_with_prefix(
            client,
            tg_chat_id,
            prefix,
            min_message_id=min_message_id,
            limit=max(2000, expected_count + 400),
        )
        group_indices = [idx for message in group_messages for idx in extract_stream_indices(message.raw_text or "", prefix)]

        last_debug = f"db={len(db_logs)} (idx={len(db_indices)}/{expected_count}), tg={len(group_messages)} (idx={len(group_indices)}/{expected_count})"

        if set(db_indices) == expected and len(db_indices) == expected_count and set(group_indices) == expected and len(group_indices) == expected_count:
            return db_logs, group_messages

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(f"Timed out waiting for stream to settle: {last_debug}")
