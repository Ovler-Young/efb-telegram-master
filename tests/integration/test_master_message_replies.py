from ehforwarderbot.chat import SelfChatMember
from pytest import mark

from .master_message_factories import MessageFactory, all_message_factories, run_telegram_operation
from .utils import link_chats

pytestmark = mark.asyncio


@mark.parametrize("factory", all_message_factories(), ids=str)
async def test_master_message_replies(helper, client, bot_group, slave, channel, factory: MessageFactory):
    chat = slave.chat_without_alias

    with link_chats(channel, (chat,), bot_group):
        tg_msg = await run_telegram_operation(factory, "initial send", factory.send_message(client, bot_group))
        efb_msg = slave.messages.get(timeout=5)
        assert efb_msg.chat == chat
        assert isinstance(efb_msg.author, SelfChatMember)
        assert efb_msg.deliver_to is slave
        assert not efb_msg.edit
        assert not efb_msg.edit_media
        factory.compare_message(tg_msg, efb_msg)
        await factory.finalize_message(tg_msg, efb_msg)

        if factory.test_quote:
            quoted_message = await run_telegram_operation(factory, "quote reply send", factory.send_message(client, bot_group, target=tg_msg))
            quoted_efb_msg = slave.messages.get(timeout=5)
            assert quoted_efb_msg.chat == chat
            assert isinstance(quoted_efb_msg.author, SelfChatMember)
            assert quoted_efb_msg.deliver_to is slave
            assert not quoted_efb_msg.edit
            assert not quoted_efb_msg.edit_media
            assert quoted_efb_msg.target.uid == efb_msg.uid
            await factory.finalize_message(quoted_message, quoted_efb_msg)
