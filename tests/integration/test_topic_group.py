from queue import Empty

import pytest

from .helper.filters import in_chats, text

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def poll_bot(channel_with_topic_group, poll_bot_factory):
    polling_thread, polling_errors = poll_bot_factory(channel_with_topic_group)
    yield channel_with_topic_group.bot_manager
    channel_with_topic_group.bot_manager.graceful_stop()
    polling_thread.join(timeout=10)
    if polling_thread.is_alive():
        raise RuntimeError("Telegram bot polling thread did not stop in time.")
    if polling_errors:
        raise polling_errors[0]


@pytest.fixture(scope="function")
async def helper(helper_wrap, slave_with_topic_group):
    helper_wrap.clear_queue()
    assert helper_wrap.queue.empty()
    slave_with_topic_group.clear_messages()
    assert slave_with_topic_group.messages.empty()
    slave_with_topic_group.clear_statuses()
    assert slave_with_topic_group.statuses.empty()
    yield helper_wrap


async def test_slave_message_creates_topic_and_delivers(helper, slave_with_topic_group, bot_topic_group,
                                                        channel_with_topic_group):
    chat = slave_with_topic_group.chat_with_alias
    sent = slave_with_topic_group.send_text_message(chat, chat.other)

    tg_message = await helper.wait_for_message(in_chats(bot_topic_group) & text)

    assert tg_message.chat_id == bot_topic_group
    assert tg_message.reply_to_msg_id is None
    assert tg_message.message_thread_id is not None
    assert sent.text in tg_message.raw_text

    slave_uid = channel_with_topic_group.db.get_topic_slave(bot_topic_group, tg_message.message_thread_id)
    assert slave_uid == chat.channel_id + "." + chat.uid


async def test_reply_inside_topic_routes_back_to_slave(helper, client, slave_with_topic_group, bot_topic_group):
    chat = slave_with_topic_group.chat_with_alias
    slave_with_topic_group.send_text_message(chat, chat.other)
    tg_message = await helper.wait_for_message(in_chats(bot_topic_group) & text)

    await client.send_message(bot_topic_group, "topic reply integration", reply_to=tg_message.id)

    slave_message = slave_with_topic_group.messages.get(timeout=10)
    slave_with_topic_group.messages.task_done()

    assert slave_message.chat.uid == chat.uid
    assert slave_message.text == "topic reply integration"
