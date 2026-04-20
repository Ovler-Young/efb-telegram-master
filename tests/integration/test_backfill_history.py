import asyncio
import re
import threading
import time
from contextlib import contextmanager
from itertools import chain
from typing import List, Sequence
from uuid import uuid4
from unittest.mock import patch

import pytest
from telethon.tl.functions.messages import CreateChatRequest
from telethon.tl.types import Chat as TelethonChat
from telethon.utils import get_peer_id

from efb_telegram_master import utils as etm_utils
from .helper.filters import has_button, in_chats

pytestmark = pytest.mark.asyncio

STREAM_INTERVAL_SECONDS = 0.5
STREAM_DURATION_SECONDS = 60.0
STREAM_MESSAGE_COUNT = int(STREAM_DURATION_SECONDS / STREAM_INTERVAL_SECONDS)
STREAM_SETTLE_TIMEOUT = 20.0
BACKFILL_WAIT_TIMEOUT = 30.0


@pytest.fixture(scope="module")
def poll_bot(channel_with_auxiliary_bots, poll_bot_factory):
    poll_bot_factory.start(channel_with_auxiliary_bots)
    yield channel_with_auxiliary_bots.bot_manager
    poll_bot_factory.stop(channel_with_auxiliary_bots)


@pytest.fixture(scope="function")
async def helper(helper_wrap, slave_with_auxiliary_bots):
    helper_wrap.clear_queue()
    assert helper_wrap.queue.empty()
    slave_with_auxiliary_bots.clear_messages()
    assert slave_with_auxiliary_bots.messages.empty()
    slave_with_auxiliary_bots.clear_statuses()
    assert slave_with_auxiliary_bots.statuses.empty()
    yield helper_wrap


async def _get_start_token(client, helper, bot_id, chat_uid):
    await client.send_message(bot_id, f"/link {chat_uid}")
    message = await helper.wait_for_message(in_chats(bot_id) & has_button)
    await message.buttons[0][0].click()
    message = await helper.wait_for_message(in_chats(bot_id) & has_button)
    url = None
    for button in chain.from_iterable(message.buttons):
        if button.url:
            url = button.url
            break
    assert url
    return re.search(r"\?startgroup=(.+)", url).groups()[0]


async def _wait_for_text_in_chat(client, chat_id: int, text_fragment: str, *,
                                 min_message_id: int = 0, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        async for message in client.iter_messages(chat_id, limit=50):
            if message.id <= min_message_id:
                break
            if text_fragment in (message.raw_text or ""):
                return message
        await asyncio.sleep(1)
    raise AssertionError(f"Timed out waiting for {text_fragment!r} in chat {chat_id}")


async def _messages_since_id(client, chat_id: int, min_message_id: int, *,
                             limit: int = 200):
    messages = []
    async for message in client.iter_messages(chat_id, limit=limit):
        if message.id <= min_message_id:
            break
        messages.append(message)
    return messages


async def _messages_with_prefix(client, chat_id: int, prefix: str, *,
                                min_message_id: int = 0, limit: int = 300):
    messages = []
    async for message in client.iter_messages(chat_id, limit=limit):
        if message.id <= min_message_id:
            break
        if prefix in (message.raw_text or ""):
            messages.append(message)
    return messages


async def _wait_for_messages_with_prefix(client, chat_id: int, prefix: str, *,
                                         min_message_id: int = 0,
                                         minimum: int = 1,
                                         timeout: float = BACKFILL_WAIT_TIMEOUT):
    deadline = time.time() + timeout
    messages = []
    while time.time() < deadline:
        messages = await _messages_with_prefix(
            client, chat_id, prefix, min_message_id=min_message_id
        )
        if len(messages) >= minimum:
            return messages
        await asyncio.sleep(1)
    raise AssertionError(f"Timed out waiting for {minimum} migrated messages with prefix {prefix!r}")


async def _create_temp_group(client, user_ids: Sequence[int] | int, title: str) -> int:
    users = [user_ids] if isinstance(user_ids, int) else list(user_ids)
    response = await client(CreateChatRequest(users=users, title=title))
    if getattr(response, "chats", None):
        chat = response.chats[0]
    elif getattr(response, "updates", None) and getattr(response.updates, "chats", None):
        chat = response.updates.chats[0]
    else:
        chat = await client.get_entity(title)
    assert isinstance(chat, TelethonChat)
    return get_peer_id(chat)


def _start_mock_stream(slave, chat, prefix: str):
    sent_texts: List[str] = []
    errors: List[BaseException] = []

    def runner():
        try:
            for index in range(STREAM_MESSAGE_COUNT):
                text = f"{prefix} {index:03d}"
                slave.send_text_message(chat, chat.other, text=text)
                sent_texts.append(text)
                if index + 1 < STREAM_MESSAGE_COUNT:
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


async def _wait_for_logged_stream_messages(channel, chat, prefix: str, expected_count: int):
    slave_chat_id = etm_utils.chat_id_to_str(chat=chat)
    deadline = time.time() + STREAM_SETTLE_TIMEOUT
    matches = []
    while time.time() < deadline:
        matches = [
            log for log in channel.db.get_recent_messages(slave_chat_id, limit=0)
            if (log.text or "").startswith(prefix)
        ]
        if len(matches) >= expected_count:
            return matches
        await asyncio.sleep(1)
    raise AssertionError(
        f"Timed out waiting for {expected_count} logged messages for {prefix!r}; got {len(matches)}"
    )


async def _require_aux_membership(channel_with_auxiliary_bots, telegram_chat_id: int):
    pool = channel_with_auxiliary_bots.bot_manager.bot_pool
    assert pool is not None

    working_bot_ids = []
    for aux_bot in pool.bots:
        if await asyncio.to_thread(aux_bot.check_membership_sync, telegram_chat_id, 5.0):
            working_bot_ids.append(aux_bot.bot_id)

    if not working_bot_ids:
        pytest.skip(f"No auxiliary bots are members of test group {telegram_chat_id}")

    return working_bot_ids


@contextmanager
def _prefer_auxiliary_bots_for_stream(channel_with_auxiliary_bots):
    pool = channel_with_auxiliary_bots.bot_manager.bot_pool
    assert pool is not None

    snapshots = []
    for aux_bot in pool.bots:
        snapshots.append((
            aux_bot,
            aux_bot.GLOBAL_LIMIT,
            aux_bot.GLOBAL_WINDOW,
            aux_bot.CHAT_LIMIT,
            aux_bot.CHAT_WINDOW,
        ))
        with aux_bot._rate_limit_lock:
            aux_bot._global_timestamps.clear()
            aux_bot._chat_timestamps.clear()
        aux_bot.GLOBAL_LIMIT = 500
        aux_bot.GLOBAL_WINDOW = 1.0
        aux_bot.CHAT_LIMIT = 500
        aux_bot.CHAT_WINDOW = 60.0

    def force_main_delay(_chat_id, peek_only=False):
        return (1.0, 0, 0)

    with patch.object(
        channel_with_auxiliary_bots.bot_manager,
        "_calculate_rate_limit_delay",
        side_effect=force_main_delay,
    ):
        try:
            yield
        finally:
            for aux_bot, global_limit, global_window, chat_limit, chat_window in snapshots:
                aux_bot.GLOBAL_LIMIT = global_limit
                aux_bot.GLOBAL_WINDOW = global_window
                aux_bot.CHAT_LIMIT = chat_limit
                aux_bot.CHAT_WINDOW = chat_window
                with aux_bot._rate_limit_lock:
                    aux_bot._global_timestamps.clear()
                    aux_bot._chat_timestamps.clear()


async def _link_chat(client, helper, bot_id: int, chat_uid: str, dest_chat_id: int, *,
                     flag: str | None = None):
    token = await _get_start_token(client, helper, bot_id, chat_uid)
    command = f"/start {token}"
    if flag is not None:
        command += f" {flag}"
    command_message = await client.send_message(dest_chat_id, command)
    await _wait_for_text_in_chat(
        client,
        dest_chat_id,
        "is now linked.",
        min_message_id=command_message.id,
        timeout=30.0,
    )
    return command_message


async def _generate_stream_history(channel_with_auxiliary_bots, client, helper, bot_id, source_group_id,
                                   slave_with_auxiliary_bots):
    chat = slave_with_auxiliary_bots.chat_with_alias
    channel_with_auxiliary_bots.db.remove_chat_assoc(
        slave_uid=etm_utils.chat_id_to_str(chat=chat)
    )

    prefix = f"STREAM{uuid4().hex[:10]}"
    await _link_chat(client, helper, bot_id, chat.uid, source_group_id)

    with _prefer_auxiliary_bots_for_stream(channel_with_auxiliary_bots):
        stream_thread, _, stream_errors = _start_mock_stream(slave_with_auxiliary_bots, chat, prefix)
        await asyncio.to_thread(stream_thread.join, STREAM_DURATION_SECONDS + 10.0)

    assert not stream_thread.is_alive(), "Mock stream did not finish in time."
    assert not stream_errors, f"Mock stream failed: {stream_errors!r}"

    history_logs = await _wait_for_logged_stream_messages(
        channel_with_auxiliary_bots, chat, prefix, STREAM_MESSAGE_COUNT
    )
    return chat, prefix, history_logs


async def test_auxiliary_bots_stream_messages_to_group(channel_with_auxiliary_bots, helper, client, bot_id,
                                                       bot_group, aux_bot_ids, slave_with_auxiliary_bots):
    source_group_id = await _create_temp_group(
        client,
        [bot_id, *aux_bot_ids],
        f"Aux stream source {uuid4()}",
    )
    working_aux_bot_ids = await _require_aux_membership(channel_with_auxiliary_bots, source_group_id)
    chat, prefix, history_logs = await _generate_stream_history(
        channel_with_auxiliary_bots, client, helper, bot_id, source_group_id, slave_with_auxiliary_bots
    )

    try:
        assert len(history_logs) == STREAM_MESSAGE_COUNT

        db_sender_ids = {
            int(log.sender_bot_id)
            for log in history_logs
            if log.sender_bot_id is not None
        }
        assert db_sender_ids & set(working_aux_bot_ids), "Expected at least one stream message to be sent by an auxiliary bot."

        group_messages = await _messages_with_prefix(client, source_group_id, prefix)
        assert len(group_messages) == STREAM_MESSAGE_COUNT
        group_sender_ids = {message.sender_id for message in group_messages if message.sender_id is not None}
        assert group_sender_ids & set(aux_bot_ids), "Expected auxiliary bot messages in the linked group."

        private_messages = await _messages_with_prefix(client, bot_id, prefix)
        assert not private_messages, "Streamed messages should stay in the linked group, not the admin private chat."
    finally:
        channel_with_auxiliary_bots.db.remove_chat_assoc(
            slave_uid=etm_utils.chat_id_to_str(chat=chat)
        )


async def test_relink_true_migrates_real_stream_history(channel_with_auxiliary_bots, helper, client, bot_id,
                                                        aux_bot_ids, slave_with_auxiliary_bots):
    source_group_id = await _create_temp_group(
        client,
        [bot_id, *aux_bot_ids],
        f"Backfill true source {uuid4()}",
    )
    await _require_aux_membership(channel_with_auxiliary_bots, source_group_id)
    chat, prefix, history_logs = await _generate_stream_history(
        channel_with_auxiliary_bots, client, helper, bot_id, source_group_id, slave_with_auxiliary_bots
    )
    target_group_id = await _create_temp_group(client, bot_id, f"Backfill true {uuid4()}")

    try:
        assert len(history_logs) == STREAM_MESSAGE_COUNT

        command_message = await _link_chat(client, helper, bot_id, chat.uid, target_group_id, flag="true")
        migrated_messages = await _wait_for_messages_with_prefix(
            client,
            target_group_id,
            prefix,
            min_message_id=command_message.id,
            minimum=1,
        )

        recent_messages = await _messages_since_id(client, target_group_id, command_message.id)
        assert migrated_messages, "Expected backfilled history in the relinked group."
        assert not any(
            "History messages are not migrated" in (message.raw_text or "")
            for message in recent_messages
        ), "Relink with true should migrate history instead of sending the history-link notice."
    finally:
        channel_with_auxiliary_bots.db.remove_chat_assoc(
            slave_uid=etm_utils.chat_id_to_str(chat=chat)
        )


async def test_relink_false_skips_real_stream_history(channel_with_auxiliary_bots, helper, client, bot_id,
                                                      aux_bot_ids, slave_with_auxiliary_bots):
    source_group_id = await _create_temp_group(
        client,
        [bot_id, *aux_bot_ids],
        f"Backfill false source {uuid4()}",
    )
    await _require_aux_membership(channel_with_auxiliary_bots, source_group_id)
    chat, prefix, history_logs = await _generate_stream_history(
        channel_with_auxiliary_bots, client, helper, bot_id, source_group_id, slave_with_auxiliary_bots
    )
    target_group_id = await _create_temp_group(client, bot_id, f"Backfill false {uuid4()}")

    try:
        assert len(history_logs) == STREAM_MESSAGE_COUNT

        command_message = await _link_chat(client, helper, bot_id, chat.uid, target_group_id, flag="false")
        await asyncio.sleep(10)
        recent_messages = await _messages_since_id(client, target_group_id, command_message.id)

        assert not any(prefix in (message.raw_text or "") for message in recent_messages), (
            "Relink with false should skip migrating historical messages."
        )
        assert not any(
            "History messages are not migrated" in (message.raw_text or "")
            for message in recent_messages
        ), "Relink with false should skip both history migration and the history-link notice."
    finally:
        channel_with_auxiliary_bots.db.remove_chat_assoc(
            slave_uid=etm_utils.chat_id_to_str(chat=chat)
        )
