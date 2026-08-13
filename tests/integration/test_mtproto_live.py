"""Credentialed MsgLog ingestion coverage for the CI Telegram forum group."""

import asyncio
import re
import time
from contextlib import suppress
from dataclasses import replace
from uuid import uuid4

import pytest

from efb_telegram_master import utils
from efb_telegram_master.db import MsgLog
from efb_telegram_master.msglog_ingestion import MsgLogIngestionService

from ..bot import get_user_session
from .helper.filters import in_chats, regex

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="session")
def user_session_info():
    try:
        return get_user_session()
    except ValueError:
        pytest.skip("Telegram integration credentials are not configured")


@pytest.fixture(scope="module")
def poll_bot(channel_with_topic_group_and_mtproto, poll_bot_factory):
    poll_bot_factory.start(channel_with_topic_group_and_mtproto)
    yield channel_with_topic_group_and_mtproto.bot_manager
    poll_bot_factory.stop(channel_with_topic_group_and_mtproto)


@pytest.fixture(scope="function")
async def helper(helper_wrap, slave_with_topic_group_and_mtproto):
    helper_wrap.clear_queue()
    slave_with_topic_group_and_mtproto.clear_messages()
    slave_with_topic_group_and_mtproto.clear_statuses()
    try:
        yield helper_wrap
    finally:
        helper_wrap.clear_queue()


def _topic_id(message):
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id is not None:
        return thread_id
    reply_to = getattr(message, "reply_to", None)
    return getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None) or getattr(message, "reply_to_msg_id", None)


async def _wait_for_msg_log(channel, master_msg_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = channel.db.get_msg_log(master_msg_id=master_msg_id)
        if row is not None:
            return row
        await asyncio.sleep(0.1)
    raise TimeoutError(f"MsgLog row {master_msg_id} was not persisted")


def _delete_msg_logs_by_master_ids(channel, master_msg_ids):
    for master_msg_id in master_msg_ids:
        channel.db.delete_msg_log(master_msg_id=master_msg_id)


async def _wait_for_ingestion_worker_exit(channel, source_chat_id, timeout=30):
    manager = channel.chat_binding
    with manager._msglog_ingestion_lock:
        worker = manager._msglog_ingestion_threads.get(source_chat_id)
    if worker is None:
        return
    await asyncio.to_thread(worker.join, timeout)
    if worker.is_alive():
        raise TimeoutError("MsgLog ingestion worker did not stop")


async def test_sync_msglog_ingests_unlogged_topic_messages_live(
    helper,
    client,
    bot_topic_group,
    channel_with_topic_group_and_mtproto,
    slave_with_topic_group_and_mtproto,
    poll_bot,
    monkeypatch,
):
    """Rebuild one removed live MsgLog row from Telegram history."""
    channel = channel_with_topic_group_and_mtproto
    slave = slave_with_topic_group_and_mtproto
    marker = f"mtproto-msglog-{uuid4().hex}"
    created_message_ids = []
    logged_message_ids = []
    topic_id = None

    try:
        delivered = slave.send_text_message(slave.chat_with_alias, slave.chat_with_alias.other)
        topic_message = await helper.wait_for_message(in_chats(bot_topic_group) & regex(re.escape(delivered.text)))
        topic_id = _topic_id(topic_message)
        assert topic_id is not None
        anchor_log_id = utils.message_id_to_str(bot_topic_group, topic_message.id)
        logged_message_ids.append(anchor_log_id)
        anchor_log = await _wait_for_msg_log(channel, anchor_log_id)
        assert anchor_log.provenance == "live"

        recovered = await client.send_message(
            bot_topic_group,
            marker,
            reply_to=topic_message.id,
        )
        created_message_ids.append(recovered.id)
        source_ids = [recovered.id]
        source_log_ids = [utils.message_id_to_str(bot_topic_group, message_id) for message_id in source_ids]
        logged_message_ids.extend(source_log_ids)

        routed_message = await asyncio.to_thread(slave.messages.get, True, 10)
        slave.messages.task_done()
        assert routed_message.text == marker

        live_rows = [await _wait_for_msg_log(channel, master_msg_id) for master_msg_id in source_log_ids]
        expected_slave_uid = channel.db.get_topic_slave(bot_topic_group, topic_id)
        assert expected_slave_uid is not None
        assert [(row.master_msg_id, row.text, row.slave_origin_uid, row.provenance) for row in live_rows] == [
            (source_log_ids[0], marker, expected_slave_uid, "live"),
        ]

        _delete_msg_logs_by_master_ids(channel, source_log_ids)
        assert all(channel.db.get_msg_log(master_msg_id=master_msg_id) is None for master_msg_id in source_log_ids)

        channel.mtproto.config = replace(channel.mtproto.config, scan_ceiling=max(source_ids))
        monkeypatch.setattr(MsgLogIngestionService, "EXISTING_STREAK_LIMIT", 1)

        command = await client.send_message(
            bot_topic_group,
            "/sync_msglog",
            reply_to=topic_message.id,
        )
        created_message_ids.append(command.id)
        acknowledgement = await helper.wait_for_message(
            in_chats(bot_topic_group) & regex(r"MsgLog sync started for this group\."),
        )
        assert _topic_id(acknowledgement) == topic_id

        await _wait_for_ingestion_worker_exit(channel, bot_topic_group)
        rows = list(MsgLog.select().where(MsgLog.master_msg_id.in_(source_log_ids)))
        assert [(row.master_msg_id, row.text, row.provenance, row.slave_origin_uid) for row in rows] == [
            (source_log_ids[0], marker, "mtproto_ingested", expected_slave_uid),
        ]
    finally:
        await _wait_for_ingestion_worker_exit(channel, bot_topic_group)
        for message_id in logged_message_ids:
            channel.db.delete_msg_log(master_msg_id=message_id)
        if topic_id is not None:
            channel.db.remove_topic_assoc(bot_topic_group, topic_id)
            with suppress(Exception):
                channel.bot_manager._bot.delete_forum_topic(
                    chat_id=bot_topic_group,
                    message_thread_id=topic_id,
                )
        if created_message_ids:
            with suppress(Exception):
                await client.delete_messages(bot_topic_group, created_message_ids)
