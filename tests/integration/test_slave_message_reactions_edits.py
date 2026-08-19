from pytest import mark

from tests.integration.helper.filters import edited, in_chats, regex
from tests.integration.slave_message_factories import MessageFactory, all_message_factories
from tests.integration.utils import link_chats

pytestmark = mark.asyncio


@mark.parametrize("factory", all_message_factories(), ids=str)
async def test_slave_message_edits(helper, bot_group, slave, channel, factory: MessageFactory):
    chat = slave.group
    with link_chats(channel, (chat,), bot_group):
        message_ids = []
        efb_messages = [factory.send_message(slave, chat)]
        efb_message = efb_messages[0]
        try:
            telegram_message = await helper.wait_for_message(in_chats(bot_group))
            message_ids.append(telegram_message.id)
            factory.compare_message(telegram_message, efb_message)

            edited_efb_message = factory.edit_message(slave, efb_message)
            if edited_efb_message is not None:
                efb_messages.append(edited_efb_message)
                filters = in_chats(bot_group)
                if factory.content_editable:
                    filters &= edited(*message_ids)
                telegram_message = await helper.wait_for_message(filters)
                if not factory.content_editable:
                    message_ids.append(telegram_message.id)
                factory.compare_message(telegram_message, edited_efb_message)

            edited_media_efb_message = factory.edit_message_media(slave, efb_message)
            if edited_media_efb_message is not None:
                efb_messages.append(edited_media_efb_message)
                filters = in_chats(bot_group)
                if factory.media_editable:
                    filters &= edited(*message_ids)
                telegram_message = await helper.wait_for_message(filters)
                if factory.media_editable:
                    try:
                        factory.compare_message(telegram_message, edited_media_efb_message)
                    except AssertionError:
                        telegram_message = await helper.wait_for_message(filters)
                if not factory.media_editable:
                    message_ids.append(telegram_message.id)
                factory.compare_message(telegram_message, edited_media_efb_message)
        finally:
            for message in reversed(efb_messages):
                factory.finalize_message(message)


async def test_slave_message_reactions(helper, client, bot_group, slave, channel):
    chat = slave.group
    with link_chats(channel, (chat,), bot_group):
        efb_message = slave.send_text_message(chat=chat, reactions=True)
        telegram_message = await helper.wait_for_message(in_chats(bot_group) & regex(efb_message.text))
        reactions_status = slave.send_reactions_update(efb_message)
        telegram_message = await helper.wait_for_message(in_chats(bot_group) & edited(telegram_message.id))
        for reaction, members in reactions_status.reactions.items():
            assert reaction in telegram_message.text
