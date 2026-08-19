import re
from itertools import chain
from typing import Optional

from ehforwarderbot import Chat
from pytest import mark
from telethon.errors import MessageIdInvalidError
from telethon.tl.custom import Message

from .helper.messages import wait_for_message_state, wait_for_new_message_after

retry_on_message_id_invalid_error = mark.flaky(
    max_runs=2,
    min_passes=1,  # default value
    rerun_filter=lambda err, *_: issubclass(err[0], MessageIdInvalidError),
)
"""Retry on ``MessageIdInvalidError`` due to flaky behavior of MTProto API"""


async def simulate_link_chat(client, chat: Chat, command_chat: int, dest_chat: int, private_response, command_channel: Optional[int] = None, dest_channel: Optional[int] = None):
    """Simulate the procedure of linking a chat.

    Provide command_channel to link from a channel.
    """
    if command_channel is not None:
        command_message_id = None

        async def trigger():
            nonlocal command_message_id
            message = await client.send_message(command_channel, f"/link {chat.uid}")
            forwarded = await message.forward_to(command_chat)
            command_message_id = forwarded.id
    else:
        command_message_id = None

        async def trigger():
            nonlocal command_message_id
            command = await client.send_message(command_chat, f"/link {chat.uid}")
            command_message_id = command.id

    def has_target_selection(current: Message) -> bool:
        return bool(current.button_count) and chat.display_name in current.buttons[0][0].text

    def is_link_response(current: Message) -> bool:
        return current.raw_text == "Processing..." or has_target_selection(current)

    async def receive_selection_panel(timeout: float) -> Message:
        assert command_message_id is not None
        response = await wait_for_new_message_after(client, command_chat, command_message_id, is_link_response, timeout=timeout)
        return await wait_for_message_state(client, command_chat, response.id, has_target_selection, timeout=timeout)

    message = await private_response(trigger, receive_selection_panel, target_chat_id=command_chat)
    session_message_id = message.id
    choose_chat = message.buttons[0][0]
    selection_text = message.raw_text

    def is_operation_panel(current: Message) -> bool:
        buttons = tuple(chain.from_iterable(current.buttons))
        return (
            current.raw_text != selection_text
            and bool(current.button_count)
            and any(button.url and "?startgroup=" in button.url for button in buttons)
            and any(button.text.lower().startswith("manual ") for button in buttons)
        )

    message = await private_response(
        choose_chat.click,
        lambda timeout: wait_for_message_state(client, command_chat, session_message_id, is_operation_panel, timeout=timeout),
        target_chat_id=command_chat,
    )
    url = None
    for i in chain.from_iterable(message.buttons):
        if i.url:
            url = i.url
            break
    assert url is not None
    match = re.search(r"\?startgroup=(.+)", url)
    assert match is not None
    token = match.group(1)
    command = f"/start {token}"

    async def complete_link():
        if dest_channel:
            message = await client.send_message(dest_channel, command)
            await message.forward_to(dest_chat)
        else:
            await client.send_message(dest_chat, command)

    await private_response(
        complete_link,
        lambda timeout: wait_for_message_state(client, command_chat, session_message_id, lambda current: not current.button_count, timeout=timeout),
        target_chat_id=command_chat,
    )
