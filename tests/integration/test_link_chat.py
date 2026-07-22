import asyncio
import re
from itertools import chain
from typing import Optional
from uuid import uuid4

from pytest import mark
from telethon.errors import MessageIdInvalidError
from telethon.tl.custom import Message, MessageButton
from telethon.tl.functions.channels import DeleteChannelRequest
from telethon.tl.functions.messages import CreateChatRequest, MigrateChatRequest, \
    DeleteChatUserRequest
from telethon.tl.types import MessageEntityCode, Updates, Chat as TelethonChat
from telethon.utils import get_peer_id

from ehforwarderbot import Chat
from .helper.filters import in_chats, has_button, edited, regex
from .utils import link_chats, assert_is_linked, unlink_all_chats

retry_on_message_id_invalid_error = mark.flaky(
    max_runs=2, min_passes=1,  # default value
    rerun_filter=lambda err, *_: issubclass(err[0], MessageIdInvalidError))
"""Retry on ``MessageIdInvalidError`` due to flaky behavior of MTProto API"""

pytestmark = [mark.asyncio, retry_on_message_id_invalid_error]


async def test_link_chat_private(helper, client, bot_id, bot_group, slave, channel,
                                 private_response):
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

    command: str = next(
        txt
        for _, txt in message.get_entities_text(MessageEntityCode)
        if txt.startswith("/start ")
    )
    token = command[len("/start "):]
    match = re.search(r"\?startgroup=(.+)", url)
    assert match is not None
    assert token == match.group(1), "URL token matches manual token"

    await client.send_message(bot_group, command)
    completion = await helper.wait_for_message(
        in_chats(bot_id) & edited(manual_session_message_id) & ~has_button
    )
    assert_is_linked(channel, (chat_0,), bot_group)
    unlink_all_chats(channel, bot_group)

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
        unavailable_chat_buttons = [
            button
            for row in message.buttons
            for button in row
            if str(slave.unknown_chat.uid) in button.text
        ]
        assert len(unavailable_chat_buttons) == 1, message.buttons
        await unavailable_chat_buttons[0].click()

        message = await helper.wait_for_message(in_chats(bot_group) & has_button)
        await message.click(text="Restore")
        await helper.wait_for_message(in_chats(bot_group))

        assert_is_linked(channel, (slave.chat_with_alias,), bot_group)


async def test_link_chat_multi_link_flag_off(helper, client, bot_id, bot_group, slave, channel,
                                             private_response):
    chat_0 = slave.chat_with_alias
    chat_1 = slave.chat_without_alias

    backup = channel.flag.config['multiple_slave_chats']
    channel.flag.config['multiple_slave_chats'] = False

    with link_chats(channel, (chat_0, ), bot_group):
        assert_is_linked(channel, (chat_0,), bot_group)
        await simulate_link_chat(client, helper, chat_1, bot_id, bot_group, private_response=private_response)
        assert_is_linked(channel, (chat_1,), bot_group)

    channel.flag.config['multiple_slave_chats'] = backup


async def test_link_chat_group_unlinked(helper, client, bot_id, bot_group, channel,
                                        private_response):
    with link_chats(channel, tuple(), bot_group):
        message: Message = await private_response(
            lambda: client.send_message(bot_group, "/link"),
            lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
        )
        await message.click(text="Cancel")


async def test_link_chat_group_linked_unlink(helper, client, bot_id, bot_group, slave, channel):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_group):
        await client.send_message(bot_group, f"/link")
        message: Message = await helper.wait_for_message(in_chats(bot_group) & has_button)
        assert 2 == len(message.buttons), "link message in group should have 2 rows"
        assert chat.display_name in message.buttons[0][0].text
        await message.click(0)

        message: Message = await helper.wait_for_message(in_chats(bot_group) & edited(message.id) & has_button)
        await message.click(text="Restore")

        await helper.wait_for_message(in_chats(bot_group) & edited(message.id) & ~has_button)

        assert_is_linked(channel, tuple(), bot_group)


async def test_link_chat_group_linked_relink(helper, client, bot_id, bot_group, bot_channel, slave, channel,
                                              private_response):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_channel):
        with link_chats(channel, tuple(), bot_group):
            await simulate_link_chat(
                client, helper, chat, bot_id, bot_group, command_channel=bot_channel, private_response=private_response
            )
            assert_is_linked(channel, tuple(), bot_channel)
            assert_is_linked(channel, (chat,), bot_group)


async def test_link_chat_channel(helper, client, bot_id, bot_group, bot_channel, slave, channel,
                                 private_response):
    chat = slave.chat_with_alias
    with link_chats(channel, tuple(), bot_channel):
        await simulate_link_chat(
            client, helper, chat, bot_id, bot_id, dest_channel=bot_channel, private_response=private_response
        )
        assert_is_linked(channel, (chat,), bot_channel)


async def test_link_chat_channel_linked_cancel(helper, client, bot_id, bot_channel, slave, channel,
                                               private_response):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_channel):
        message: Message = await client.send_message(bot_channel, f"/link")
        message = await private_response(
            lambda: message.forward_to(bot_id),
            lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
        )
        assert 2 == len(message.buttons), "link message from channel should have 2 rows"
        await message.click(text="Cancel")


async def test_link_chat_target_incoming_message(helper, client, bot_id, slave, channel,
                                                  private_response):
    chat = slave.chat_with_alias
    efb_msg = slave.send_text_message(chat, chat.other)

    incoming_msg = await helper.wait_for_message(in_chats(bot_id) & regex(re.escape(efb_msg.text)))
    message = await private_response(
        lambda: client.send_message(bot_id, "/link", reply_to=incoming_msg),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    assert chat.display_name in message.raw_text
    await message.click(text="Cancel")


async def simulate_link_chat(client, helper, chat: Chat, command_chat: int, dest_chat: int, private_response,
                             command_channel: Optional[int] = None, dest_channel: Optional[int] = None):
    """Simulate the procedure of linking a chat.

    Provide command_channel to link from a channel.
    """
    if command_channel is not None:
        async def trigger():
            message = await client.send_message(command_channel, f"/link {chat.uid}")
            await message.forward_to(command_chat)
    else:
        async def trigger():
            await client.send_message(command_chat, f"/link {chat.uid}")
    receive = lambda timeout: helper.wait_for_message(in_chats(command_chat) & has_button, timeout)
    message = await private_response(trigger, receive)
    session_message_id = message.id
    await message.buttons[0][0].click()  # choose chat
    message = await helper.wait_for_message(
        in_chats(command_chat) & edited(session_message_id) & has_button
    )  # operation panel
    url = None
    # print("STIMULATE_LINK_CHAT_MESSAGE_DICT", message.to_dict())
    for i in chain.from_iterable(message.buttons):
        if i.url:
            url = i.url
            break
    assert url is not None
    match = re.search(r"\?startgroup=(.+)", url)
    assert match is not None
    token = match.group(1)
    command = f"/start {token}"
    if dest_channel:
        message = await client.send_message(dest_channel, command)
        await message.forward_to(dest_chat)
    else:
        await client.send_message(dest_chat, command)
    completion = await helper.wait_for_message(
        in_chats(command_chat) & edited(session_message_id) & ~has_button
    )


async def test_group_chat_migration(client, helper, channel, slave, bot_id):
    slave_chats = slave.chats_by_chat_type["PrivateChat"]
    title = f"Chat upgrade test {uuid4()}"
    response = await client(CreateChatRequest(users=[bot_id], title=title))
    if getattr(response, "chats", None):
        chat: TelethonChat = response.chats[0]
    elif getattr(response, "updates", None) and getattr(response.updates, "chats", None):
        chat: TelethonChat = response.updates.chats[0]
    else:
        chat = await client.get_entity(title)
    with link_chats(channel, slave_chats, get_peer_id(chat)):
        mega_chat_response = await client(MigrateChatRequest(chat_id=chat.id))
        if getattr(mega_chat_response, "chats", None):
            mega_chat: TelethonChat = next(
                c for c in mega_chat_response.chats
                if getattr(c, "id", None) != chat.id
            )
        elif getattr(mega_chat_response, "updates", None) and getattr(mega_chat_response.updates, "chats", None):
            mega_chat = next(
                c for c in mega_chat_response.updates.chats
                if getattr(c, "id", None) != chat.id
            )
        else:
            mega_chat = await client.get_entity(get_peer_id(chat))

        deadline = asyncio.get_running_loop().time() + 20
        while True:
            migrated = get_peer_id(mega_chat)
            original = get_peer_id(chat)
            try:
                assert_is_linked(channel, slave_chats, migrated)
                assert_is_linked(channel, tuple(), original)
            except AssertionError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.1)
            else:
                break

    # Clean up
    unlink_all_chats(channel, get_peer_id(mega_chat))
    unlink_all_chats(channel, get_peer_id(chat))
    await client(DeleteChannelRequest(mega_chat.id))
