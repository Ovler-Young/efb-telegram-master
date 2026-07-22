from pytest import mark
from telethon.tl.custom import Message

from .helper.filters import in_chats
from .utils import link_chats, assert_is_linked

pytestmark = mark.asyncio


async def test_unlink_all_private_guidance(helper, client, bot_id, private_response):
    content: str = await private_response(
        lambda: client.send_message(bot_id, "/unlink_all"),
        lambda timeout: helper.wait_for_message_text(in_chats(bot_id), timeout),
    )
    assert "/unlink_all" in content
    assert "group to unlink all remote chats" in content


async def test_unlink_all_group_empty(helper, client, bot_group, slave, channel):
    with link_chats(channel, tuple(), bot_group):
        await client.send_message(bot_group, "/unlink_all")
        content: str = await helper.wait_for_message_text(in_chats(bot_group))
        assert content == "No chat is linked to the group."
        assert_is_linked(channel, tuple(), bot_group)


async def test_unlink_all_group_linked(helper, client, bot_group, slave, channel):
    linked_chats = slave.get_chats_by_criteria(alias=True, avatar=True)
    with link_chats(channel, linked_chats, bot_group):
        await client.send_message(bot_group, "/unlink_all")
        content: str = await helper.wait_for_message_text(in_chats(bot_group))
        assert content == f"All {len(linked_chats)} chats has been unlinked from this group."
        assert_is_linked(channel, tuple(), bot_group)


async def test_unlink_all_channel_linked(helper, client, bot_channel, bot_id, slave, channel,
                                         private_response):
    with link_chats(channel, slave.get_chats_by_criteria(alias=True, avatar=True), bot_channel):
        message: Message = await client.send_message(bot_channel, "/unlink_all")
        await private_response(
            lambda: message.forward_to(bot_id),
            lambda timeout: helper.wait_for_message(in_chats(bot_id), timeout),
        )
        assert_is_linked(channel, tuple(), bot_channel)
