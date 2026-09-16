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


async def test_main_confirmation_of_auxiliary_document_keeps_observer_file_ownership(
    channel, slave, client, bot_group, bot_id, monkeypatch,
):
    from functools import wraps
    from types import SimpleNamespace

    import httpx
    from telegram import Bot, Update
    from telegram.error import TimedOut
    from telegram.ext import TypeHandler

    from efb_telegram_master.bot_manager import QueuedDbLogContext
    from efb_telegram_master.message import ETMMsg
    from efb_telegram_master.msg_type import TGMsgType
    from efb_telegram_master.outbound import DeliveryUncertainError, HISTORY_SOURCE_PREFIX, QueueRequest
    from .helper.helper import wait_for_limiter_slot

    manager = channel.bot_manager
    pool = manager.bot_pool
    assert pool is not None and pool.bots
    auxiliary = next(bot for bot in pool.bots if not bot.disabled)
    aux_id = int(auxiliary.bot_id)
    await _ensure_users_in_group(client, bot_group, aux_id)
    deadline = time.monotonic() + 45
    while await asyncio.to_thread(auxiliary.check_membership_tri, bot_group) is not True:
        assert time.monotonic() < deadline, 'Auxiliary membership was not confirmed'
        await asyncio.sleep(0.25)

    chat = slave.chat_with_alias
    source = etm_utils.chat_id_to_str(chat=chat)
    pool.record_successful_auxiliary_send(source, aux_id)
    await wait_for_limiter_slot(lambda: auxiliary.peek_delay(bot_group))
    filename = 'confirm-owner-' + uuid4().hex + '.txt'
    contents = b'Main observer file ID; auxiliary author and replay sender.\n'
    accepted, observed, cleanup = [], [], []
    original_send = Bot.send_document

    @wraps(original_send)
    async def lose_response(bot, *args, **kwargs):
        result = await original_send(bot, *args, **kwargs)
        if kwargs.get('filename') == filename:
            accepted.append(result)
            try:
                raise httpx.ReadTimeout('CI response loss after acceptance')
            except httpx.ReadTimeout as cause:
                raise TimedOut('CI response loss') from cause
        return result

    async def observe(update, context):
        message = update.effective_message
        if message and message.text and message.text.startswith('/confirm_send'):
            reply = message.reply_to_message
            if reply and reply.document and reply.document.file_name == filename:
                observed.append(update)

    probe = TypeHandler(Update, observe)
    # Install on the running main application; normal /confirm_send still runs.
    async def install_probe():
        manager.application.add_handler(probe, group=-100)
    async def remove_probe():
        manager.application.remove_handler(probe, group=-100)
    await asyncio.to_thread(manager._runtime.call, install_probe())
    monkeypatch.setattr(Bot, 'send_document', lose_response)
    message = ETMMsg(uid=filename, text=filename, chat=chat, author=chat.other,
                     deliver_to=channel, type=MsgType.File, type_telegram=TGMsgType.Document)
    try:
        row_id, waiter = manager._enqueue_requests([
            QueueRequest('send_document', (int(bot_group), contents), {'filename': filename, '_slave_id': source})
        ], db_log_context=QueuedDbLogContext(message))
        with pytest.raises(DeliveryUncertainError):
            await asyncio.wait_for(asyncio.wrap_future(waiter), 60)
        assert len(accepted) == 1 and accepted[0].from_user.id == aux_id
        cleanup.append(accepted[0].message_id)
        command = await client.send_message(
            bot_group, f'/confirm_send@{manager.me.username} {row_id}', reply_to=accepted[0].message_id,
        )
        cleanup.append(command.id)
        deadline = time.monotonic() + 60
        stored = None
        while time.monotonic() < deadline:
            stored = channel.db.get_msg_log(master_msg_id=f'{bot_group}.{accepted[0].message_id}')
            if observed and stored is not None:
                break
            await asyncio.sleep(0.25)
        assert observed and stored is not None, 'Main update and durable confirmation were not observed'
        reply = observed[0].effective_message.reply_to_message
        assert observed[0].get_bot().id == bot_id
        assert reply.from_user.id == aux_id
        assert stored.file_id == reply.document.file_id
        assert stored.sender_bot_id == str(aux_id) and stored.file_bot_id == '__main__'
        file_meta = await asyncio.to_thread(manager.get_file, stored.file_id, sender_bot_id=stored.file_bot_id)
        downloaded = await asyncio.to_thread(manager._runtime.call, file_meta.download_as_bytearray())
        assert bytes(downloaded) == contents
        restored = stored.build_etm_msg(channel.chat_manager)
        assert restored.sender_bot_id == str(aux_id) and restored.file_bot_id == '__main__'
        # Editing still belongs to the auxiliary author.
        await asyncio.to_thread(manager.edit_message_caption, chat_id=bot_group,
                                message_id=accepted[0].message_id, caption=filename,
                                _sender_bot_id=stored.sender_bot_id)
        # Remove only the original remote message to force saved-file recovery.
        await client.delete_messages(bot_group, [accepted[0].message_id])
        operation, kwargs = channel.chat_binding._prepare_history_migration_call(
            SimpleNamespace(formatted_text=None, source_master_msg_id=stored.master_msg_id), bot_group, None,
        )
        pool.record_successful_auxiliary_send(HISTORY_SOURCE_PREFIX + source, aux_id)
        await wait_for_limiter_slot(lambda: auxiliary.peek_delay(bot_group))
        with patch.object(manager._bot, 'get_file', wraps=manager._bot.get_file) as main_fetch, \
                patch.object(auxiliary.bot, 'get_file', wraps=auxiliary.bot.get_file) as aux_fetch:
            waiter = manager.enqueue_history_operation(
                source_key=source, target_chat_id=bot_group, operation=operation, args=(), kwargs=kwargs,
                history_entry_ids=[],
            )
            result = await asyncio.wait_for(asyncio.wrap_future(waiter), 90)
            cleanup.append(result.message_id)
            main_fetch.assert_called_once_with(stored.file_id)
            aux_fetch.assert_not_called()
        remote = await client.get_messages(bot_group, ids=result.message_id)
        assert remote.sender_id == aux_id
        assert await client.download_media(remote, file=bytes) == contents
        current = channel.db.get_msg_log(master_msg_id=stored.master_msg_id)
        assert current is not None and (current.file_id, current.sender_bot_id, current.file_bot_id) == (
            stored.file_id, str(aux_id), '__main__',
        )
    finally:
        await asyncio.to_thread(manager._runtime.call, remove_probe())
        if cleanup:
            await client.delete_messages(bot_group, cleanup)
