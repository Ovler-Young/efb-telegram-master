import re
import threading
from itertools import chain
from unittest.mock import patch

import pytest
from telethon.tl.custom import Message

from .helper.filters import has_button, in_chats, text

pytestmark = pytest.mark.asyncio


async def _get_start_token(client, helper, bot_id, chat_uid):
    await client.send_message(bot_id, f"/link {chat_uid}")
    message = await helper.wait_for_message(in_chats(bot_id) & has_button)
    await message.buttons[0][0].click()
    message = await helper.wait_for_message(in_chats(bot_id) & has_button)
    url = None
    for button in chain.from_iterable(message.buttons):
        if button.url:
            url = button.url
            break
    assert url
    return re.search(r"\?startgroup=(.+)", url).groups()[0]


async def test_link_chat_start_false_skips_backfill(helper, client, bot_id, bot_group, slave, channel):
    token = await _get_start_token(client, helper, bot_id, slave.chat_with_alias.uid)

    with patch.object(channel.chat_binding, "migrate_chat_history") as migrate_chat_history, \
         patch.object(channel.chat_binding, "send_history_link") as send_history_link:
        await client.send_message(bot_group, f"/start {token} false")
        await helper.wait_for_message(in_chats(bot_id) & text)

    migrate_chat_history.assert_not_called()
    send_history_link.assert_not_called()


async def test_link_chat_start_true_forces_backfill_on_relink(helper, client, bot_id, bot_group, slave, channel):
    token = await _get_start_token(client, helper, bot_id, slave.chat_with_alias.uid)
    await client.send_message(bot_group, f"/start {token}")
    await helper.wait_for_message(in_chats(bot_id) & text)

    token = await _get_start_token(client, helper, bot_id, slave.chat_with_alias.uid)
    migration_called = threading.Event()
    original_migrate_chat_history = channel.chat_binding.migrate_chat_history

    def observe_migration(*args, **kwargs):
        migration_called.set()
        return original_migrate_chat_history(*args, **kwargs)

    with patch.object(channel.chat_binding, "migrate_chat_history", side_effect=observe_migration) as migrate_chat_history, \
         patch.object(channel.chat_binding, "send_history_link") as send_history_link:
        await client.send_message(bot_group, f"/start {token} true")
        await helper.wait_for_message(in_chats(bot_id) & text)
        assert migration_called.wait(timeout=5), "Timed out waiting for migrate_chat_history"

    migrate_chat_history.assert_called()
    send_history_link.assert_not_called()
