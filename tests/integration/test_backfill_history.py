import asyncio
import re
import threading
import time
from typing import List, Set
from uuid import uuid4

import pytest

from efb_telegram_master import utils as etm_utils
from efb_telegram_master.callback_sessions import ChatListStorage
from efb_telegram_master.constants import Flags
from efb_telegram_master.models import HistoryMigrationEntry
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID

from .utils import decode_start_link_token, get_start_link

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


async def _wait_for_text_in_chat(client, chat_id: int, text_fragment: str, *, min_message_id: int = 0, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        async for message in client.iter_messages(chat_id, limit=50):
            if message.id <= min_message_id:
                break
            if text_fragment in (message.raw_text or ""):
                return message
        await asyncio.sleep(1)
    raise AssertionError(f"Timed out waiting for {text_fragment!r} in chat {chat_id}")


async def _messages_since_id(client, chat_id: int, min_message_id: int, *, limit: int = 200):
    messages = []
    async for message in client.iter_messages(chat_id, limit=limit):
        if message.id <= min_message_id:
            break
        messages.append(message)
    return messages


async def _messages_with_prefix(client, chat_id: int, prefix: str, *, min_message_id: int = 0, limit: int = 300):
    messages = []
    async for message in client.iter_messages(chat_id, limit=limit):
        if message.id <= min_message_id:
            break
        if prefix in (message.raw_text or ""):
            messages.append(message)
    return messages


async def _wait_for_messages_with_prefix(client, chat_id: int, prefix: str, *, min_message_id: int = 0, minimum: int = 1, timeout: float = BACKFILL_WAIT_TIMEOUT):
    deadline = time.time() + timeout
    messages = []
    while time.time() < deadline:
        messages = await _messages_with_prefix(client, chat_id, prefix, min_message_id=min_message_id)
        if len(messages) >= minimum:
            return messages
        await asyncio.sleep(1)
    raise AssertionError(f"Timed out waiting for {minimum} migrated messages with prefix {prefix!r}")


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
        matches = [log for log in channel.msglogs.get_recent_messages(slave_chat_id, limit=0) if (log.text or "").startswith(prefix)]
        if len(matches) >= expected_count:
            return matches
        await asyncio.sleep(1)
    raise AssertionError(f"Timed out waiting for {expected_count} logged messages for {prefix!r}; got {len(matches)}")


async def _link_chat(
    client,
    helper,
    bot_id: int,
    chat_uid: str,
    dest_chat_id: int,
    private_response,
    *,
    flag: str | None = None,
    start_token: str | None = None,
):
    if start_token is None:
        start_token = (await get_start_link(client, helper, bot_id, chat_uid, private_response)).token
    command = f"/start {start_token}"
    if flag is not None:
        command += f" {flag}"
    command_message = None

    async def trigger():
        nonlocal command_message
        command_message = await client.send_message(dest_chat_id, command)

    async def receive(timeout):
        assert command_message is not None
        return await _wait_for_text_in_chat(
            client,
            dest_chat_id,
            "is now linked.",
            min_message_id=command_message.id,
            timeout=timeout,
        )

    await private_response(trigger, receive)
    assert command_message is not None
    return command_message


def _create_relink_start_token(channel_with_auxiliary_bots, etm_chat, *, storage_key: tuple[TelegramChatID, TelegramMessageID]) -> str:
    channel_with_auxiliary_bots.callback_sessions.start(
        channel_with_auxiliary_bots.link_handler,
        storage_key,
        Flags.LINK_EXEC,
        ChatListStorage([etm_chat]),
    )
    return etm_utils.b64en(etm_utils.message_id_to_str(*storage_key))


def _extract_stream_indices(text: str, prefix: str) -> List[int]:
    pattern = re.compile(re.escape(prefix) + r"\s+(\d{3})")
    return [int(match.group(1)) for match in pattern.finditer(text)]


def _expected_stream_indices(expected_count: int) -> Set[int]:
    return set(range(expected_count))


def _migration_activity_completed(*, activity_observed: bool, expected: Set[int], db_indices: List[int], telegram_indices: List[int], target_entry_count: int) -> bool:
    expected_count = len(expected)
    return (
        activity_observed
        and target_entry_count == 0
        and set(db_indices) == expected
        and len(db_indices) == expected_count
        and set(telegram_indices) == expected
        and len(telegram_indices) == expected_count
    )


def _target_migration_entry_count(slave_chat_id: str, target_chat_id: int) -> int:
    return int(HistoryMigrationEntry.select().where((HistoryMigrationEntry.slave_chat_id == slave_chat_id) & (HistoryMigrationEntry.target_chat_id == str(target_chat_id))).count())


def _logs_with_prefix(channel_with_auxiliary_bots, chat, prefix: str):
    slave_chat_id = etm_utils.chat_id_to_str(chat=chat)
    return [log for log in channel_with_auxiliary_bots.msglogs.get_recent_messages(slave_chat_id, limit=0) if (log.text or "").startswith(prefix)]


async def _wait_for_stream_stable(channel_with_auxiliary_bots, client, *, tg_chat_id: int, chat, prefix: str, expected_count: int, min_message_id: int):
    expected = _expected_stream_indices(expected_count)
    deadline = time.time() + STREAM_SETTLE_TIMEOUT

    last_debug = ""
    while time.time() < deadline:
        db_logs = _logs_with_prefix(channel_with_auxiliary_bots, chat, prefix)
        db_indices = [idx for log in db_logs for idx in _extract_stream_indices(log.text or "", prefix)]

        group_messages = await _messages_with_prefix(
            client,
            tg_chat_id,
            prefix,
            min_message_id=min_message_id,
            limit=max(2000, expected_count + 400),
        )
        group_indices = [idx for message in group_messages for idx in _extract_stream_indices(message.raw_text or "", prefix)]

        last_debug = f"db={len(db_logs)} (idx={len(db_indices)}/{expected_count}), tg={len(group_messages)} (idx={len(group_indices)}/{expected_count})"

        if set(db_indices) == expected and len(db_indices) == expected_count and set(group_indices) == expected and len(group_indices) == expected_count:
            return db_logs, group_messages

        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(f"Timed out waiting for stream to settle: {last_debug}")


async def _wait_for_migrated_stream_terminal(channel_with_auxiliary_bots, client, chat, chat_id: int, prefix: str, expected_count: int, *, min_message_id: int, timeout: float = BACKFILL_WAIT_TIMEOUT):
    expected = _expected_stream_indices(expected_count)
    slave_chat_id = etm_utils.chat_id_to_str(chat=chat)
    deadline = time.time() + timeout
    replay_or_entry_observed = False

    last_debug = ""
    while time.time() < deadline:
        recent = await _messages_since_id(client, chat_id, min_message_id)
        telegram_indices = [idx for message in recent for idx in _extract_stream_indices(message.raw_text or "", prefix)]
        db_logs = _logs_with_prefix(channel_with_auxiliary_bots, chat, prefix)
        db_indices = [idx for log in db_logs for idx in _extract_stream_indices(log.text or "", prefix)]
        target_entry_count = _target_migration_entry_count(slave_chat_id, chat_id)
        replay_or_entry_observed = replay_or_entry_observed or bool(telegram_indices) or target_entry_count > 0
        last_debug = (
            f"replay_or_entry_observed={replay_or_entry_observed}, db_idx={len(db_indices)}/{expected_count}, "
            f"tg_idx={len(telegram_indices)}/{expected_count}, target_entries={target_entry_count}, "
            f"expected_migration_sends={expected_count}"
        )
        if _migration_activity_completed(
            activity_observed=replay_or_entry_observed,
            expected=expected,
            db_indices=db_indices,
            telegram_indices=telegram_indices,
            target_entry_count=target_entry_count,
        ):
            return recent
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(f"Timed out waiting for migrated stream terminal state: {last_debug}")


async def test_auxiliary_bots_stream_blackbox_and_relink(channel_with_auxiliary_bots, helper, client, bot_id, bot_group, bot_topic_group, slave_with_auxiliary_bots, private_response):
    if bot_topic_group is None:
        pytest.skip("TOPIC_GROUP is required for backfill history relink coverage.")

    client_user = await client.get_me()
    assert client_user is not None and client_user.id is not None, "Telethon client user ID is required to prepare relink callback sessions."
    private_chat_owner = TelegramChatID(client_user.id)

    source_group_id = bot_group
    chat = slave_with_auxiliary_bots.chat_with_alias
    slave_uid = etm_utils.chat_id_to_str(chat=chat)

    etm_chat = channel_with_auxiliary_bots.chat_manager.get_chat(chat.module_id, chat.uid)
    assert etm_chat is not None
    etm_chat.unlink()
    channel_with_auxiliary_bots.chat_associations.remove_topic_assoc(slave_uid=slave_uid)

    prefix = f"AUXSEND{uuid4().hex[:10]}"
    source_start_link = await get_start_link(client, helper, bot_id, chat.uid, private_response)
    source_storage_key = decode_start_link_token(source_start_link.token, expected_owner=private_chat_owner)
    assert source_storage_key[0] == private_chat_owner
    command_message = await _link_chat(client, helper, bot_id, chat.uid, source_group_id, private_response, start_token=source_start_link.token)

    # Private-panel delivery is covered by the backfill tests; this test isolates history replay.
    relink_true_token = _create_relink_start_token(
        channel_with_auxiliary_bots,
        etm_chat,
        storage_key=source_storage_key,
    )

    stream_thread, sent_texts, stream_errors = _start_mock_stream(slave_with_auxiliary_bots, chat, prefix)
    await asyncio.to_thread(stream_thread.join, STREAM_SETTLE_TIMEOUT)

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

    group_indices = [idx for msg in group_messages for idx in _extract_stream_indices(msg.raw_text or "", prefix)]
    assert set(group_indices) == _expected_stream_indices(STREAM_MESSAGE_COUNT)
    assert len(group_indices) == STREAM_MESSAGE_COUNT, "Expected exactly 120 stream messages in the Telegram group."

    db_indices = [idx for log in db_logs for idx in _extract_stream_indices(log.text or "", prefix)]
    assert set(db_indices) == _expected_stream_indices(STREAM_MESSAGE_COUNT)
    assert len(db_indices) == STREAM_MESSAGE_COUNT, "Expected exactly 120 logged stream messages in DB."

    target_group_id = bot_topic_group
    try:
        relink_true_message = await _link_chat(
            client,
            helper,
            bot_id,
            chat.uid,
            target_group_id,
            private_response,
            flag="true",
            start_token=relink_true_token,
        )
        await _wait_for_migrated_stream_terminal(
            channel_with_auxiliary_bots,
            client,
            chat,
            target_group_id,
            prefix,
            STREAM_MESSAGE_COUNT,
            min_message_id=relink_true_message.id,
        )
        assert _target_migration_entry_count(slave_uid, target_group_id) == 0

        recent_messages = await _messages_since_id(client, target_group_id, relink_true_message.id)
        assert not any("History messages are not migrated" in (message.raw_text or "") for message in recent_messages), (
            "Relink with true should migrate history instead of sending the history-link notice."
        )

        relink_false_token = _create_relink_start_token(
            channel_with_auxiliary_bots,
            etm_chat,
            storage_key=source_storage_key,
        )
        relink_false_message = await _link_chat(
            client,
            helper,
            bot_id,
            chat.uid,
            source_group_id,
            private_response,
            flag="false",
            start_token=relink_false_token,
        )
        await asyncio.sleep(10)
        recent_source_messages = await _messages_since_id(client, source_group_id, relink_false_message.id)
        assert not any(prefix in (message.raw_text or "") for message in recent_source_messages), "Relink with false should skip migrating historical messages."
        assert not any("History messages are not migrated" in (message.raw_text or "") for message in recent_source_messages), (
            "Relink with false should skip both history migration and the history-link notice."
        )
    finally:
        etm_chat.unlink()
