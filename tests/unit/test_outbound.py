import threading
from datetime import timedelta

import pytest
from telegram.error import RetryAfter

from efb_telegram_master.outbound import (
    OutboundQueue,
    QueueRequest,
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
