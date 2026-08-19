import re
from itertools import chain
from typing import List

from pytest import mark
from telethon.tl.custom import Message, MessageButton
from telethon.tl.types import MessageEntityCode

from .helper.filter_chats import in_chats
from .helper.filter_content import has_button, regex
from .helper.filter_messages import edited
from .link_chat_flows import retry_on_message_id_invalid_error, simulate_link_chat
from .utils import assert_is_linked, link_chats, unlink_all_chats

pytestmark = [mark.asyncio, retry_on_message_id_invalid_error]


async def test_link_chat_pagination(helper, client, bot_id, slave, private_response):
    message: Message = await private_response(
        lambda: client.send_message(bot_id, "/link"),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    assert slave.channel_emoji in message.text
    assert slave.channel_name in message.text

    buttons: List[List[MessageButton]] = message.buttons
    assert message.button_count > 2, "more than two buttons are shown in the chat list"
    assert ">" in buttons[-1][-1].text, "next-page button is available"

    await buttons[-1][-1].click()
    message = await helper.wait_for_message(in_chats(bot_id) & edited(message.id) & has_button)
    buttons = message.buttons
    assert "<" in buttons[-1][0].text, "previous-page button is available after navigating"

    await buttons[-1][0].click()
    message = await helper.wait_for_message(in_chats(bot_id) & edited(message.id) & has_button)
    await message.click(text="Cancel")


async def test_link_chat_private(helper, client, bot_id, bot_group, slave, channel, private_response):
    chat_0 = slave.chat_with_alias

    message: Message = await private_response(
        lambda: client.send_message(bot_id, f"/link {chat_0.uid}"),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    choose_chat: MessageButton = message.buttons[0][0]
    assert chat_0.display_name in choose_chat.text

    await choose_chat.click()
    message = await helper.wait_for_message(in_chats(bot_id) & edited(message.id) & has_button)
    url = ""
    manual_button: MessageButton = message.buttons[0][-1]
    for i in chain.from_iterable(message.buttons):
        if i.url:
            url = i.url
            break

    assert url
    assert "manual" in manual_button.text.lower()
    await manual_button.click()
    message = await helper.wait_for_message(in_chats(bot_id) & edited(message.id) & has_button)
    manual_session_message_id = message.id

    command: str = next(txt for _, txt in message.get_entities_text(MessageEntityCode) if txt.startswith("/start "))
    token = command[len("/start ") :]
    match = re.search(r"\?startgroup=(.+)", url)
    assert match is not None
    assert token == match.group(1), "URL token matches manual token"

    try:
        await private_response(
            lambda: client.send_message(bot_group, command),
            lambda timeout: helper.wait_for_message(in_chats(bot_id) & edited(manual_session_message_id) & ~has_button, timeout),
        )
        assert_is_linked(channel, (chat_0,), bot_group)
    finally:
        await unlink_all_chats(channel, client, helper, bot_group)

    message = await private_response(
        lambda: client.send_message(bot_id, "/link"),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    await message.click(text="Cancel")
    await helper.wait_for_event(in_chats(bot_id) & edited(message.id) & ~has_button)


async def test_unlink_unavailable_chat(helper, client, bot_group, slave, channel):
    with link_chats(channel, (slave.chat_with_alias, slave.unknown_chat), bot_group):
        await client.send_message(bot_group, "/link")
        message = await helper.wait_for_message(in_chats(bot_group) & has_button)

        assert message.button_count == 3, f"{message.buttons} should be one known, one unknown, one cancel"
        unavailable_chat_buttons = [button for row in message.buttons for button in row if str(slave.unknown_chat.uid) in button.text]
        assert len(unavailable_chat_buttons) == 1, message.buttons
        await unavailable_chat_buttons[0].click()

        message = await helper.wait_for_message(in_chats(bot_group) & has_button)
        await message.click(text="Restore")
        await helper.wait_for_message(in_chats(bot_group))

        assert_is_linked(channel, (slave.chat_with_alias,), bot_group)


async def test_link_chat_multi_link_flag_off(helper, client, bot_id, bot_group, slave, channel, private_response):
    chat_0 = slave.chat_with_alias
    chat_1 = slave.chat_without_alias

    backup = channel.flag.config["multiple_slave_chats"]
    channel.flag.config["multiple_slave_chats"] = False
    try:
        with link_chats(channel, (chat_0,), bot_group):
            assert_is_linked(channel, (chat_0,), bot_group)
            await simulate_link_chat(client, chat_1, bot_id, bot_group, private_response=private_response)
            assert_is_linked(channel, (chat_1,), bot_group)
    finally:
        channel.flag.config["multiple_slave_chats"] = backup


async def test_link_chat_group_unlinked(helper, client, bot_id, bot_group, channel, private_response):
    with link_chats(channel, tuple(), bot_group):
        message: Message = await private_response(
            lambda: client.send_message(bot_group, "/link"),
            lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
        )
        await message.click(text="Cancel")


async def test_link_chat_group_linked_unlink(helper, client, bot_id, bot_group, slave, channel):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_group):
        await client.send_message(bot_group, "/link")
        message: Message = await helper.wait_for_message(in_chats(bot_group) & has_button)
        assert 2 == len(message.buttons), "link message in group should have 2 rows"
        assert chat.display_name in message.buttons[0][0].text
        await message.click(0)

        message: Message = await helper.wait_for_message(in_chats(bot_group) & edited(message.id) & has_button)
        await message.click(text="Restore")

        await helper.wait_for_message(in_chats(bot_group) & edited(message.id) & ~has_button)

        assert_is_linked(channel, tuple(), bot_group)


async def test_link_chat_channel(helper, client, bot_id, bot_group, bot_channel, slave, channel, private_response):
    chat = slave.chat_with_alias
    with link_chats(channel, tuple(), bot_channel):
        await simulate_link_chat(client, chat, bot_id, bot_id, dest_channel=bot_channel, private_response=private_response)
        assert_is_linked(channel, (chat,), bot_channel)


async def test_link_chat_channel_linked_cancel(helper, client, bot_id, bot_channel, slave, channel, private_response):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_channel):
        message: Message = await client.send_message(bot_channel, "/link")
        message = await private_response(
            lambda: message.forward_to(bot_id),
            lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
        )
        assert 2 == len(message.buttons), "link message from channel should have 2 rows"
        await message.click(text="Cancel")


async def test_link_chat_target_incoming_message(helper, client, bot_id, slave, channel, private_response):
    chat = slave.chat_with_alias
    efb_msg = slave.send_text_message(chat, chat.other)

    incoming_msg = await helper.wait_for_message(in_chats(bot_id) & regex(re.escape(efb_msg.text)))
    message = await private_response(
        lambda: client.send_message(bot_id, "/link", reply_to=incoming_msg),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    assert chat.display_name in message.raw_text
    await message.click(text="Cancel")
