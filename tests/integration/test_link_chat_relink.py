import asyncio
from uuid import uuid4

from pytest import mark
from telethon.tl.functions.channels import DeleteChannelRequest
from telethon.tl.functions.messages import CreateChatRequest, MigrateChatRequest
from telethon.tl.types import Chat as TelethonChat
from telethon.utils import get_peer_id

from .link_chat_flows import retry_on_message_id_invalid_error, simulate_link_chat
from .utils import assert_is_linked, link_chats, unlink_all_chats

pytestmark = [mark.asyncio, retry_on_message_id_invalid_error]


async def test_link_chat_group_linked_relink(helper, client, bot_id, bot_group, bot_channel, slave, channel, private_response):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_channel):
        with link_chats(channel, tuple(), bot_group):
            await simulate_link_chat(client, chat, bot_id, bot_group, command_channel=bot_channel, private_response=private_response)
            assert_is_linked(channel, tuple(), bot_channel)
            assert_is_linked(channel, (chat,), bot_group)


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
    mega_chat = None
    try:
        with link_chats(channel, slave_chats, get_peer_id(chat)):
            mega_chat_response = await client(MigrateChatRequest(chat_id=chat.id))
            if getattr(mega_chat_response, "chats", None):
                mega_chat = next(c for c in mega_chat_response.chats if getattr(c, "id", None) != chat.id)
            elif getattr(mega_chat_response, "updates", None) and getattr(mega_chat_response.updates, "chats", None):
                mega_chat = next(c for c in mega_chat_response.updates.chats if getattr(c, "id", None) != chat.id)
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
    finally:
        if mega_chat is not None:
            await unlink_all_chats(channel, client, helper, get_peer_id(mega_chat))
            await client(DeleteChannelRequest(mega_chat.id))
        else:
            await client.delete_dialog(chat)
