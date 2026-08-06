from pathlib import Path
from tempfile import NamedTemporaryFile

from ehforwarderbot.message import MsgType
from pytest import mark
from telegram.constants import FileSizeLimit

from tests.integration.helper.filters import in_chats, regex, reply_to
from tests.integration.utils import link_chats

pytestmark = mark.asyncio


async def test_slave_message_file_oversize_reports_bot_api_limit(helper, client, bot_group, slave, channel):
    chat = slave.chat_with_alias
    with link_chats(channel, (chat,), bot_group), NamedTemporaryFile(suffix=".bin") as file:
        file.truncate(FileSizeLimit.FILESIZE_UPLOAD + 1024 * 10)
        file.seek(0)
        efb_message = slave.send_file_like_message(
            MsgType.File,
            Path(file.name),
            mime="application/octet-stream",
            chat=chat,
            author=chat.other,
            commands=True,
        )

        notice_target = await helper.wait_for_message(in_chats(bot_group) & regex(efb_message.text))
        assert not notice_target.file
        await helper.wait_for_message(in_chats(bot_group) & reply_to(notice_target.id))
