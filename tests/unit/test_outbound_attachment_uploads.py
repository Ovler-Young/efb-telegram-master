import io
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram import InputFile, InputMediaVideo
from telegram.constants import MessageLimit
from telegram.error import BadRequest, RetryAfter

from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.outbound_types import QueuedCall, QueueRequest, SenderSelection, UploadCleanup, rewind_uploads
from efb_telegram_master.transport.telegram_calls import TelegramCallAdapter
from tests.support.outbound_queue import _queue


def _execute_adapter_call(adapter, call, selection):
    primary = adapter.execute_primary(call, selection)
    if primary.attachment is not None:
        adapter.execute_attachment(primary.attachment, selection)
    adapter.record_successful_send(call, selection)
    return primary.receipt


def test_queue_rewinds_file_like_upload_before_retry_after():
    upload = io.BytesIO(b"image-bytes")
    received_uploads = []

    class Sender:
        def send_document(self, *, chat_id, document):
            received_uploads.append(document.read())
            if len(received_uploads) == 1:
                raise RetryAfter(0)
            return SimpleNamespace(message_id=7)

    queue = _queue(Sender(), worker_count=1)
    try:
        receipt = queue.enqueue(QueueRequest("send_document", (), {"chat_id": 1, "document": upload}, 1)).result(1)
        assert receipt.message.message_id == 7
        assert received_uploads == [b"image-bytes", b"image-bytes"]
    finally:
        queue.stop()


def test_queue_rewinds_nested_media_uploads_before_retry_after():
    media_source = io.BytesIO(b"media-bytes")
    thumbnail_source = io.BytesIO(b"thumbnail-bytes")
    media = InputMediaVideo(
        InputFile(media_source, read_file_handle=False),
        thumbnail=InputFile(thumbnail_source, read_file_handle=False),
    )
    received_uploads = []

    class Sender:
        def send_media_group(self, *, chat_id, media):
            received_uploads.append((media[0].media.input_file_content.read(), media[0].thumbnail.input_file_content.read()))
            if len(received_uploads) == 1:
                raise RetryAfter(0)
            return [SimpleNamespace(message_id=7)]

    queue = _queue(Sender(), worker_count=1)
    try:
        queue.enqueue(QueueRequest("send_media_group", (), {"chat_id": 1, "media": [media]}, 1)).result(1)
        assert received_uploads == [(b"media-bytes", b"thumbnail-bytes"), (b"media-bytes", b"thumbnail-bytes")]
    finally:
        queue.stop()


def test_queue_records_retry_after_and_cleans_unrewindable_upload(tmp_path) -> None:
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")
    available_sent = threading.Event()
    retrying_calls = []

    class UnrewindableUpload:
        def seek(self, _offset):
            raise OSError("rewind failed")

    class RetryingSender:
        def send_document(self, *, chat_id, document):
            retrying_calls.append((chat_id, document))
            raise RetryAfter(60)

    class AvailableSender:
        def send_message(self, *, chat_id, text):
            available_sent.set()
            return SimpleNamespace(message_id=8)

    retrying_auxiliary = Mock(bot_id=10, disabled=False, check_membership_tri=Mock(return_value=True), peek_delay=Mock(return_value=0.0), try_acquire_limits=Mock(return_value=True))
    retrying_auxiliary.bot = RetryingSender()
    available_auxiliary = Mock(bot_id=20, disabled=False, check_membership_tri=Mock(return_value=True), peek_delay=Mock(return_value=0.0), try_acquire_limits=Mock(return_value=True))
    available_auxiliary.bot = AvailableSender()
    queue = _queue(SimpleNamespace(), worker_count=1, bot_pool=BotPool([retrying_auxiliary, available_auxiliary]))
    try:
        waiter = queue.enqueue(
            QueueRequest(
                "send_document",
                (),
                {"chat_id": 1, "document": UnrewindableUpload()},
                1,
                required_sender_bot_id="10",
                cleanup=UploadCleanup((str(upload),)),
            )
        )
        with pytest.raises(OSError, match="rewind failed"):
            waiter.result(1)
        assert queue.cooldown_snapshot()["auxiliary"] > 0.0
        retrying_waiter = queue.enqueue(QueueRequest("send_document", (), {"chat_id": 1, "document": UnrewindableUpload()}, 1, required_sender_bot_id="10"))
        available_waiter = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 2, "text": "sent"}, 2, required_sender_bot_id="20"))
        assert available_sent.wait(1)
        receipt = available_waiter.result(1)
        assert receipt.message.message_id == 8
        assert not retrying_waiter.done()
        assert len(retrying_calls) == 1
        assert not upload.exists()
    finally:
        queue.stop()


@pytest.mark.parametrize(
    ("operation", "args", "kwargs", "content_key", "content_index", "limit"),
    [
        ("send_message", (1,), {}, "text", 1, MessageLimit.MAX_TEXT_LENGTH),
        ("send_photo", (1, "photo"), {}, "caption", 2, MessageLimit.CAPTION_LENGTH),
    ],
)
def test_adapter_sends_oversize_text_and_caption_as_attachment(operation, args, kwargs, content_key, content_index, limit):
    full_content = "x" * int(limit)
    sender = Mock()
    getattr(sender, operation).return_value = SimpleNamespace(message_id=7)
    call = QueuedCall(operation, args, {**kwargs, content_key: full_content}, 1, None, None)

    receipt = TelegramCallAdapter(None).execute(call, SenderSelection(sender, None))

    assert receipt.message.message_id == 7
    delivered = getattr(sender, operation).call_args.kwargs[content_key]
    assert delivered == full_content[:100] + "\n...\n" + full_content[-100:]
    attachment = sender.send_document.call_args.args[1]
    assert attachment.getvalue() == full_content.encode()


@pytest.mark.parametrize(
    ("operation", "args", "kwargs"),
    [
        ("send_message", (1,), {"text": "<broken>"}),
        ("send_photo", (1, "photo"), {"caption": "<broken>"}),
    ],
)
def test_adapter_retries_entity_parse_failure_without_parse_mode(operation, args, kwargs):
    sender = Mock()
    getattr(sender, operation).side_effect = [BadRequest("Can't parse entities"), SimpleNamespace(message_id=7)]

    _execute_adapter_call(TelegramCallAdapter(None), QueuedCall(operation, args, {**kwargs, "parse_mode": "HTML"}, 1, None, None), SenderSelection(sender, None))

    calls = getattr(sender, operation).call_args_list
    assert calls[0].kwargs["parse_mode"] == "HTML"
    assert calls[1].kwargs == kwargs


@pytest.mark.parametrize(
    ("operation", "args", "kwargs", "content_key", "content_index", "limit"),
    [
        ("edit_message_text", ("body", 1, 2), {}, "text", 0, MessageLimit.MAX_TEXT_LENGTH),
        ("edit_message_text", (), {"chat_id": 1, "message_id": 2, "text": "body"}, "text", 0, MessageLimit.MAX_TEXT_LENGTH),
        ("edit_message_caption", (1, 2, "inline-id", "body"), {}, "caption", 3, MessageLimit.CAPTION_LENGTH),
        ("edit_message_caption", (), {"chat_id": 1, "message_id": 2, "caption": "body"}, "caption", 3, MessageLimit.CAPTION_LENGTH),
    ],
)
def test_adapter_edit_overflow_attaches_the_prepared_content(operation, args, kwargs, content_key, content_index, limit):
    full_content = "Prefix\n" + "x" * int(limit) + "\nSuffix"
    prepared_args = (*args[:content_index], full_content, *args[content_index + 1 :]) if args else ()
    prepared_kwargs = {**kwargs, content_key: full_content} if not args else kwargs
    sender = Mock()
    getattr(sender, operation).return_value = SimpleNamespace(message_id=7)

    _execute_adapter_call(TelegramCallAdapter(None), QueuedCall(operation, prepared_args, prepared_kwargs, 1, None, None), SenderSelection(sender, None))

    call = getattr(sender, operation).call_args
    delivered = call.args[content_index] if prepared_args else call.kwargs[content_key]
    assert delivered == full_content[:100] + "\n...\n" + full_content[-100:]
    assert sender.send_document.call_args.args[1].getvalue() == full_content.encode()


@pytest.mark.parametrize(
    ("operation", "args", "kwargs", "content_key", "content_index"),
    [
        ("edit_message_text", ("<broken>", 1, 2), {}, "text", 0),
        ("edit_message_caption", (1, 2, "inline-id", "<broken>"), {}, "caption", 3),
    ],
)
def test_adapter_edit_retries_entity_parse_failure_without_parse_mode(operation, args, kwargs, content_key, content_index):
    sender = Mock()
    getattr(sender, operation).side_effect = [BadRequest("Can't parse entities"), SimpleNamespace(message_id=7)]

    _execute_adapter_call(TelegramCallAdapter(None), QueuedCall(operation, args, {**kwargs, "parse_mode": "HTML"}, 1, None, None), SenderSelection(sender, None))

    calls = getattr(sender, operation).call_args_list
    assert len(calls) == 2
    assert calls[0].args[content_index] == args[content_index]
    assert calls[0].kwargs["parse_mode"] == "HTML"
    assert calls[1].args[content_index] == args[content_index]
    assert "parse_mode" not in calls[1].kwargs


def test_adapter_rewinds_files_before_retrying_entity_parse_failure() -> None:
    photo = io.BytesIO(b"photo")
    positions = []

    class Sender:
        def send_photo(self, _chat_id, source, *, caption, parse_mode=None):
            positions.append(source.tell())
            source.seek(3)
            if len(positions) == 1:
                raise BadRequest("Can't parse entities")
            return SimpleNamespace(message_id=7)

    _execute_adapter_call(
        TelegramCallAdapter(None),
        QueuedCall("send_photo", (1, photo), {"caption": "caption", "parse_mode": "HTML"}, 1, None, None),
        SenderSelection(Sender(), None),
    )

    assert positions == [0, 0]


def test_rewind_uploads_recursively_rewinds_input_media_and_nested_containers() -> None:
    media_source = io.BytesIO(b"media")
    thumbnail_source = io.BytesIO(b"thumbnail")
    nested_source = io.BytesIO(b"nested")
    media = InputMediaVideo(
        InputFile(media_source, read_file_handle=False),
        thumbnail=InputFile(thumbnail_source, read_file_handle=False),
    )
    for source in (media_source, thumbnail_source, nested_source):
        source.seek(1)

    rewind_uploads(({"media": [media, {"nested": nested_source}]},), {})

    assert [source.tell() for source in (media_source, thumbnail_source, nested_source)] == [0, 0, 0]


def test_adapter_aborts_parse_retry_when_upload_cannot_rewind() -> None:
    class UnrewindableUpload:
        def seek(self, _offset):
            raise OSError("rewind failed")

    sender = Mock()
    sender.send_photo.side_effect = BadRequest("Can't parse entities")

    with pytest.raises(OSError, match="rewind failed"):
        _execute_adapter_call(
            TelegramCallAdapter(None),
            QueuedCall("send_photo", (1, UnrewindableUpload()), {"caption": "caption", "parse_mode": "HTML"}, 1, None, None),
            SenderSelection(sender, None),
        )

    assert sender.send_photo.call_count == 1
