import io
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram.constants import MessageLimit
from telegram.error import BadRequest, RetryAfter

from efb_telegram_master.outbound import (
    OutboundQueue,
    QueuedCall,
    QueueRequest,
    SenderSelection,
    TelegramCallAdapter,
    retry_after_seconds,
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
