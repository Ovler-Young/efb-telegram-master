"""Live relink fetches media with its owner and sends through ordinary selection."""

import asyncio
import time
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from PIL import Image, ImageChops, ImageStat

import pytest
from ehforwarderbot import MsgType

from efb_telegram_master import utils as etm_utils
from .test_backfill_history import _ensure_users_in_group
from .test_relink_history_media import wait_logged
from .utils import get_start_token, link_chats

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope='module')
def channel(channel_with_auxiliary_bots):
    return channel_with_auxiliary_bots


@pytest.fixture(scope='module')
def slave(slave_with_auxiliary_bots):
    return slave_with_auxiliary_bots


async def test_relink_owner_fetch_is_independent_of_sending_and_text_batching(
    channel, slave, client, helper, bot_id, bot_group, private_response,
):
    manager = channel.bot_manager
    pool = manager.bot_pool
    assert pool is not None and pool.bots, 'Real auxiliary credentials are required for sender ownership acceptance.'
    auxiliary = next(bot for bot in pool.bots if not bot.disabled)
    aux_id = int(auxiliary.bot_id)
    await _ensure_users_in_group(client, bot_group, aux_id)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if await asyncio.to_thread(auxiliary.check_membership_tri, bot_group) is True:
            break
        await asyncio.sleep(0.25)
    else:
        raise AssertionError('Auxiliary bot did not become a confirmed member of the test group.')

    chat = slave.chat_with_alias
    source = etm_utils.chat_id_to_str(chat=chat)
    prefix = 'sender-' + uuid4().hex[:10]
    labels = [f'{prefix} {letter}' for letter in 'ABCDEFGH']
    saved = []
    media_labels = []
    original_media = []

    def prefer(owner):
        # Seed original messages with different senders through real affinity.
        # Historical identity must not override the ordinary replay selection.
        for bot in pool.bots:
            pool.remove_failed_membership_affinity(source, bot.bot_id)
        if owner == aux_id:
            pool.record_successful_auxiliary_send(source, aux_id)

    async def text_pair(start, owner):
        prefer(owner)
        for label in labels[start:start + 2]:
            message = await asyncio.to_thread(slave.send_text_message, chat, chat.other, text=label)
            row = await wait_logged(channel, source, message)
            assert (int(row.sender_bot_id) if row.sender_bot_id else bot_id) == owner
            saved.append(row)

    async def media(kind, path, mime):
        prefer(aux_id)
        message = await asyncio.to_thread(slave.send_file_like_message, kind, Path(path), mime, chat, chat.other)
        row = await wait_logged(channel, source, message)
        assert row.sender_bot_id == str(aux_id) and row.file_id
        source_chat, source_id = etm_utils.message_id_str_to_id(row.master_msg_id)
        received = await client.get_messages(source_chat, ids=source_id)
        assert received.sender_id == aux_id
        saved.append(row)
        media_labels.append(str(message.uid))
        original_media.append(await client.download_media(received, file=bytes))
        # Force real saved-file-ID recovery: original source copy cannot work.
        await asyncio.to_thread(auxiliary.bot.delete_message, source_chat, source_id)

    with link_chats(channel, (chat,), bot_group):
        await text_pair(0, bot_id)
        await text_pair(2, aux_id)
        await media(MsgType.Image, 'tests/mocks/image.png', 'image/png')
        await text_pair(4, bot_id)
        await media(MsgType.Video, 'tests/mocks/video_0.mp4', 'video/mp4')
        await text_pair(6, aux_id)
        prefer(bot_id)  # The currently preferred/available bot is NOT the owner of most history.

        token = await get_start_token(client, helper, bot_id, chat.uid, private_response)
        with patch.object(auxiliary.bot, 'get_file', wraps=auxiliary.bot.get_file) as owner_fetch, \
                patch.object(manager._bot, 'get_file', wraps=manager._bot.get_file) as main_fetch:
            command = await client.send_message(bot_group, f'/start {token} true')
            deadline = time.monotonic() + 240
            matches = []
            while time.monotonic() < deadline:
                recent = await client.get_messages(bot_group, limit=100)
                matches = sorted((msg for msg in recent if msg.id > command.id and (
                    prefix in (msg.raw_text or '') or any(label in (msg.raw_text or '') for label in media_labels)
                )), key=lambda msg: msg.id)
                if any(labels[-1] in (msg.raw_text or '') for msg in matches):
                    break
                await asyncio.sleep(0.5)
            assert {call.args[0] for call in owner_fetch.call_args_list} == {row.file_id for row in saved if row.file_id}
            main_fetch.assert_not_called()

        # Four consecutive texts from two original bots form ONE batch.
        # Main is the normal sender here; auxiliary owns only the saved files.
        assert len(matches) == 5, [(msg.id, msg.sender_id, msg.raw_text) for msg in matches]
        assert all(msg.sender_id == bot_id for msg in matches)
        assert all(label in matches[0].raw_text for label in labels[:4])
        for output, start in ((2, 4), (4, 6)):
            assert labels[start] in matches[output].raw_text and labels[start + 1] in matches[output].raw_text
        assert matches[1].photo is not None and matches[3].video is not None
        photo_bytes = await client.download_media(matches[1], file=bytes)
        with Image.open(BytesIO(original_media[0])) as original, Image.open(BytesIO(photo_bytes)) as restored:
            assert original.size == restored.size
            # Telegram can recompress a photo on upload; compare decoded content.
            difference = ImageChops.difference(original.convert('RGB'), restored.convert('RGB'))
            assert max(ImageStat.Stat(difference).mean) < 5
        assert await client.download_media(matches[3], file=bytes) == original_media[1]
        for row in saved:
            current = channel.db.get_msg_log(master_msg_id=row.master_msg_id)
            assert current is not None
            assert (current.slave_message_id, current.text, current.file_id, current.sender_bot_id) == (
                row.slave_message_id, row.text, row.file_id, row.sender_bot_id,
            )
