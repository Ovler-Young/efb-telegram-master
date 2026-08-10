import io
import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from telegram import InputFile, InputMediaVideo
from telegram.constants import MessageLimit
from telegram.error import BadRequest, RetryAfter

from efb_telegram_master.auxiliary_bot import AuxiliaryBot
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.outbound import (
    OutboundQueue,
    QueuedCall,
    QueueRequest,
    SenderSelection,
    TelegramCallAdapter,
    UploadCleanup,
    retry_after_seconds,
    rewind_uploads,
)


class _Limiter:
    def peek_delay(self, _chat_id):
        return 0.0

    def try_acquire(self, _chat_id):
        return True

    def occupancy_snapshot(self):
        return {"global": 0.0, "chat": 0.0}


def _queue(sender, worker_count=2):
    queue = OutboundQueue(
        sender,
        None,
        _Limiter(),
        worker_count=worker_count,
        blocking_timeout=1,
        shutdown_drain_timeout=1,
        shutdown_join_grace=0.1,
    )
    queue.start()
    return queue


def test_queue_serializes_same_chat_calls():
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Sender:
        def send_message(self, *, chat_id, text):
            calls.append(text)
            if text == "first":
                started.set()
                assert release.wait(1)
            return text

    queue = _queue(Sender())
    try:
        first = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "first"}, 1))
        second = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "second"}, 1))
        assert started.wait(1)
        assert calls == ["first"]
        release.set()
        assert first.result(1).message == "first"
        assert second.result(1).message == "second"
        assert calls == ["first", "second"]
    finally:
        queue.stop()


@pytest.mark.parametrize("retry_after", [0, timedelta(0)])
def test_queue_retries_numeric_and_timedelta_retry_after(retry_after):
    attempts = 0

    class Sender:
        def send_message(self, *, chat_id, text):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RetryAfter(retry_after)
            return text

    queue = _queue(Sender(), worker_count=1)
    try:
        waiter = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "sent"}, 1))
        assert waiter.result(1).message == "sent"
        assert attempts == 2
    finally:
        queue.stop()


@pytest.mark.parametrize(("value", "seconds"), [(3, 3.0), (timedelta(seconds=4), 4.0)])
def test_retry_after_seconds_accepts_numeric_and_timedelta_values(value, seconds):
    assert retry_after_seconds(RetryAfter(value)) == seconds


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

    TelegramCallAdapter(None).execute(QueuedCall(operation, args, {**kwargs, "parse_mode": "HTML"}, 1, None, None), SenderSelection(sender, None))

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

    TelegramCallAdapter(None).execute(QueuedCall(operation, prepared_args, prepared_kwargs, 1, None, None), SenderSelection(sender, None))

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

    TelegramCallAdapter(None).execute(QueuedCall(operation, args, {**kwargs, "parse_mode": "HTML"}, 1, None, None), SenderSelection(sender, None))

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

    TelegramCallAdapter(None).execute(
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
        TelegramCallAdapter(None).execute(
            QueuedCall("send_photo", (1, UnrewindableUpload()), {"caption": "caption", "parse_mode": "HTML"}, 1, None, None),
            SenderSelection(sender, None),
        )

    assert sender.send_photo.call_count == 1


def test_queue_cleans_owned_upload_after_terminal_failure(tmp_path) -> None:
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")

    class Sender:
        def send_document(self, *, chat_id, document):
            raise ValueError(f"cannot send {document} to {chat_id}")

    queue = _queue(Sender(), worker_count=1)
    try:
        waiter = queue.enqueue(QueueRequest("send_document", (), {"chat_id": 1, "document": upload.as_uri()}, 1, cleanup=UploadCleanup((str(upload),))))
        with pytest.raises(ValueError, match="cannot send"):
            waiter.result(1)
        assert not upload.exists()
    finally:
        queue.stop()


def test_queue_preserves_owned_upload_through_retry_after(tmp_path) -> None:
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")
    first_attempt = threading.Event()
    attempts = 0

    class Sender:
        def send_document(self, *, chat_id, document):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                first_attempt.set()
                raise RetryAfter(0.1)
            return document

    queue = _queue(Sender(), worker_count=1)
    try:
        waiter = queue.enqueue(QueueRequest("send_document", (), {"chat_id": 1, "document": upload.as_uri()}, 1, cleanup=UploadCleanup((str(upload),))))
        assert first_attempt.wait(1)
        assert upload.exists()
        assert waiter.result(1).message == upload.as_uri()
        assert not upload.exists()
    finally:
        queue.stop()


def test_queue_preserves_owned_upload_after_caller_timeout(tmp_path) -> None:
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")
    started = threading.Event()
    release = threading.Event()

    class Sender:
        def send_document(self, *, chat_id, document):
            started.set()
            assert release.wait(1)
            return document

    queue = OutboundQueue(Sender(), None, _Limiter(), worker_count=1, blocking_timeout=0.01, shutdown_drain_timeout=1, shutdown_join_grace=0.1)
    queue.start()
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            queue.enqueue_and_wait(QueueRequest("send_document", (), {"chat_id": 1, "document": upload.as_uri()}, 1, cleanup=UploadCleanup((str(upload),))))
        assert started.wait(1)
        assert upload.exists()
        release.set()
        deadline = time.monotonic() + 1
        while upload.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not upload.exists()
    finally:
        release.set()
        queue.stop()


def test_queue_cleans_owned_upload_when_shutdown_cancels_pending_call(tmp_path) -> None:
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")
    queue = OutboundQueue(Mock(), None, _Limiter(), worker_count=1, blocking_timeout=1, shutdown_drain_timeout=1, shutdown_join_grace=0.1)

    waiter = queue.enqueue(QueueRequest("send_document", (), {"chat_id": 1, "document": upload.as_uri()}, 1, cleanup=UploadCleanup((str(upload),))))
    queue.stop()

    with pytest.raises(Exception, match="Outbound queue stopped"):
        waiter.result()
    assert not upload.exists()


def test_queue_propagates_terminal_sender_failure() -> None:
    class Sender:
        def send_message(self, *, chat_id, text):
            raise ValueError(f"cannot send {text} to {chat_id}")

    queue = _queue(Sender(), worker_count=1)
    try:
        waiter = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "body"}, 1))
        with pytest.raises(ValueError, match="cannot send body to 1"):
            waiter.result(1)
    finally:
        queue.stop()


def test_queue_failure_rechecks_cached_member_and_clears_only_the_triggering_affinity() -> None:
    probe_started = threading.Event()
    release_probe = threading.Event()

    def get_chat_member(_chat_id: int, _bot_id: int) -> SimpleNamespace:
        probe_started.set()
        assert release_probe.wait(1)
        return SimpleNamespace(status="left")

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        auxiliary = AuxiliaryBot("123:token")
    auxiliary.bot_id = 10
    auxiliary.async_bot.get_chat_member.side_effect = get_chat_member
    auxiliary.bot.send_message.side_effect = ValueError("publication failed")
    auxiliary.update_membership(1, True)

    replacement = Mock()
    replacement.bot_id = 20
    replacement.disabled = False
    pool = BotPool([auxiliary, replacement])
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)
    pool.record_successful_auxiliary_send("slave-new", 20)
    queue = OutboundQueue(
        Mock(),
        pool,
        _Limiter(),
        worker_count=1,
        blocking_timeout=1,
        shutdown_drain_timeout=1,
        shutdown_join_grace=0.1,
    )
    queue.start()
    try:
        waiter = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "body"}, 1, slave_id="slave-a", required_sender_bot_id="10"))
        with pytest.raises(ValueError, match="publication failed"):
            waiter.result(1)
        assert probe_started.wait(0.2)
        release_probe.set()

        deadline = time.monotonic() + 1
        while pool.preferred_sender("slave-a") is auxiliary and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        release_probe.set()
        queue.stop()

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-b") is auxiliary
    assert pool.preferred_sender("slave-new") is replacement
