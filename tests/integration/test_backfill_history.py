import asyncio
import re
import threading
import time
from itertools import chain
from typing import List, Sequence, Set
from uuid import uuid4

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
STREAM_SETTLE_TIMEOUT = 8 * 60.0
BACKFILL_WAIT_TIMEOUT = 6 * 60.0
POLL_INTERVAL_SECONDS = 2.0


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


def _start_mock_stream(slave, chat, prefix: str, *, expected_count: int = STREAM_MESSAGE_COUNT):
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
        if await asyncio.to_thread(aux_bot.check_membership_sync, telegram_chat_id, 15.0):
            working_bot_ids.append(aux_bot.bot_id)

    return working_bot_ids


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


def _extract_stream_indices(text: str, prefix: str) -> List[int]:
    pattern = re.compile(re.escape(prefix) + r"\s+(\d{3})")
    return [int(match.group(1)) for match in pattern.finditer(text)]


def _expected_stream_indices(expected_count: int) -> Set[int]:
    return set(range(expected_count))


def _delayed_state(bot_manager):
    with bot_manager._delayed_queue_lock:
        queue_len = len(bot_manager._delayed_queue)
    with bot_manager._pending_logs_lock:
        pending_len = len(bot_manager._pending_delayed_logs)
        completed_len = len(bot_manager._completed_delayed_results)
    return queue_len, pending_len, completed_len


def _logs_with_prefix(channel_with_auxiliary_bots, chat, prefix: str):
    slave_chat_id = etm_utils.chat_id_to_str(chat=chat)
    return [
        log for log in channel_with_auxiliary_bots.db.get_recent_messages(slave_chat_id, limit=0)
        if (log.text or "").startswith(prefix)
    ]


async def _wait_for_stream_stable(channel_with_auxiliary_bots, client, *, tg_chat_id: int, chat, prefix: str,
                                  expected_count: int, min_message_id: int):
    expected = _expected_stream_indices(expected_count)
    deadline = time.time() + STREAM_SETTLE_TIMEOUT

    last_debug = ""
    while time.time() < deadline:
        db_logs = _logs_with_prefix(channel_with_auxiliary_bots, chat, prefix)
        db_indices = {
            idx
            for log in db_logs
            for idx in _extract_stream_indices(log.text or "", prefix)
        }

        group_messages = await _messages_with_prefix(
            client,
            tg_chat_id,
            prefix,
            min_message_id=min_message_id,
            limit=max(2000, expected_count + 400),
        )
        group_indices = {
            idx
            for message in group_messages
            for idx in _extract_stream_indices(message.raw_text or "", prefix)
        }

        queue_len, pending_len, completed_len = _delayed_state(channel_with_auxiliary_bots.bot_manager)
        last_debug = (
            f"db={len(db_logs)} (idx={len(db_indices)}/{expected_count}), "
            f"tg={len(group_messages)} (idx={len(group_indices)}/{expected_count}), "
            f"delayed_queue={queue_len}, pending_logs={pending_len}, completed_results={completed_len}"
        )

        if db_indices == expected and group_indices == expected and (queue_len, pending_len, completed_len) == (0, 0, 0):
            return db_logs, group_messages

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(f"Timed out waiting for stream to settle: {last_debug}")


async def _wait_for_migrated_stream_indices(client, chat_id: int, prefix: str, expected_count: int, *,
                                            min_message_id: int, timeout: float = BACKFILL_WAIT_TIMEOUT):
    expected = _expected_stream_indices(expected_count)
    deadline = time.time() + timeout

    last_debug = ""
    while time.time() < deadline:
        recent = await _messages_since_id(client, chat_id, min_message_id)
        found = {
            idx
            for message in recent
            for idx in _extract_stream_indices(message.raw_text or "", prefix)
        }
        last_debug = f"migrated_idx={len(found)}/{expected_count}, messages_scanned={len(recent)}"
        if found == expected:
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(f"Timed out waiting for migrated stream indices: {last_debug}")


async def test_auxiliary_bots_stream_blackbox_and_relink(channel_with_auxiliary_bots, helper, client, bot_id,
                                                         bot_group, bot_topic_group, aux_bot_ids,
                                                         slave_with_auxiliary_bots):
    # Prefer the pre-configured test group; if aux bots are not members, fall back to a temporary group.
    source_group_id = bot_group
    working_aux_bot_ids = await _require_aux_membership(channel_with_auxiliary_bots, source_group_id)
    if not working_aux_bot_ids:
        source_group_id = await _create_temp_group(
            client,
            [bot_id, *aux_bot_ids],
            f"Aux stream source {uuid4()}",
        )
        working_aux_bot_ids = await _require_aux_membership(channel_with_auxiliary_bots, source_group_id)
        if not working_aux_bot_ids:
            pytest.skip(f"No auxiliary bots are members of test group {source_group_id}")

    chat = slave_with_auxiliary_bots.chat_with_alias
    slave_uid = etm_utils.chat_id_to_str(chat=chat)

    # Ensure a clean link state (DB + cached chat object).
    etm_chat = channel_with_auxiliary_bots.chat_manager.get_chat(chat.module_id, chat.uid)
    assert etm_chat is not None
    etm_chat.unlink()
    channel_with_auxiliary_bots.db.remove_topic_assoc(slave_uid=slave_uid)

    prefix = f"AUXSEND{uuid4().hex[:10]}"
    command_message = await _link_chat(client, helper, bot_id, chat.uid, source_group_id)

    bot_manager = channel_with_auxiliary_bots.bot_manager
    task_counter_before = bot_manager._task_counter

    stream_thread, sent_texts, stream_errors = _start_mock_stream(slave_with_auxiliary_bots, chat, prefix)
    await asyncio.to_thread(stream_thread.join, STREAM_DURATION_SECONDS + 15.0)

    assert not stream_thread.is_alive(), "Mock stream did not finish in time."
    assert not stream_errors, f"Mock stream failed: {stream_errors!r}"
    assert len(sent_texts) == STREAM_MESSAGE_COUNT

    db_logs, group_messages = await _wait_for_stream_stable(
        channel_with_auxiliary_bots,
        client,
        tg_chat_id=source_group_id,
        chat=chat,
        prefix=prefix,
        expected_count=STREAM_MESSAGE_COUNT,
        min_message_id=command_message.id,
    )

    # 1) Telegram: received all 120 (no missing/duplicate indices).
    group_indices = [idx for msg in group_messages for idx in _extract_stream_indices(msg.raw_text or "", prefix)]
    assert set(group_indices) == _expected_stream_indices(STREAM_MESSAGE_COUNT)
    assert len(group_indices) == STREAM_MESSAGE_COUNT, "Expected exactly 120 stream messages in the Telegram group."

    # 2) DB: all 120 are logged (no missing/duplicate indices).
    db_indices = [idx for log in db_logs for idx in _extract_stream_indices(log.text or "", prefix)]
    assert set(db_indices) == _expected_stream_indices(STREAM_MESSAGE_COUNT)
    assert len(db_indices) == STREAM_MESSAGE_COUNT, "Expected exactly 120 logged stream messages in DB."

    # 3) aux actually participated in sending.
    db_sender_ids = {
        int(log.sender_bot_id)
        for log in db_logs
        if log.sender_bot_id is not None
    }
    assert db_sender_ids & set(working_aux_bot_ids), "Expected at least one stream message to be sent by an auxiliary bot."

    group_sender_ids = {message.sender_id for message in group_messages if message.sender_id is not None}
    assert group_sender_ids & set(working_aux_bot_ids), "Expected auxiliary bot messages in the linked group."

    # 4) Delayed queue was used, and there is no pending delayed DB update residue.
    assert bot_manager._task_counter - task_counter_before >= STREAM_MESSAGE_COUNT
    assert _delayed_state(bot_manager) == (0, 0, 0)

    # ---- Relink/migration checks (same stream history) ----

    target_group_id = await _create_temp_group(client, bot_id, f"Backfill true {uuid4()}")
    try:
        relink_true_message = await _link_chat(client, helper, bot_id, chat.uid, target_group_id, flag="true")
        await _wait_for_migrated_stream_indices(
            client,
            target_group_id,
            prefix,
            STREAM_MESSAGE_COUNT,
            min_message_id=relink_true_message.id,
        )

        recent_messages = await _messages_since_id(client, target_group_id, relink_true_message.id)
        assert not any(
            "History messages are not migrated" in (message.raw_text or "")
            for message in recent_messages
        ), "Relink with true should migrate history instead of sending the history-link notice."

        # Relink false -> topic group, if configured (skips both migration and history-link notice).
        if bot_topic_group is not None:
            relink_false_message = await _link_chat(client, helper, bot_id, chat.uid, bot_topic_group, flag="false")
            await asyncio.sleep(10)
            recent_topic_messages = await _messages_since_id(client, bot_topic_group, relink_false_message.id)
            assert not any(prefix in (message.raw_text or "") for message in recent_topic_messages), (
                "Relink with false should skip migrating historical messages."
            )
            assert not any(
                "History messages are not migrated" in (message.raw_text or "")
                for message in recent_topic_messages
            ), "Relink with false should skip both history migration and the history-link notice."
    finally:
        # Keep cached chat state consistent across tests.
        etm_chat.unlink()
