from pytest import mark

from tests.integration.helper.filter_chats import in_chats
from tests.integration.helper.filter_content import regex
from tests.integration.utils import link_chats

pytestmark = mark.asyncio


async def test_slave_message_name(helper, client, bot_group, slave, channel):
    chat = slave.group
    member = chat.members[2]
    assert "&" in member.name
    with link_chats(channel, (chat,), bot_group):
        efb_msg = slave.send_text_message(chat=chat, author=member)
        tg_msg = await helper.wait_for_message(in_chats(bot_group) & regex(efb_msg.text))
        assert member.name in tg_msg.raw_text
