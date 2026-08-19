from pytest import mark

from tests.integration.helper.filter_chats import in_chats
from tests.integration.slave_message_factory_base import MessageFactory
from tests.integration.slave_message_media_factories import all_message_factories
from tests.integration.utils import link_chats

pytestmark = mark.asyncio


@mark.parametrize("factory", all_message_factories(), ids=str)
async def test_slave_message_text_and_media(helper, bot_group, slave, channel, factory: MessageFactory):
    chat = slave.group
    with link_chats(channel, (chat,), bot_group):
        efb_message = factory.send_message(slave, chat)
        try:
            telegram_message = await helper.wait_for_message(in_chats(bot_group))
            factory.compare_message(telegram_message, efb_message)
        finally:
            factory.finalize_message(efb_message)
