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
