import threading
from unittest.mock import patch

import pytest

from .helper.filters import edited, in_chats, text
from .utils import get_start_link, unlink_all_chats

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("backfill_flag", ["false", "no", "off", "0"])
async def test_link_chat_start_false_skips_backfill(helper, client, bot_id, bot_group, slave, channel, private_response, backfill_flag):
    try:
        start_link = await get_start_link(client, helper, bot_id, slave.chat_with_alias.uid, private_response)
        with patch.object(channel.history_replay, "start") as migrate_chat_history, patch.object(channel.link_completion, "send_history_link") as send_history_link:
            await client.send_message(bot_group, f"/start {start_link.token} {backfill_flag}")
            await helper.wait_for_message(in_chats(bot_id) & edited(start_link.session_message_id) & text)

        migrate_chat_history.assert_not_called()
        send_history_link.assert_not_called()
    finally:
        unlink_all_chats(channel, bot_group)


@pytest.mark.parametrize("backfill_flag", ["true", "yes", "on", "1"])
async def test_link_chat_start_true_forces_backfill_on_relink(helper, client, bot_id, bot_group, slave, channel, private_response, backfill_flag):
    try:
        first_start_link = await get_start_link(client, helper, bot_id, slave.chat_with_alias.uid, private_response)
        await client.send_message(bot_group, f"/start {first_start_link.token}")
        await helper.wait_for_message(in_chats(bot_id) & edited(first_start_link.session_message_id) & text)

        start_link = await get_start_link(client, helper, bot_id, slave.chat_with_alias.uid, private_response)
        migration_called = threading.Event()
        original_migrate_chat_history = channel.history_replay.start

        def observe_migration(*args, **kwargs):
            migration_called.set()
            return original_migrate_chat_history(*args, **kwargs)

        with (
            patch.object(channel.history_replay, "start", side_effect=observe_migration) as migrate_chat_history,
            patch.object(channel.link_completion, "send_history_link") as send_history_link,
        ):
            await client.send_message(bot_group, f"/start {start_link.token} {backfill_flag}")
            await helper.wait_for_message(in_chats(bot_id) & edited(start_link.session_message_id) & text)
            assert migration_called.wait(timeout=5), "Timed out waiting for migrate_chat_history"

        migrate_chat_history.assert_called()
        send_history_link.assert_not_called()
    finally:
        unlink_all_chats(channel, bot_group)
