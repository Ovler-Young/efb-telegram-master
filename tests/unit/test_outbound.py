import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, RetryAfter

from efb_telegram_master.outbound import (
    OutboundQueue,
    QueuedCall,
    QueueRequest,
    SenderPolicy,
    SenderSelection,
    TelegramCallAdapter,
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


def test_queue_runs_distinct_chats_concurrently():
    barrier = threading.Barrier(2)

    class Sender:
        def send_message(self, *, chat_id, text):
            barrier.wait(1)
            return text

    queue = _queue(Sender())
    try:
        first = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "first"}, 1))
        second = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 2, "text": "second"}, 2))
        assert first.result(1).message == "first"
        assert second.result(1).message == "second"
    finally:
        queue.stop()


def test_queue_exposes_main_limiter_delay():
    class Limiter:
        def peek_delay(self, chat_id):
            return float(chat_id)

        def try_acquire(self, _chat_id):
            return True

        def occupancy_snapshot(self):
            return {"global": 0.0, "chat": 0.0}

    queue = OutboundQueue(
        object(),
        None,
        Limiter(),
        worker_count=1,
        blocking_timeout=1,
        shutdown_drain_timeout=1,
        shutdown_join_grace=0.1,
    )
    try:
        assert queue.main_limiter_delay(7) == 7.0
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


def test_stop_drains_in_flight_call():
    started = threading.Event()
    release = threading.Event()

    class Sender:
        def send_message(self, *, chat_id, text):
            started.set()
            assert release.wait(1)
            return text

    queue = _queue(Sender(), worker_count=1)
    waiter = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "sent"}, 1))
    assert started.wait(1)
    release.set()
    queue.stop()
    assert waiter.result(1).message == "sent"


def test_sender_policy_prefers_affinity_and_honors_required_sender():
    class Auxiliary:
        disabled = False

        def __init__(self, bot_id):
            self.bot_id = bot_id
            self.bot = object()

        def check_membership_tri(self, _chat_id):
            return True

        def peek_delay(self, _chat_id):
            return 0.0

        def try_acquire_limits(self, _chat_id):
            return True

    class Pool:
        def __init__(self):
            self.first, self.second = Auxiliary(1), Auxiliary(2)

        def get_bot_by_id(self, bot_id):
            return {"1": self.first, "2": self.second}.get(str(bot_id))

        def candidate_bots(self, _chat_id):
            return [(self.first, True), (self.second, True)]

        def preferred_sender(self, _slave_id):
            return self.second

        def rate_limit_occupancy_snapshot(self):
            return {"global": 0.0, "chat": 0.0}

    pool = Pool()
    policy = SenderPolicy(object(), pool, _Limiter())
    call = QueuedCall("send_message", (), {"chat_id": 1, "text": "sent"}, 1, "slave", None)
    assert policy.select(call, 0).selection == SenderSelection(pool.second.bot, "2")
    required = QueuedCall("send_message", (), {"chat_id": 1, "text": "sent"}, 1, None, "1")
    assert policy.select(required, 0).selection == SenderSelection(pool.first.bot, "1")


def test_adapter_removes_metadata_and_retries_parse_fallback():
    calls = []

    class Sender:
        def send_message(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise BadRequest("Can't parse entities")
            return "sent"

    call = QueuedCall("send_message", (), {"chat_id": 1, "text": "sent", "parse_mode": "HTML", "_slave_id": "x"}, 1, None, None)
    receipt = TelegramCallAdapter(None).execute(call, SenderSelection(Sender(), None))
    assert receipt.message == "sent"
    assert calls == [{"chat_id": 1, "text": "sent", "parse_mode": "HTML"}, {"chat_id": 1, "text": "sent"}]


def test_adapter_attaches_full_html_content_after_truncating_message():
    sent_messages = []
    documents = []

    class Sender:
        def send_message(self, **kwargs):
            sent_messages.append(kwargs)
            return SimpleNamespace(message_id=9)

        def send_document(self, *args, **kwargs):
            documents.append((args, kwargs))

    full_text = "x" * 4096
    call = QueuedCall("send_message", (), {"chat_id": 1, "text": full_text, "parse_mode": "HTML"}, 1, None, None)

    TelegramCallAdapter(None).execute(call, SenderSelection(Sender(), None))

    assert sent_messages == [{"chat_id": 1, "text": full_text[:100] + "\n...\n" + full_text[-100:], "parse_mode": "HTML"}]
    args, kwargs = documents[0]
    assert args[0] == 1
    assert kwargs["filename"] == "1_9.html"
    assert args[1].read() == b"<html><head><meta charset='utf-8'></head><body><pre style='white-space:pre-wrap'>" + full_text.encode() + b"</pre></body></html>"
