from ehforwarderbot.message import StatusAttribute
from pytest import mark
from telethon.events import UserUpdate
from telethon.tl.types import SendMessageRecordAudioAction, SendMessageTypingAction, SendMessageUploadDocumentAction, SendMessageUploadPhotoAction, SendMessageUploadVideoAction

from tests.integration.helper.filter_chats import in_chats
from tests.integration.helper.filter_messages import reply_to
from tests.integration.helper.filter_updates import typing
from tests.integration.slave_message_factory_base import MessageFactory
from tests.integration.slave_message_media_factories import all_message_factories
from tests.integration.utils import link_chats

pytestmark = mark.asyncio


@mark.parametrize("factory", all_message_factories(), ids=str)
async def test_slave_message_reply_delivery(helper, bot_group, slave, channel, factory: MessageFactory):
    chat = slave.group
    with link_chats(channel, (chat,), bot_group):
        efb_messages = [factory.send_message(slave, chat)]
        efb_message = efb_messages[0]
        try:
            telegram_message = await helper.wait_for_message(in_chats(bot_group))
            factory.compare_message(telegram_message, efb_message)
            targeted_message = factory.send_message(slave, chat, target=efb_message)
            efb_messages.append(targeted_message)
            targeted_telegram_message = await helper.wait_for_message(in_chats(bot_group) & reply_to(telegram_message.id))
            factory.compare_message(targeted_telegram_message, targeted_message)
        finally:
            for message in reversed(efb_messages):
                factory.finalize_message(message)


@mark.parametrize(
    "efb_status,tg_status",
    [
        (StatusAttribute.Types.TYPING, SendMessageTypingAction),
        (StatusAttribute.Types.UPLOADING_VOICE, SendMessageRecordAudioAction),
        (StatusAttribute.Types.UPLOADING_IMAGE, SendMessageUploadPhotoAction),
        (StatusAttribute.Types.UPLOADING_VIDEO, SendMessageUploadVideoAction),
        (StatusAttribute.Types.UPLOADING_FILE, SendMessageUploadDocumentAction),
    ],
)
async def test_slave_message_statuses(helper, bot_id, slave, efb_status, tg_status):
    chat = slave.chat_with_alias
    slave.send_status_message(StatusAttribute(efb_status), chat)
    event = await helper.wait_for_event(in_chats(bot_id) & typing)
    assert isinstance(event, UserUpdate.Event)
    assert isinstance(event.action, tg_status)
