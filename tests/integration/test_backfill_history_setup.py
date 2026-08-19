import asyncio
import threading
from typing import AsyncGenerator
from unittest.mock import patch
from uuid import uuid4

import pytest
from ehforwarderbot.types import ChatID

from efb_telegram_master.core import utils as etm_utils
from efb_telegram_master.core.constants import Flags
from efb_telegram_master.core.utils import TelegramChatID, TelegramMessageID
from efb_telegram_master.link.callback_sessions import ChatListStorage

from .helper.messages import wait_for_new_message_after
from .utils import get_start_link


@pytest.fixture(scope="module")
def poll_bot(channel_with_auxiliary_bots, poll_bot_factory):
    poll_bot_factory.start(channel_with_auxiliary_bots)
    yield channel_with_auxiliary_bots.bot_manager
    poll_bot_factory.stop(channel_with_auxiliary_bots)


@pytest.fixture(scope="function")
async def helper(helper_wrap, slave_with_auxiliary_bots) -> AsyncGenerator:
    helper_wrap.clear_queue()
    assert helper_wrap.queue.empty()
    slave_with_auxiliary_bots.clear_messages()
    assert slave_with_auxiliary_bots.messages.empty()
    slave_with_auxiliary_bots.clear_statuses()
    assert slave_with_auxiliary_bots.statuses.empty()
    yield helper_wrap


async def link_chat(
    client,
    helper,
    bot_id: int,
    chat_uid: str,
    dest_chat_id: int,
    private_response,
    *,
    flag: str | None = None,
    start_token: str | None = None,
    channel=None,
    storage_key: tuple[TelegramChatID, TelegramMessageID] | None = None,
    slave_uid: str | None = None,
):
    if start_token is None:
        start_token = (await get_start_link(client, helper, bot_id, chat_uid, private_response)).token
    command = f"/start {start_token}"
    if flag is not None:
        command += f" {flag}"
    command_message = None

    async def trigger():
        nonlocal command_message
        command_message = await client.send_message(dest_chat_id, command)

    async def receive(timeout):
        assert command_message is not None
        return await wait_for_new_message_after(
            client,
            dest_chat_id,
            command_message.id,
            lambda message: "is now linked." in (message.raw_text or ""),
            timeout=timeout,
        )

    if channel is None or storage_key is None:
        await private_response(trigger, receive)
    else:
        completed = threading.Event()
        completion_errors: list[BaseException] = []
        original_complete = channel.link_completion.complete

        def observe_completion(update, args):
            if not args or args[0] != start_token or (flag is not None and args[1:2] != [flag]):
                return original_complete(update, args)
            try:
                result = original_complete(update, args)
            except BaseException as error:
                completion_errors.append(error)
                completed.set()
                raise
            completed.set()
            return result

        try:
            async with asyncio.timeout(65.0):
                with patch.object(channel.link_completion, "complete", new=observe_completion):
                    await trigger()
                    await asyncio.to_thread(completed.wait)
        except TimeoutError as error:
            raise AssertionError("Telegram /start was not processed within 65 seconds") from error

        if completion_errors:
            raise completion_errors[0]

        assert channel.callback_sessions.lookup(storage_key) is None, "Telegram /start did not consume its callback session."
        assert channel.callback_sessions.get(channel.link_handler, storage_key) is None, "Telegram /start did not clear its callback handler state."
        assert slave_uid is not None
        expected_master_uid = etm_utils.chat_id_to_str(channel.channel_id, ChatID(str(dest_chat_id)))
        assert channel.chat_associations.get_chat_assoc(slave_uid=slave_uid) == [expected_master_uid]
    assert command_message is not None
    return command_message


def create_relink_start_token(channel_with_auxiliary_bots, etm_chat, *, private_chat_owner: TelegramChatID) -> tuple[str, tuple[TelegramChatID, TelegramMessageID]]:
    placeholder = channel_with_auxiliary_bots.bot_manager.api.send_message(
        private_chat_owner,
        f"Relink callback placeholder {uuid4().hex}",
        _force_main_bot=True,
    )
    assert placeholder.sender_bot_id is None, "Relink callback placeholder must be owned by the main bot."
    storage_key = (private_chat_owner, TelegramMessageID(placeholder.message_id))
    channel_with_auxiliary_bots.callback_sessions.start(
        channel_with_auxiliary_bots.link_handler,
        storage_key,
        Flags.LINK_EXEC,
        int(private_chat_owner),
        ChatListStorage([etm_chat]),
    )
    return etm_utils.b64en(etm_utils.message_id_to_str(*storage_key)), storage_key
