import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from prometheus_client import generate_latest
from telegram.error import NetworkError, RetryAfter

from efb_telegram_master.auxiliary_bot import AuxiliaryBot
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.outbound import OutboundQueue
from efb_telegram_master.outbound_types import QueueEnqueueError, QueueRequest
from efb_telegram_master.sender_policy import retry_after_seconds
from tests.support.outbound_queue import _Limiter, _queue


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


def test_queue_retries_network_errors_before_terminal_outcome():
    attempts = 0

    class Sender:
        def send_message(self, *, chat_id, text):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise NetworkError("temporary network failure")
            return SimpleNamespace(message_id=7)

    queue = _queue(Sender(), worker_count=1)
    try:
        receipt = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "sent"}, 1)).result(1)
        assert receipt.message.message_id == 7
        assert attempts == 2
    finally:
        queue.stop()


def test_queue_records_rate_limit_and_transport_retry_metrics_before_transport_exhaustion():
    attempts = 0

    class Sender:
        def send_message(self, *, chat_id, text):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RetryAfter(0)
            raise NetworkError("temporary network failure")

    metrics = Metrics()
    queue = _queue(Sender(), worker_count=1)
    queue.bind_metrics(metrics)
    try:
        with pytest.raises(NetworkError, match="temporary network failure"):
            queue.enqueue(QueueRequest("send_message", (), {"chat_id": 987654, "text": "sent"}, 987654)).result(2)
    finally:
        queue.stop()

    rendered = generate_latest(metrics.registry).decode()
    assert attempts == 4
    assert 'etm_outbound_retries_total{operation="send_message",reason="rate_limit"} 1.0' in rendered
    assert 'etm_outbound_retries_total{operation="send_message",reason="transport"} 2.0' in rendered
    assert 'etm_outbound_outcomes_total{operation="send_message",outcome="failure"} 1.0' in rendered
    assert "987654" not in rendered

def test_queue_collector_emits_oldest_age_for_a_live_pending_call() -> None:
    metrics = Metrics()
    queue = OutboundQueue(Mock(), None, _Limiter(), worker_count=1, blocking_timeout=1, shutdown_drain_timeout=1, shutdown_join_grace=0.1)
    metrics.register_outbound_queue_collectors(queue, top_n=1)
    try:
        queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "pending"}, 1))

        rendered = generate_latest(metrics.registry).decode()
    finally:
        queue.stop()

    age_lines = [line for line in rendered.splitlines() if line.startswith('etm_outbound_destination_oldest_age_seconds{destination="rank_1"}')]
    assert len(age_lines) == 1
    assert float(age_lines[0].rsplit(" ", 1)[1]) >= 0.0


@pytest.mark.parametrize(("value", "seconds"), [(3, 3.0), (timedelta(seconds=4), 4.0)])
def test_retry_after_seconds_accepts_numeric_and_timedelta_values(value, seconds):
    assert retry_after_seconds(RetryAfter(value)) == seconds


def test_queue_rejects_saturated_pending_work_and_releases_cancelled_request() -> None:
    started, release = threading.Event(), threading.Event()

    class Sender:
        def send_message(self, *, chat_id, text):
            started.set()
            assert release.wait(1)
            return SimpleNamespace(message_id=7)

    queue = OutboundQueue(Sender(), None, _Limiter(), worker_count=1, blocking_timeout=1, shutdown_drain_timeout=1, shutdown_join_grace=0.1, max_pending=1)
    queue.start()
    try:
        queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "active"}, 1))
        assert started.wait(1)
        cancelled = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 2, "text": "cancelled"}, 2))
        assert cancelled.cancel()
        queued = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 3, "text": "queued"}, 3))
        with pytest.raises(QueueEnqueueError, match="pending capacity"):
            queue.enqueue(QueueRequest("send_message", (), {"chat_id": 4, "text": "rejected"}, 4))
        release.set()
        assert queued.result(1).message.message_id == 7
    finally:
        release.set()
        queue.stop()


def test_queue_emits_bounded_outcome_saturation_and_latency_metrics() -> None:
    class Sender:
        def send_message(self, *, chat_id, text):
            if text == "fail":
                raise ValueError("terminal")
            return SimpleNamespace(message_id=chat_id)

    metrics = Metrics()
    queue = OutboundQueue(Sender(), None, _Limiter(), worker_count=1, blocking_timeout=1, shutdown_drain_timeout=1, shutdown_join_grace=0.1, max_pending=0)
    queue.bind_metrics(metrics)
    queue.start()
    try:
        with pytest.raises(QueueEnqueueError, match="pending capacity"):
            queue.enqueue(QueueRequest("send_message", (), {"chat_id": 987654, "text": "rejected"}, 987654))
    finally:
        queue.stop()

    queue = _queue(Sender(), worker_count=1)
    queue.bind_metrics(metrics)
    try:
        assert queue.enqueue(QueueRequest("send_message", (), {"chat_id": 987654, "text": "sent"}, 987654)).result(1).message.message_id == 987654
        with pytest.raises(ValueError, match="terminal"):
            queue.enqueue(QueueRequest("send_message", (), {"chat_id": 987654, "text": "fail"}, 987654)).result(1)
    finally:
        queue.stop()

    rendered = generate_latest(metrics.registry).decode()
    assert 'etm_outbound_outcomes_total{operation="send_message",outcome="rejected"} 1.0' in rendered
    assert 'etm_outbound_latency_seconds_count{operation="send_message",outcome="rejected"} 1.0' in rendered
    assert 'etm_outbound_latency_seconds_sum{operation="send_message",outcome="rejected"} 0.0' in rendered
    assert 'etm_outbound_outcomes_total{operation="send_message",outcome="success"} 1.0' in rendered
    assert 'etm_outbound_saturation_total{reason="pending_capacity"} 1.0' in rendered
    assert 'etm_outbound_outcomes_total{operation="send_message",outcome="failure"} 1.0' in rendered
    assert "987654" not in rendered


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
