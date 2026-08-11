import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import chain
from typing import Iterable

from ehforwarderbot import Chat
from ehforwarderbot.types import ChatID
from telethon import TelegramClient
from telethon.tl.types import ChannelParticipantsAdmins

from efb_telegram_master import TelegramChannel
from efb_telegram_master.utils import chat_id_to_str

from .helper.filters import edited, has_button, in_chats, reply_to, text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartLink:
    token: str
    session_message_id: int


@contextmanager
def link_chats(channel: TelegramChannel, slave_chats: Iterable[Chat], telegram_chat_id: int):
    """Link a list of remote chats to a Telegram chat and revert the changes
    upon finishing.
    """
    # Link the chats
    db = channel.chat_associations
    slave_ids = [chat_id_to_str(chat=i) for i in slave_chats]
    master_str = chat_id_to_str(channel.channel_id, ChatID(str(telegram_chat_id)))
    backup = tuple(db.get_chat_assoc(master_uid=master_str))

    db.remove_chat_assoc(master_uid=master_str)
    for i in slave_ids:
        db.add_chat_assoc(master_str, i, multiple_slave=True)
    body_failed = False
    try:
        yield
    except BaseException:
        body_failed = True
        raise
    finally:
        try:
            db.remove_chat_assoc(master_uid=master_str)
            for i in backup:
                db.add_chat_assoc(master_str, i, multiple_slave=True)
        except BaseException:
            if not body_failed:
                raise
            logger.exception("Unable to restore chat associations after a test failure")


async def is_bot_admin(client: TelegramClient, bot_id: int, group):
    async for admin in client.iter_participants(group, filter=ChannelParticipantsAdmins()):
        if admin.id == bot_id:
            return True

    return False


async def get_start_link(client, helper, bot_id, chat_uid, private_response) -> StartLink:
    message = await private_response(
        lambda: client.send_message(bot_id, f"/link {chat_uid}"),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & has_button, timeout),
    )
    session_message_id = message.id
    message = await private_response(
        lambda: message.buttons[0][0].click(),
        lambda timeout: helper.wait_for_message(in_chats(bot_id) & edited(session_message_id) & has_button, timeout),
    )
    url = None
    for button in chain.from_iterable(message.buttons):
        if button.url:
            url = button.url
            break
    assert url
    match = re.search(r"\?startgroup=(.+)", url)
    assert match is not None
    return StartLink(match.groups()[0], message.id)


def assert_is_linked(channel: TelegramChannel, slave_chats: Iterable[Chat], telegram_chat_id: int):
    master_str = chat_id_to_str(channel.channel_id, ChatID(str(telegram_chat_id)))
    chats_str = set(channel.chat_associations.get_chat_assoc(master_uid=master_str))
    slave_ids = {chat_id_to_str(chat=i) for i in slave_chats}
    # print("ASSERT_IS_LINKED", chats_str, slave_ids)
    assert chats_str == slave_ids, f"expecting {slave_ids} linked, found {chats_str}"


async def unlink_all_chats(channel: TelegramChannel, client: TelegramClient, helper, telegram_chat_id: int) -> None:
    helper.watch_chat(telegram_chat_id)
    try:
        command = await client.send_message(telegram_chat_id, "/unlink_all")
        await helper.wait_for_message(in_chats(telegram_chat_id) & reply_to(command.id) & text, timeout=65.0)
    finally:
        helper.unwatch_chat(telegram_chat_id)
    master_str = chat_id_to_str(channel.channel_id, ChatID(str(telegram_chat_id)))
    assert not channel.chat_associations.get_chat_assoc(master_uid=master_str)
