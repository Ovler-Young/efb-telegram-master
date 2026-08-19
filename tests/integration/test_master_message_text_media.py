from ehforwarderbot.chat import SelfChatMember
from pytest import mark

from .master_message_factories import MessageFactory, all_message_factories, run_telegram_operation
from .utils import link_chats

pytestmark = mark.asyncio


@mark.parametrize("factory", all_message_factories(), ids=str)
async def test_master_message_text_and_media(helper, client, bot_group, slave, channel, factory: MessageFactory):
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

        edited_msg = await run_telegram_operation(factory, "text edit", factory.edit_message(client, tg_msg))
        if edited_msg is not None:
            efb_msg = slave.messages.get(timeout=5)
            assert efb_msg.chat == chat
            assert isinstance(efb_msg.author, SelfChatMember)
            assert efb_msg.deliver_to is slave
            assert efb_msg.edit
            assert not efb_msg.edit_media
            factory.compare_message(edited_msg, efb_msg)
            await factory.finalize_message(edited_msg, efb_msg)

        media_edited = await run_telegram_operation(factory, "media edit", factory.edit_message_media(client, tg_msg))
        if media_edited is not None:
            efb_msg = slave.messages.get(timeout=5)
            assert efb_msg.chat == chat
            assert isinstance(efb_msg.author, SelfChatMember)
            assert efb_msg.deliver_to is slave
            assert efb_msg.edit
            assert efb_msg.edit_media
            factory.compare_message(media_edited, efb_msg)
            await factory.finalize_message(media_edited, efb_msg)
