import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram import InputFile, InputMediaDocument

from efb_telegram_master.bot_manager import TelegramBotManager
from efb_telegram_master.outbound import OutboundQueue, QueueRequest, SenderSelection
from efb_telegram_master.transfer_timeout import media_size, parse_transfer_min_mbps, transfer_timeout


def test_transfer_budget_uses_configured_megabits_and_preserves_disabled_timeouts():
    size = 2 * 1024 ** 3
    assert transfer_timeout(120, size, 2.5) == pytest.approx(6991.9476736)
    assert transfer_timeout(120, size, None) == 120
    assert transfer_timeout(None, size, 2.5) is None
    assert transfer_timeout(120, None, 2.5) == 120


def test_transfer_rate_requires_positive_finite_number():
    assert parse_transfer_min_mbps(2.5) == 2.5
    assert parse_transfer_min_mbps(None) is None
    for invalid in (True, 0, -1, float('nan'), float('inf'), '2.5'):
        with pytest.raises(ValueError, match='transfer_min_mbps'):
            parse_transfer_min_mbps(invalid)


def test_album_size_includes_streams_and_thumbnail_without_consuming_them(tmp_path):
    attachment = tmp_path / 'document.bin'
    attachment.write_bytes(b'12345')
    stream = io.BytesIO(b'1234567')
    stream.seek(3)
    album = [InputMediaDocument(InputFile(stream, read_file_handle=False), thumbnail=b'123'),
             InputMediaDocument(attachment)]
    assert media_size(album) == 15
    assert stream.tell() == 3


def test_queued_upload_and_retry_budget_use_the_same_media_size(tmp_path):
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager._transfer_min_mbps = 2.5
    manager._request_kwargs = {'read_timeout': 15, 'media_write_timeout': 20}
    queue = OutboundQueue(tmp_path, transfer_min_mbps=2.5)
    manager._outbound_queue = queue
    sender = SimpleNamespace(send_document=Mock(return_value='sent'))
    selection = SenderSelection(sender=sender, sender_bot_id=None)
    document = b'x' * 312500
    row_id, _ = queue.enqueue_many(
        [QueueRequest('send_document', (), {'chat_id': 1, 'document': document})],
        lambda _: lambda chat_id, document: None,
    )
    row = queue.heads()[0]
    assert row.id == row_id
    assert queue.blocking_timeout(row.payload) == 301
    args, kwargs = queue.decode_payload(row.payload)
    try:
        assert manager.execute_queued_call(row, args, kwargs, selection) == 'sent'
        sent = sender.send_document.call_args.kwargs
        assert sent['read_timeout'] == 16
        assert sent['write_timeout'] == 21
    finally:
        queue.close_payload_resources(queue.payload_closeables(args, kwargs))
        queue.close()


@pytest.mark.parametrize('owner_id', [None, 'aux-7'])
def test_incoming_attachment_budget_covers_get_file_and_download(owner_id, monkeypatch):
    import asyncio

    from efb_telegram_master import message as message_module

    size = 2 * 1024 ** 3
    transfer_seconds = size * 8 / (2.5 * 1_000_000)

    async def download_to_memory(out, *, read_timeout):
        if read_timeout < transfer_seconds:
            raise TimeoutError('File download exceeded its read budget')
        out.write(b'downloaded attachment')

    def get_file(file_id, *, read_timeout=15):
        assert file_id == 'owned-file'
        if read_timeout < transfer_seconds:
            raise TimeoutError('Server-side getFile transfer exceeded its read budget')
        return SimpleNamespace(file_path='media.txt', file_size=size,
                               download_to_memory=download_to_memory)

    def run_download(coroutine, *, timeout):
        assert timeout == pytest.approx(120 + transfer_seconds)
        return asyncio.run(coroutine)

    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager._transfer_min_mbps = 2.5
    manager._request_kwargs = {'read_timeout': 15}
    manager._runtime = SimpleNamespace(call=run_download)
    sender = SimpleNamespace(get_file=get_file)
    manager._bot = sender if owner_id is None else SimpleNamespace(
        get_file=Mock(side_effect=AssertionError('Main bot does not own this file')))
    manager._is_main_sender_id = lambda bot_id: False
    manager.bot_pool = SimpleNamespace(get_bot_by_id=lambda bot_id: SimpleNamespace(
        bot=sender, disabled=False) if bot_id == owner_id else None)
    monkeypatch.setattr(message_module, 'coordinator', SimpleNamespace(
        master=SimpleNamespace(bot_manager=manager)))
    message = message_module.ETMMsg()
    message.sender_bot_id = owner_id
    message.put_telegram_file(SimpleNamespace(document=SimpleNamespace(
        file_id='owned-file', file_unique_id='unique', file_size=size, mime_type='text/plain')))
    downloaded = message.get_file()
    try:
        assert downloaded.read() == b'downloaded attachment'
    finally:
        downloaded.close()
