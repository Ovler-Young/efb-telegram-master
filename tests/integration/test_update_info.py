import asyncio

from pytest import mark
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import PeerChannel
from telethon.tl.types.messages import ChatFull
from telethon.utils import resolve_id

from .helper.filters import in_chats, new_photo, new_title, regex, text
from .utils import is_bot_admin, link_chats

pytestmark = mark.asyncio


async def test_update_info_private_guidance(helper, client, bot_id, private_response):
    content = await private_response(
        lambda: client.send_message(bot_id, "/update_info"),
        lambda timeout: helper.wait_for_message_text(text & in_chats(bot_id), timeout),
    )
    assert "Send /update_info to a group" in content


async def test_update_info_group_empty(helper, client, bot_group, channel, private_response):
    content = await private_response(
        lambda: client.send_message(bot_group, "/update_info"),
        lambda timeout: helper.wait_for_message_text(text & in_chats(bot_group), timeout),
        source_channel=channel,
        target_chat_id=bot_group,
    )
    assert "This only works in a group linked with one chat. Currently 0 chats linked to this group." in content


async def test_update_info_group_multi(helper, client, bot_group, channel, slave, private_response):
    with link_chats(channel, slave.get_chats_by_criteria(alias=True), bot_group):
        content = await private_response(
            lambda: client.send_message(bot_group, "/update_info"),
            lambda timeout: helper.wait_for_message_text(text & in_chats(bot_group), timeout),
            source_channel=channel,
            target_chat_id=bot_group,
        )
        assert "This only works in a group linked with one chat." in content


async def test_update_info_no_permission(helper, client, bot_group, bot_id, channel, slave, private_response):
    with link_chats(channel, (slave.chat_with_alias,), bot_group):
        if await is_bot_admin(client, bot_id, bot_group):
            await client.edit_admin(bot_group, bot_id, change_info=False, is_admin=False, edit_messages=False)
        content = await private_response(
            lambda: client.send_message(bot_group, "/update_info"),
            lambda timeout: helper.wait_for_message_text(text & in_chats(bot_group), timeout),
            source_channel=channel,
            target_chat_id=bot_group,
        )
        assert "Error occurred while update chat details." in content


@mark.parametrize("chat_type", ["PrivateChat", "GroupChat"])
@mark.parametrize("alias", [False, True], ids=["no alias", "alias"])
@mark.parametrize("avatar", [False, True], ids=["no avatar", "avatar"])
async def test_update_info_group_user(helper, client, bot_group, channel, slave, bot_id, chat_type, alias, avatar, private_response):
    chat = slave.get_chat_by_criteria(chat_type=chat_type, alias=alias, avatar=avatar)

    # Set bot as admin if needed
    if await is_bot_admin(client, bot_id, bot_group):
        await client.edit_admin(bot_group, bot_id, change_info=True, is_admin=True, delete_messages=False)

    with link_chats(channel, (chat,), bot_group):

        async def receive_update(timeout):
            deadline = asyncio.get_running_loop().time() + timeout
            title = (await helper.wait_for_event(in_chats(bot_group) & new_title, timeout)).new_title
            if not avatar:
                return title

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0.0:
                raise TimeoutError("Chat details update did not arrive within the response budget")
            await helper.wait_for_event(in_chats(bot_group) & new_photo, remaining)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0.0:
                raise TimeoutError("Chat details feedback did not arrive within the response budget")
            feedback = await helper.wait_for_message(in_chats(bot_group) & text & regex("Chat details updated"), remaining)
            return title, feedback

        update = await private_response(
            lambda: client.send_message(bot_group, "/update_info"),
            receive_update,
            source_channel=channel,
            target_chat_id=bot_group,
        )
        if avatar:
            title, feedback = update
            assert "Chat details updated" in feedback.text
        else:
            title = update
        if alias:
            assert chat.alias in title
        else:
            assert chat.name in title

        if chat_type == "GroupChat":
            bot_group_t, peer_type = resolve_id(bot_group)
            deadline = asyncio.get_running_loop().time() + 20
            while True:
                if peer_type == PeerChannel:
                    group: ChatFull = await client(GetFullChannelRequest(bot_group_t))
                else:
                    group = await client(GetFullChatRequest(bot_group_t))
                desc = group.full_chat.about

                chats_found = sum(
                    int(
                        (i.name in desc)  # Original name is found, and
                        and (i.alias is None or i.alias in desc)  # alias is found too if available
                    )
                    for i in chat.members
                )

                assert len(chat.members) >= 5
                if chats_found >= 5:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    assert chats_found >= 5, f"At least 5 members shall be found in the description: {desc}"
                await asyncio.sleep(0.1)
