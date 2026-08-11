import re

import pytest

from efb_telegram_master import utils

from .helper.filters import in_chats, regex

pytestmark = pytest.mark.asyncio


def get_message_thread_id(message):
    message_thread_id = getattr(message, "message_thread_id", None)
    if message_thread_id is not None:
        return message_thread_id

    reply_to = getattr(message, "reply_to", None)
    return getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None) or getattr(message, "reply_to_msg_id", None)


@pytest.fixture(scope="module")
def poll_bot(channel_with_topic_group, poll_bot_factory):
    poll_bot_factory.start(channel_with_topic_group)
    yield channel_with_topic_group.bot_manager
    poll_bot_factory.stop(channel_with_topic_group)


@pytest.fixture(scope="function")
async def helper(helper_wrap, slave_with_topic_group):
    helper_wrap.clear_queue()
    assert helper_wrap.queue.empty()
    slave_with_topic_group.clear_messages()
    assert slave_with_topic_group.messages.empty()
    slave_with_topic_group.clear_statuses()
    assert slave_with_topic_group.statuses.empty()
    yield helper_wrap


async def test_slave_message_creates_topic_and_delivers(helper, slave_with_topic_group, bot_topic_group, channel_with_topic_group):
    chat = slave_with_topic_group.chat_with_alias
    sent = slave_with_topic_group.send_text_message(chat, chat.other)

    tg_message = await helper.wait_for_message(in_chats(bot_topic_group) & regex(re.escape(sent.text)))
    message_thread_id = get_message_thread_id(tg_message)

    assert tg_message.chat_id == bot_topic_group
    assert message_thread_id is not None
    assert tg_message.reply_to_msg_id in (None, message_thread_id)
    assert sent.text in tg_message.raw_text

    slave_uid = channel_with_topic_group.chat_associations.get_topic_slave(bot_topic_group, message_thread_id)
    assert slave_uid == utils.chat_id_to_str(chat=chat)


async def test_reply_inside_topic_routes_back_to_slave(helper, client, slave_with_topic_group, bot_topic_group):
    chat = slave_with_topic_group.chat_with_alias
    sent = slave_with_topic_group.send_text_message(chat, chat.other)
    tg_message = await helper.wait_for_message(in_chats(bot_topic_group) & regex(re.escape(sent.text)))

    await client.send_message(bot_topic_group, "topic reply integration", reply_to=tg_message.id)

    slave_message = slave_with_topic_group.messages.get(timeout=10)
    slave_with_topic_group.messages.task_done()

    assert slave_message.chat.uid == chat.uid
    assert slave_message.text == "topic reply integration"
