import re
import os
from queue import Empty

import pytest

from efb_telegram_master import utils
from .helper.filters import in_chats, regex

pytestmark = pytest.mark.asyncio


def get_message_thread_id(message):
    message_thread_id = getattr(message, "message_thread_id", None)
    if message_thread_id is not None:
        return message_thread_id

    reply_to = getattr(message, "reply_to", None)
    return (
        getattr(reply_to, "reply_to_top_id", None) or
        getattr(reply_to, "reply_to_msg_id", None) or
        getattr(message, "reply_to_msg_id", None)
    )


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


async def test_slave_message_creates_topic_and_delivers(helper, slave_with_topic_group, bot_topic_group,
                                                        channel_with_topic_group):
    chat = slave_with_topic_group.chat_with_alias
    sent = slave_with_topic_group.send_text_message(chat, chat.other)

    tg_message = await helper.wait_for_message(in_chats(bot_topic_group) & regex(re.escape(sent.text)))
    message_thread_id = get_message_thread_id(tg_message)

    assert tg_message.chat_id == bot_topic_group
    assert message_thread_id is not None
    assert tg_message.reply_to_msg_id in (None, message_thread_id)
    assert sent.text in tg_message.raw_text

    slave_uid = channel_with_topic_group.db.get_topic_slave(bot_topic_group, message_thread_id)
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


def test_bot_api_accepts_generated_custom_emoji_in_message_text(channel_with_topic_group, slave_with_topic_group,
                                                                bot_topic_group):
    if os.getenv("TEST_PREMIUM_TOPIC_CUSTOM_EMOJI") != "1":
        pytest.skip("Set TEST_PREMIUM_TOPIC_CUSTOM_EMOJI=1 to run this live Bot API capability check.")

    chat = slave_with_topic_group.chat_with_alias
    slave_uid = utils.chat_id_to_str(chat=chat)
    manager = channel_with_topic_group.chat_binding
    old_topic_icons = channel_with_topic_group.config.get("topic_icons")
    base_name = os.getenv("TEST_TOPIC_CUSTOM_EMOJI_SET", "etm_topic_icon_live")
    try:
        channel_with_topic_group.config["topic_icons"] = {
            "sync_avatar_to_custom_emoji": True,
            "sticker_set_name": base_name,
        }
        topic = channel_with_topic_group.bot_manager.create_forum_topic(
            chat_id=bot_topic_group,
            name="ETM custom emoji smoke",
        )
        picture = slave_with_topic_group.get_chat_picture(chat)
        custom_emoji_id = manager._get_or_create_topic_icon_custom_emoji(slave_uid, picture)

        assert custom_emoji_id
        assert channel_with_topic_group.bot_manager.send_message(
            chat_id=bot_topic_group,
            text=f'<tg-emoji emoji-id="{custom_emoji_id}"></tg-emoji> ETM custom emoji smoke',
            message_thread_id=topic.message_thread_id,
            parse_mode="HTML",
        )
    finally:
        if "topic" in locals():
            channel_with_topic_group.bot_manager.delete_forum_topic(
                chat_id=bot_topic_group,
                message_thread_id=topic.message_thread_id,
            )
        if old_topic_icons is None:
            channel_with_topic_group.config.pop("topic_icons", None)
        else:
            channel_with_topic_group.config["topic_icons"] = old_topic_icons
