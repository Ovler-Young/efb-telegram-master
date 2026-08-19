import asyncio
from uuid import uuid4

import pytest

from efb_telegram_master import utils as etm_utils
from efb_telegram_master.utils import TelegramChatID

from .test_backfill_history_ingestion import STREAM_MESSAGE_COUNT, STREAM_SETTLE_TIMEOUT, expected_stream_indices, extract_stream_indices, messages_since_id, start_mock_stream, wait_for_stream_stable
from .test_backfill_history_lifecycle import target_migration_entry_count, wait_for_migrated_stream_terminal
from .test_backfill_history_setup import create_relink_start_token, link_chat
from .test_backfill_history_setup import helper as backfill_helper
from .test_backfill_history_setup import poll_bot as poll_bot
from .utils import decode_start_link_token, get_start_link

pytestmark = pytest.mark.asyncio
helper = backfill_helper


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
    command_message = await link_chat(client, helper, bot_id, chat.uid, source_group_id, private_response, start_token=source_start_link.token)

    stream_thread, sent_texts, stream_errors = start_mock_stream(slave_with_auxiliary_bots, chat, prefix)
    await asyncio.to_thread(stream_thread.join, STREAM_SETTLE_TIMEOUT)

    assert not stream_thread.is_alive(), "Mock stream did not finish in time."
    assert not stream_errors, f"Mock stream failed: {stream_errors!r}"
    assert len(sent_texts) == STREAM_MESSAGE_COUNT

    db_logs, group_messages = await wait_for_stream_stable(
        channel_with_auxiliary_bots,
        client,
        tg_chat_id=source_group_id,
        chat=chat,
        prefix=prefix,
        expected_count=STREAM_MESSAGE_COUNT,
        min_message_id=command_message.id,
    )

    group_indices = [idx for msg in group_messages for idx in extract_stream_indices(msg.raw_text or "", prefix)]
    assert set(group_indices) == expected_stream_indices(STREAM_MESSAGE_COUNT)
    assert len(group_indices) == STREAM_MESSAGE_COUNT, "Expected exactly 120 stream messages in the Telegram group."

    db_indices = [idx for log in db_logs for idx in extract_stream_indices(log.text or "", prefix)]
    assert set(db_indices) == expected_stream_indices(STREAM_MESSAGE_COUNT)
    assert len(db_indices) == STREAM_MESSAGE_COUNT, "Expected exactly 120 logged stream messages in DB."

    target_group_id = bot_topic_group
    try:
        relink_true_token, relink_true_storage_key = create_relink_start_token(
            channel_with_auxiliary_bots,
            etm_chat,
            private_chat_owner=private_chat_owner,
        )
        relink_true_message = await link_chat(
            client,
            helper,
            bot_id,
            chat.uid,
            target_group_id,
            private_response,
            flag="true",
            start_token=relink_true_token,
            channel=channel_with_auxiliary_bots,
            storage_key=relink_true_storage_key,
            slave_uid=slave_uid,
        )
        await wait_for_migrated_stream_terminal(
            channel_with_auxiliary_bots,
            client,
            chat,
            target_group_id,
            prefix,
            STREAM_MESSAGE_COUNT,
            min_message_id=relink_true_message.id,
        )
        assert target_migration_entry_count(slave_uid, target_group_id) == 0

        recent_messages = await messages_since_id(client, target_group_id, relink_true_message.id)
        assert not any("History messages are not migrated" in (message.raw_text or "") for message in recent_messages), (
            "Relink with true should migrate history instead of sending the history-link notice."
        )

        relink_false_token, relink_false_storage_key = create_relink_start_token(
            channel_with_auxiliary_bots,
            etm_chat,
            private_chat_owner=private_chat_owner,
        )
        assert relink_false_storage_key != relink_true_storage_key, "Each relink must use its own bot-owned callback message."
        relink_false_message = await link_chat(
            client,
            helper,
            bot_id,
            chat.uid,
            source_group_id,
            private_response,
            flag="false",
            start_token=relink_false_token,
            channel=channel_with_auxiliary_bots,
            storage_key=relink_false_storage_key,
            slave_uid=slave_uid,
        )
        recent_source_messages = await messages_since_id(client, source_group_id, relink_false_message.id)
        assert not any(prefix in (message.raw_text or "") for message in recent_source_messages), "Relink with false should skip migrating historical messages."
        assert not any("History messages are not migrated" in (message.raw_text or "") for message in recent_source_messages), (
            "Relink with false should skip both history migration and the history-link notice."
        )
    finally:
        etm_chat.unlink()
