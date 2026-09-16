"""Real /start ... true replay: grouped text, videos, and deleted-source file recovery."""

import asyncio
import time
from pathlib import Path
from uuid import uuid4

import pytest
from ehforwarderbot import MsgType

from efb_telegram_master import utils as etm_utils
from .helper.filters import in_chats, regex
from .utils import get_start_token, link_chats

pytestmark = pytest.mark.asyncio


async def wait_logged(channel, source, message):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        row = channel.db.get_msg_log(slave_msg_id=message.uid, slave_origin_uid=source)
        if row is not None:
            return row
        await asyncio.sleep(0.1)
    raise AssertionError(f'MsgLog never recorded source {message.uid}')


@pytest.mark.parametrize('remove_original_video', [False, True], ids=['copy-video', 'recover-deleted-video'])
async def test_relink_groups_text_around_a_real_video(
    channel, slave, client, helper, bot_id, bot_group, private_response, remove_original_video,
):
    chat = slave.chat_with_alias
    source = etm_utils.chat_id_to_str(chat=chat)
    prefix = 'relink-' + uuid4().hex[:10]
    labels = [f'{prefix} {letter}' for letter in 'ABCD']
    saved = []
    with link_chats(channel, (chat,), bot_group):
        for label in labels[:2]:
            message = await asyncio.to_thread(slave.send_text_message, chat, chat.other, text=label)
            await helper.wait_for_message(in_chats(bot_group) & regex(label))
            saved.append(await wait_logged(channel, source, message))
        video = await asyncio.to_thread(
            slave.send_file_like_message, MsgType.Video, Path('tests/mocks/video_0.mp4'),
            'video/mp4', chat, chat.other,
        )
        video_label = str(video.uid)
        original_video = await helper.wait_for_message(in_chats(bot_group) & regex(video_label))
        video_log = await wait_logged(channel, source, video)
        saved.append(video_log)
        assert video_log.media_type == 'Video' and video_log.file_id
        assert original_video.video is not None
        for label in labels[2:]:
            message = await asyncio.to_thread(slave.send_text_message, chat, chat.other, text=label)
            await helper.wait_for_message(in_chats(bot_group) & regex(label))
            saved.append(await wait_logged(channel, source, message))

        if remove_original_video:
            # The bot deletes only the test message it just sent. Its MsgLog
            # and cached Telegram file ID stay available for real recovery.
            await asyncio.to_thread(channel.bot_manager._bot.delete_message, bot_group, original_video.id)

        token = await get_start_token(client, helper, bot_id, chat.uid, private_response)
        command = await client.send_message(bot_group, f'/start {token} true')
        deadline = time.monotonic() + 180
        matches = []
        while time.monotonic() < deadline:
            recent = await client.get_messages(bot_group, limit=100)
            matches = sorted(
                (msg for msg in recent if msg.id > command.id and
                 (prefix in (msg.raw_text or '') or video_label in (msg.raw_text or ''))),
                key=lambda msg: msg.id,
            )
            if any(labels[3] in (msg.raw_text or '') for msg in matches):
                break
            await asyncio.sleep(0.5)

        assert len(matches) == 3, [(msg.id, msg.raw_text) for msg in matches]
        assert labels[0] in matches[0].raw_text and labels[1] in matches[0].raw_text
        assert matches[1].video is not None and video_label in matches[1].raw_text
        assert matches[1].document.id == original_video.document.id
        assert labels[2] in matches[2].raw_text and labels[3] in matches[2].raw_text
        for row in saved:
            current = channel.db.get_msg_log(master_msg_id=row.master_msg_id)
            assert current is not None
            assert (current.slave_message_id, current.text, current.file_id) == (
                row.slave_message_id, row.text, row.file_id,
            )
