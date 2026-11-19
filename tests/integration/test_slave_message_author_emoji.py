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

from efb_telegram_master import utils

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


async def test_author_custom_emoji_appears_after_it_is_created(helper, slave, channel, bot_group, monkeypatch):
    chat = slave.group
    author = next(member for member in chat.members if member.uid != chat.self.uid)
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
