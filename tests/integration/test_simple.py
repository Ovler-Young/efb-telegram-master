import re

from ehforwarderbot.__version__ import __version__ as efb_version
from pytest import mark
from telethon.tl.custom import Message

from .helper.filters import in_chats, regex
from .utils import link_chats

pytestmark = mark.asyncio


async def test_start(helper, client, bot_id, private_response):
    text = await private_response(
        lambda: client.send_message(bot_id, "/start"),
        lambda timeout: helper.wait_for_message_text(in_chats(bot_id), timeout),
    )
    assert "EFB Telegram Master Channel" in text


async def test_help(helper, client, bot_group):
    await client.send_message(bot_group, "/help")
    text = await helper.wait_for_message_text(in_chats(bot_group))
    for i in ("/link", "/chat", "/extra", "/unlink_all", "/info", "/react", "/update_info", "/rm", "/help"):
        assert i in text


async def test_info_bot(helper, client, bot_id, coordinator, channel, slave, private_response):
    text = await private_response(
        lambda: client.send_message(bot_id, "/info"),
        lambda timeout: helper.wait_for_message_text(in_chats(bot_id), timeout),
    )

    assert efb_version in text
    assert coordinator.profile in text

    assert channel.__version__ in text
    if channel.instance_id:
        assert channel.instance_id in text

    assert slave.channel_emoji in text
    assert slave.channel_name in text
    assert slave.channel_id in text
    assert slave.__version__ in text


async def test_info_chat(helper, client, bot_group, channel, slave):
    group_name = (await client.get_entity(bot_group)).title
    await client.send_message(bot_group, "/info")
    text = await helper.wait_for_message_text(in_chats(bot_group))
    assert group_name in text
    assert str(bot_group) in text
    assert "/link" in text

    with link_chats(channel, (slave.unknown_chat, slave.unknown_channel, slave.chat_with_alias), bot_group):
        await client.send_message(bot_group, "/info")
        text = await helper.wait_for_message_text(in_chats(bot_group))

        assert group_name in text
        assert str(bot_group) in text

        assert slave.unknown_channel.module_id in text
        assert slave.unknown_channel.uid in text

        assert slave.unknown_chat.module_id in text
        assert slave.unknown_chat.module_name in text
        assert slave.unknown_chat.uid in text

        assert slave.chat_with_alias.module_id in text
        assert slave.chat_with_alias.module_name in text
        assert slave.chat_with_alias.uid in text
        assert slave.chat_with_alias.name in text
        assert slave.chat_with_alias.alias in text


async def test_info_channel(helper, client, bot_id, bot_channel, channel, slave, private_response):
    # Not linked
    group_name = (await client.get_entity(bot_channel)).title
    message: Message = await client.send_message(bot_channel, "/info")
    text = await private_response(
        lambda: message.forward_to(bot_id),
        lambda timeout: helper.wait_for_message_text(in_chats(bot_id), timeout),
    )
    assert group_name in text
    assert str(bot_channel) in text
    assert "/link" in text

    # Linked group
    with link_chats(channel, (slave.unknown_chat, slave.unknown_channel, slave.chat_with_alias), bot_channel):
        message: Message = await client.send_message(bot_channel, "/info")
        text = await private_response(
            lambda: message.forward_to(bot_id),
            lambda timeout: helper.wait_for_message_text(in_chats(bot_id), timeout),
        )

        # Group info
        assert group_name in text
        assert str(bot_channel) in text

        # Unknown channel
        assert slave.unknown_channel.module_id in text
        assert slave.unknown_channel.uid in text

        # Unknown chat
        assert slave.unknown_chat.module_id in text
        assert slave.unknown_chat.module_name in text
        assert slave.unknown_chat.uid in text

        # Known chat
        assert slave.chat_with_alias.module_id in text
        assert slave.chat_with_alias.module_name in text
        assert slave.chat_with_alias.uid in text
        assert slave.chat_with_alias.name in text
        assert slave.chat_with_alias.alias in text


async def test_extra_echo(helper, client, bot_group, channel, slave):
    # Get command list
    await client.send_message(bot_group, "/extra")
    text = await helper.wait_for_message_text(in_chats(bot_group) & regex("echo"))
    assert slave.echo.name in text

    cmd_match = re.search(r"/[a-zA-Z0-9_-]+echo", text)
    assert cmd_match is not None, "Help text of echo command should be found."
    command = cmd_match.group()

    # Get command help
    await client.send_message(bot_group, command)
    text = await helper.wait_for_message_text(in_chats(bot_group))

    cmd_match = re.search(r"/[a-zA-Z0-9_-]+echo", text)
    assert cmd_match is not None, "Echo command should be found."
    command = cmd_match.group()
    assert slave.echo.name in text
    assert slave.echo.desc.format(function_name=command) in text

    # Run command
    content = "信じたものは、都合のいい妄想を繰り返し映し出す鏡。"
    await client.send_message(bot_group, f"{command} {content}")
    await helper.wait_for_event(in_chats(bot_group) & regex(content))
