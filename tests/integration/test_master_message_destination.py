"""
Test message destinations
Only testing with text messages, as everything else shall follow suit.

- Singly linked chat is tested in test_master_messages.py
- Chat head is tested in test_chat_head.py
"""

import asyncio
import re
import time
from contextlib import suppress
from typing import List
from unittest.mock import patch

from pytest import mark, raises
from telethon.tl.custom import Message, MessageButton

from .helper.filters import in_chats, has_button, edited, regex, text

retry_on_integration_timeout = mark.flaky(
    max_runs=2,
    min_passes=1,
    rerun_filter=lambda err, *_: bool(err and err[0] and issubclass(err[0], TimeoutError)),
)

pytestmark = mark.asyncio


async def test_master_master_quick_reply_no_cache(helper, client, bot_id, slave, channel,
                                                  private_response):
    assert channel.chat_dest_cache.enabled
    channel.chat_dest_cache.weak.clear()
    channel.chat_dest_cache.strong.clear()
    slave.clear_messages()

    await private_response(
        lambda: client.send_message(
            bot_id,
            "test_master_master_quick_reply_no_cache this shall not be sent due to empty cache",
        ),
        lambda timeout: helper.wait_for_message(in_chats(bot_id), timeout),
    )
    assert slave.messages.empty()


async def test_master_master_quick_reply(helper, client, bot_id, slave, channel,
                                         private_response):
    """Tests if the quick reply cache exists, and changes afterwards by
    incoming message from slave channel.
    """
    assert channel.chat_dest_cache.enabled
    assert channel.flag("send_to_last_chat") == "warn"

    chat = slave.chat_with_alias
    content = "test_master_master_quick_reply set cache with chat head"
    message = await private_response(
        lambda: client.send_message(bot_id, f"/chat {chat.uid}"),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    await message.click(0)
    message = await helper.wait_for_message(in_chats(bot_id) & edited(message.id) & ~has_button)
    await message.reply(content)
    message = slave.messages.get(timeout=5)
    slave.messages.task_done()
    assert message.text == content
    assert message.chat == chat

    content = "test_master_master_quick_reply send new message with quick reply"
    text = await private_response(
        lambda: client.send_message(bot_id, content),
        lambda timeout: helper.wait_for_message_text(
            in_chats(bot_id) & regex(re.escape(chat.display_name)), timeout
        ),
    )
    assert chat.display_name in text, f"{text!r} is not a warning message for {chat}"
    message = slave.messages.get(timeout=5)
    slave.messages.task_done()

    assert message.text == content
    assert message.chat == chat

    content = "test_master_master_quick_reply send another new message " \
              "with quick reply, should give no warning"
    await client.send_message(bot_id, content)
    message = slave.messages.get(timeout=5)
    slave.messages.task_done()
    assert message.text == content
    assert message.chat == chat
    with raises(asyncio.TimeoutError):
        await helper.wait_for_message_text(in_chats(bot_id) & regex(chat.display_name), timeout=3)

    chat_alt = slave.chat_without_alias
    message = None

    async def send_incoming_message():
        nonlocal message
        message = slave.send_text_message(chat_alt, author=chat_alt.other)

    text = await private_response(
        send_incoming_message,
        lambda timeout: helper.wait_for_message_text(
            in_chats(bot_id) & regex(re.escape(message.text if message else "")), timeout
        ),
    )
    assert message is not None
    assert message.text in text  # there might be message header in ``text``

    content = "test_master_master_quick_reply this shall not be sent due to cleared cache"
    message = await private_response(
        lambda: client.send_message(bot_id, content),
        lambda timeout: helper.wait_for_message(in_chats(bot_id), timeout),
    )  # Error message
    assert slave.messages.empty()
    await cancel_destination_suggestion(helper, message)


async def test_master_master_quick_reply_cache_expiry(helper, client, bot_id, slave, channel,
                                                       private_response):
    assert channel.chat_dest_cache.enabled
    slave.clear_messages()

    chat = slave.chat_with_alias
    content = "test_master_master_quick_reply_cache_expiry set cache with chat head"
    message = await private_response(
        lambda: client.send_message(bot_id, f"/chat {chat.uid}"),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    await message.click(0)
    message = await helper.wait_for_message(in_chats(bot_id) & edited(message.id) & ~has_button)
    await message.reply(content)
    slave.messages.get(timeout=5)
    slave.messages.task_done()

    # Mutate only the cache entry. Patching ``time.time`` changes the shared
    # module object Telethon also uses for its transport deadlines.
    human_chat_cache_key = str((await client.get_me()).id)
    assert human_chat_cache_key in channel.chat_dest_cache.weak
    channel.chat_dest_cache.weak[human_chat_cache_key].expiry = time.time() - 1
    content = "test_master_master_quick_reply_cache_expiry this shall not be sent due to expired cache"
    message = await private_response(
        lambda: client.send_message(bot_id, content),
        lambda timeout: helper.wait_for_message(
            in_chats(bot_id) & text & regex("Error: No recipient specified"), timeout
        ),
    )
    assert slave.messages.empty()
    await cancel_destination_suggestion(helper, message)


@retry_on_integration_timeout
async def test_master_master_destination_suggestion(helper, client, bot_id, slave, channel,
                                                    private_response):
    with patch.dict(channel.flag.config, send_to_last_chat="disabled"), \
         patch.multiple(channel.chat_dest_cache, enabled=False):
        assert not channel.chat_dest_cache.enabled
        slave.clear_messages()
        chat = slave.chat_with_alias
        previous_message = slave.send_text_message(chat, author=chat.other)
        await helper.wait_for_message_text(in_chats(bot_id) & regex(previous_message.text))

        content = "test_master_master_destination_suggestion this shall be replied with a list of candidates"
        sent_message: Message | None = None

        async def send_message() -> None:
            nonlocal sent_message
            sent_message = await client.send_message(bot_id, content)

        message: Message = await private_response(
            send_message,
            lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
        )
        assert sent_message is not None
        buttons: List[List[MessageButton]] = message.buttons
        chat_buttons = [
            button
            for row in buttons
            for button in row
            if chat.display_name in button.text
        ]
        assert chat_buttons
        # await buttons[-1][0].click()  # Cancel the error message.

        await chat_buttons[0].click()  # deliver the message
        slave.clear_messages()

        content = "test_master_master_destination_suggestion edited message shall be delivered without a prompt"
        await sent_message.edit(text=content)
        slave_message = slave.messages.get(timeout=15)
        assert slave_message.text == content



async def cancel_destination_suggestion(helper, message: Message):
    """Cancel chat destination suggestions if available."""
    with suppress(asyncio.TimeoutError):
        while not message.button_count:
            message = await helper.wait_for_message(in_chats(message.chat_id))
    if message.button_count:
        await message.buttons[-1][-1].click()
