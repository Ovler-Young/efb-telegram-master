"""Author custom emoji in the header of a group message, with a bot created by a Premium account.

Runs only when ``ETM_TEST_PREMIUM_BOT`` is set, with ``TOKEN`` of such a bot, and ``ADMINS[0]`` having started that bot.
It creates a sticker set owned by ``ADMINS[0]`` and deletes it afterwards.
"""
import asyncio
import io
import os
import uuid

import httpx
import pytest
from telethon.tl.types import MessageEntityCustomEmoji

from efb_telegram_master import custom_emoji, utils
from efb_telegram_master.db import UserEmojiCache, database
from efb_telegram_master.db_runtime import connection_scope

from ..conftest import build_channel_config, load_test_coordinator
from .helper.filters import in_chats, regex
from .utils import link_chats

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.getenv("ETM_TEST_PREMIUM_BOT"), reason="ETM_TEST_PREMIUM_BOT is not set"),
]

SET_BASE_NAME = f"etm_it_{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def coordinator(tmp_path_factory, monkey_class, bot_token, bot_admins):
    config = build_channel_config(bot_token, bot_admins)
    config["user_emoji"] = {"sticker_set_name": SET_BASE_NAME}
    coordinator_obj = load_test_coordinator(tmp_path_factory, monkey_class, config)
    # The tests poll through the bot manager, which does not start the emoji worker; ``poll`` would.
    coordinator_obj.master.custom_emoji_creator.start()
    yield coordinator_obj
    coordinator_obj.master.stop_polling()
    for slave_channel in coordinator_obj.slaves.values():
        slave_channel.stop_polling()
    bot_username = coordinator_obj.master.bot_manager.me.username
    httpx.post(f"https://api.telegram.org/bot{bot_token}/deleteStickerSet",
               data={"name": f"{SET_BASE_NAME}_by_{bot_username}"}, timeout=60)


def custom_emoji_entities(message):
    return [entity for entity in message.entities or [] if isinstance(entity, MessageEntityCustomEmoji)]


def sticker_set_ids(bot_token, set_name):
    reply = httpx.post(f"https://api.telegram.org/bot{bot_token}/getStickerSet",
                       data={"name": set_name}, timeout=60).json()
    return [sticker.get("custom_emoji_id") for sticker in reply["result"]["stickers"]]


async def wait_for_emoji(channel, uid, other_than=None):
    """The author's emoji once it exists and differs from ``other_than``; ``None`` if that takes too long."""
    for _ in range(90):
        found = channel.custom_emoji.lookup(uid)
        if found is not None and found.custom_emoji_id != other_than:
            return found
        await asyncio.sleep(1)
    return None


async def test_author_custom_emoji_appears_after_it_is_created(helper, slave, channel, bot_group, monkeypatch):
    chat = slave.group
    author, other = [member for member in chat.members if member.uid != chat.self.uid][:2]
    uid = utils.chat_id_to_str(channel_id=author.module_id, chat_uid=author.uid)

    def member_picture(_member):
        with open("tests/mocks/A.png", "rb") as picture:
            return io.BytesIO(picture.read())

    monkeypatch.setattr(slave, "get_chat_member_picture", member_picture, raising=False)

    with link_chats(channel, (chat,), bot_group):
        first = slave.send_text_message(chat=chat, author=author)
        first_tg = await helper.wait_for_message(in_chats(bot_group) & regex(first.text))
        assert custom_emoji_entities(first_tg) == []

        for _ in range(60):
            created = channel.custom_emoji.lookup(uid)
            if created is not None:
                break
            await asyncio.sleep(1)
        assert created is not None, "the author's custom emoji was not created in time"

        second = slave.send_text_message(chat=chat, author=author)
        second_tg = await helper.wait_for_message(in_chats(bot_group) & regex(second.text))
        entities = custom_emoji_entities(second_tg)
        assert [entity.document_id for entity in entities] == [int(created.custom_emoji_id)]

        # A combined message: the author with an emoji first, then one whose emoji does not exist yet.
        monkeypatch.setitem(channel.flag.config, "text_aggregation", True)
        third = slave.send_text_message(chat=chat, author=author)
        await helper.wait_for_message(in_chats(bot_group) & regex(third.text))
        fourth = slave.send_text_message(chat=chat, author=other)
        combined = await helper.wait_for_message(in_chats(bot_group) & regex(fourth.text))
        assert third.text in combined.raw_text
        entities = custom_emoji_entities(combined)
        assert [(entity.offset, entity.document_id) for entity in entities] == [(0, int(created.custom_emoji_id))]


async def test_author_custom_emoji_is_replaced_when_the_avatar_changes(
        helper, slave, channel, bot_group, bot_token, monkeypatch):
    chat = slave.group
    author = [member for member in chat.members if member.uid != chat.self.uid][2]
    uid = utils.chat_id_to_str(channel_id=author.module_id, chat_uid=author.uid)
    avatar = {"path": "tests/mocks/A.png"}

    def member_picture(_member):
        with open(avatar["path"], "rb") as picture:
            return io.BytesIO(picture.read())

    monkeypatch.setattr(slave, "get_chat_member_picture", member_picture, raising=False)
    monkeypatch.setattr(custom_emoji, "GROUP_AVATAR_RECHECK_INTERVAL", 0.01)

    with link_chats(channel, (chat,), bot_group):
        first = slave.send_text_message(chat=chat, author=author)
        await helper.wait_for_message(in_chats(bot_group) & regex(first.text))
        created = await wait_for_emoji(channel, uid)
        assert created is not None, "the author's custom emoji was not created in time"
        set_name = channel.custom_emoji.saved(uid).sticker_set_name
        assert created.custom_emoji_id in sticker_set_ids(bot_token, set_name)

        avatar["path"] = "tests/mocks/B.png"

        # The message that follows the change is sent at once with the emoji that exists.
        second = slave.send_text_message(chat=chat, author=author)
        second_tg = await helper.wait_for_message(in_chats(bot_group) & regex(second.text))
        assert [entity.document_id for entity in custom_emoji_entities(second_tg)] == [int(created.custom_emoji_id)]

        replaced = await wait_for_emoji(channel, uid, other_than=created.custom_emoji_id)
        assert replaced is not None, "the author's custom emoji was not replaced in time"

        third = slave.send_text_message(chat=chat, author=author)
        third_tg = await helper.wait_for_message(in_chats(bot_group) & regex(third.text))
        assert [entity.document_id for entity in custom_emoji_entities(third_tg)] == [int(replaced.custom_emoji_id)]

        ids = sticker_set_ids(bot_token, set_name)
        assert replaced.custom_emoji_id in ids
        assert created.custom_emoji_id not in ids


async def test_author_custom_emoji_saved_by_an_earlier_version_is_made_again_on_the_next_message(
        helper, slave, channel, bot_group, bot_token, monkeypatch):
    chat = slave.group
    author = [member for member in chat.members if member.uid != chat.self.uid][3]
    uid = utils.chat_id_to_str(channel_id=author.module_id, chat_uid=author.uid)

    def member_picture(_member):
        with open("tests/mocks/A.png", "rb") as picture:
            return io.BytesIO(picture.read())

    monkeypatch.setattr(slave, "get_chat_member_picture", member_picture, raising=False)
    monkeypatch.setattr(custom_emoji, "GROUP_AVATAR_RECHECK_INTERVAL", 0.01)

    with link_chats(channel, (chat,), bot_group):
        first = slave.send_text_message(chat=chat, author=author)
        await helper.wait_for_message(in_chats(bot_group) & regex(first.text))
        created = await wait_for_emoji(channel, uid)
        assert created is not None, "the author's custom emoji was not created in time"
        set_name = channel.custom_emoji.saved(uid).sticker_set_name
        assert created.custom_emoji_id in sticker_set_ids(bot_token, set_name)

        # The row as an earlier version saved it: no avatar fingerprint. Loaded again, as after an upgrade.
        with connection_scope(database.obj):
            UserEmojiCache.update(avatar_hash=None).where(UserEmojiCache.cache_key == f"member-user:{uid}").execute()
        channel.custom_emoji.load()
        assert channel.custom_emoji.saved(uid).avatar_hash is None

        second = slave.send_text_message(chat=chat, author=author)
        second_tg = await helper.wait_for_message(in_chats(bot_group) & regex(second.text))
        assert [entity.document_id for entity in custom_emoji_entities(second_tg)] == [int(created.custom_emoji_id)]

        remade = await wait_for_emoji(channel, uid, other_than=created.custom_emoji_id)
        assert remade is not None, "the author's custom emoji was not made again in time"
        saved = channel.custom_emoji.saved(uid)
        assert (saved.sticker_set_name, saved.avatar_hash is not None) == (set_name, True)

        third = slave.send_text_message(chat=chat, author=author)
        third_tg = await helper.wait_for_message(in_chats(bot_group) & regex(third.text))
        assert [entity.document_id for entity in custom_emoji_entities(third_tg)] == [int(remade.custom_emoji_id)]

        ids = sticker_set_ids(bot_token, set_name)
        assert remade.custom_emoji_id in ids
        assert created.custom_emoji_id not in ids
