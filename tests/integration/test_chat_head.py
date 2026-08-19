import re

from pytest import mark
from telethon.tl.custom import Message

from .helper.filter_chats import in_chats
from .helper.filter_content import has_button, regex
from .helper.filter_messages import edited
from .utils import link_chats

pytestmark = mark.asyncio


async def test_chat_head_private_cancel(helper, client, bot_id, private_response):
    message: Message = await private_response(
        lambda: client.send_message(bot_id, "/chat"),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    await message.click(text="Cancel")
    await helper.wait_for_event(in_chats(bot_id) & edited(message.id) & ~has_button)


async def test_chat_head_private(helper, client, bot_id, slave, private_response):
    message: Message = await private_response(
        lambda: client.send_message(bot_id, "/chat"),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    content = message.text

    assert slave.channel_emoji in content
    assert slave.channel_name in content

    buttons = message.buttons

    assert message.button_count > 2, "more than 2 buttons found on the chats list."
    assert ">" in buttons[-1][-1].text, "Next page button exists"
    await message.mark_read()
    await buttons[-1][-1].click()
    message = await helper.wait_for_message(in_chats(bot_id) & edited(message.id) & has_button)
    buttons = message.buttons
    assert "<" in buttons[-1][0].text, "Previous page button exists"
    await message.mark_read()
    await buttons[-1][0].click()

    message = await helper.wait_for_message(in_chats(bot_id) & edited(message.id) & has_button)
    buttons = message.buttons
    await message.mark_read()
    await buttons[0][0].click()

    content = "test_chat_head_private this should be sent to slave channel"
    await helper.wait_for_message(in_chats(bot_id) & edited(message.id) & ~has_button)
    tg_msg = await client.send_message(bot_id, content, reply_to=message)

    efb_msg = slave.messages.get(timeout=5)  # raises queue.Empty upon timeout
    slave.messages.task_done()

    assert efb_msg.text == content
    assert efb_msg.target is None

    content = "test_chat_head_private this edited msg should not carry a target"
    await tg_msg.edit(text=content)

    efb_msg = slave.messages.get(timeout=5)  # raises queue.Empty upon timeout
    slave.messages.task_done()

    assert efb_msg.text == content
    assert efb_msg.target is None


async def test_chat_head_singly_linked(helper, client, bot_group, slave, channel):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_group):
        await client.send_message(bot_group, "/chat")
        content: str = await helper.wait_for_message_text(in_chats(bot_group) & regex(chat.name))

        assert chat.name in content
        assert chat.alias in content
        assert chat.channel_emoji in content
        assert chat.module_name in content


async def test_chat_head_singly_linked_unknown_chat(helper, client, bot_group, slave, channel):
    chat = slave.unknown_chat
    with link_chats(channel, (chat,), bot_group):
        await client.send_message(bot_group, "/chat")
        content: str = await helper.wait_for_message_text(in_chats(bot_group) & regex(chat.uid))

        assert chat.uid in content
        assert chat.module_name in content
        assert chat.module_id in content


async def test_chat_head_singly_linked_unknown_channel(helper, client, bot_group, slave, channel):
    chat = slave.unknown_channel
    with link_chats(channel, (chat,), bot_group):
        await client.send_message(bot_group, "/chat")
        content: str = await helper.wait_for_message_text(in_chats(bot_group) & regex(chat.uid))

        assert chat.uid in content
        assert chat.module_id in content


async def test_chat_head_multi_linked(helper, client, bot_group, slave, channel):
    chats = slave.get_chats()[:5]
    chat = chats[0]
    with link_chats(channel, chats, bot_group):
        await client.send_message(bot_group, "/chat")
        message: Message = await helper.wait_for_message(in_chats(bot_group) & has_button)

        assert len(chats) + 1 == len(message.buttons), f"buttons should have {len(chats)} + 1 rows"

        pattern = r"(^|\W)" + re.escape(chat.name) + r"(\W|$)"
        await message.click(text=re.compile(pattern).search)

        content = "test_chat_head_multi_linked this should be sent to slave channel"
        message: Message = await helper.wait_for_message(in_chats(bot_group) & edited(message.id) & ~has_button)
        await client.send_message(bot_group, content, reply_to=message)

        efb_msg = slave.messages.get(timeout=5)  # raises queue.Empty upon timeout
        slave.messages.task_done()

        assert efb_msg.chat == chat
        assert efb_msg.text == content
