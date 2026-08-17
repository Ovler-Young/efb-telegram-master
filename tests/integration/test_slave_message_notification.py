import asyncio
from unittest.mock import patch

from ehforwarderbot.chat import ChatNotificationState
from pytest import mark, raises

from tests.integration.helper.filters import in_chats, regex
from tests.integration.utils import link_chats

pytestmark = mark.asyncio

# Only testing text messages here, assuming all other message types shall follow suit.


@mark.parametrize(
    "notification_state,mentioned",
    [
        (ChatNotificationState.NONE, True),
        (ChatNotificationState.MENTIONS, True),
        (ChatNotificationState.ALL, True),
        (ChatNotificationState.MENTIONS, False),
    ],
    ids=["mentioned-NONE", "mentioned-MENTIONS", "mentioned-ALL", "not mentioned-MENTIONS"],
)
async def test_slave_message_notification(helper, bot_group, slave, channel, notification_state, mentioned):
    chat = slave.get_chat_by_criteria(chat_type="PrivateChat", notification=notification_state)
    with link_chats(channel, (chat,), bot_group), patch.dict(channel.flag.config, message_muted_on_slave="silent"):
        efb_msg = slave.send_text_message(chat=chat, author=chat.other, substitution=mentioned)
        tg_msg = await helper.wait_for_message(in_chats(bot_group) & regex(efb_msg.text))
        if notification_state == ChatNotificationState.NONE:
            should_be_silent = True
        elif notification_state == ChatNotificationState.MENTIONS:
            should_be_silent = not mentioned
        else:  # ChatNotificationState.ALL
            should_be_silent = False
        assert tg_msg.silent == should_be_silent


async def test_slave_message_notification_your_normal(helper, bot_group, slave, channel):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_group), patch.dict(channel.flag.config, your_message_on_slave="normal"):
        efb_msg = slave.send_text_message(chat=chat, substitution=False)
        tg_msg = await helper.wait_for_message(in_chats(bot_group) & regex(efb_msg.text))
        assert not tg_msg.silent


async def test_slave_message_notification_your_silent(helper, bot_group, slave, channel):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_group), patch.dict(channel.flag.config, your_message_on_slave="silent"):
        efb_msg = slave.send_text_message(chat=chat, substitution=False)
        tg_msg = await helper.wait_for_message(in_chats(bot_group) & regex(efb_msg.text))
        assert tg_msg.silent


async def test_slave_message_notification_your_mute(helper, bot_group, slave, channel):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_group), patch.dict(channel.flag.config, your_message_on_slave="mute"):
        efb_msg = slave.send_text_message(chat=chat, substitution=False)
        with raises(asyncio.TimeoutError):
            await helper.wait_for_message(in_chats(bot_group) & regex(efb_msg.text), timeout=3)
