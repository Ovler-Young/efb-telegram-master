import asyncio

from pytest import mark
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import PeerChannel
from telethon.tl.types.messages import ChatFull
from telethon.utils import resolve_id

from .helper.filters import in_chats, text, new_title, reply_to, regex
from .utils import link_chats, is_bot_admin

pytestmark = mark.asyncio


async def test_update_info_private_guidance(helper, client, bot_id, private_response):
    content = await private_response(
        lambda: client.send_message(bot_id, "/update_info"),
        lambda timeout: helper.wait_for_message_text(text & in_chats(bot_id), timeout),
    )
    assert "Send /update_info to a group" in content


async def test_update_info_group_empty(helper, client, bot_group):
    await client.send_message(bot_group, "/update_info")
    await helper.wait_for_event(text & in_chats(bot_group))
    # Should receive a text with error messages.
    # No assertions needed.


async def test_update_info_group_multi(helper, client, bot_group, channel, slave):
    with link_chats(
            channel,
            slave.get_chats_by_criteria(alias=True),
            bot_group
    ):
        await client.send_message(bot_group, "/update_info")
        await helper.wait_for_event(text & in_chats(bot_group))
        # Should receive a text with error messages.
        # No assertions needed.


async def test_update_info_no_permission(helper, client, bot_group, bot_id):
    if not await is_bot_admin(client, bot_id, bot_group):
        await client.edit_admin(bot_group, bot_id,
                                change_info=False, is_admin=False, edit_messages=False)
    await client.send_message(bot_group, "/update_info")
    await helper.wait_for_event(text & in_chats(bot_group))
    # Should receive a text with error messages.
    # No assertions needed.


@mark.parametrize("chat_type", ["PrivateChat", "GroupChat"])
@mark.parametrize("alias", [False, True], ids=['no alias', 'alias'])
@mark.parametrize("avatar", [False, True], ids=['no avatar', 'avatar'])
async def test_update_info_group_user(helper, client, bot_group, channel, slave,
                                      bot_id, chat_type, alias, avatar):
    chat = slave.get_chat_by_criteria(chat_type=chat_type, alias=alias, avatar=avatar)

    # Set bot as admin if needed
    if await is_bot_admin(client, bot_id, bot_group):
        await client.edit_admin(bot_group, bot_id,
                                change_info=True, is_admin=True, delete_messages=False)

    with link_chats(channel, (chat,), bot_group):
        command = await client.send_message(bot_group, "/update_info")
        title = (await helper.wait_for_event(in_chats(bot_group) & new_title)).new_title
        if alias:
            assert chat.alias in title
        else:
            assert chat.name in title

        if avatar:
            result = await helper.wait_for_message(
                in_chats(bot_group) & text & reply_to(command.id) & regex("Chat details updated")
            )
            assert "Chat details updated" in result.text

        if chat_type == "GroupChat":
            bot_group_t, peer_type = resolve_id(bot_group)
            deadline = asyncio.get_running_loop().time() + 20
            while True:
                if peer_type == PeerChannel:
                    group: ChatFull = await client(GetFullChannelRequest(bot_group_t))
                else:
                    group = await client(GetFullChatRequest(bot_group_t))
                desc = group.full_chat.about

                chats_found = sum(int(
                    (i.name in desc) and  # Original name is found, and
                    (i.alias is None or i.alias in desc)  # alias is found too if available
                ) for i in chat.members)

                assert len(chat.members) >= 5
                if chats_found >= 5:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    assert chats_found >= 5, (
                        f"At least 5 members shall be found in the description: {desc}"
                    )
                await asyncio.sleep(0.1)
