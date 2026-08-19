import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prometheus_client import generate_latest
from telegram.constants import MessageLimit
from telegram.error import RetryAfter

from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.outbound import OutboundQueue
from efb_telegram_master.outbound_types import (
    OutboundLifecycle,
    OutboundShutdownTimeout,
    QueueRequest,
    SchedulerStoppedError,
    UploadCleanup,
)
from tests.support.outbound_queue import _Limiter, _queue


def test_queue_records_cancelled_outcomes_once_for_shutdown_and_stop_during_retry():
    metrics = Metrics()
    pending_queue = OutboundQueue(Mock(), None, _Limiter(), worker_count=1, blocking_timeout=1, shutdown_drain_timeout=1, shutdown_join_grace=0.1)
    pending_queue.bind_metrics(metrics)
    pending = pending_queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "pending"}, 1))
    pending_queue.stop()
    with pytest.raises(SchedulerStoppedError):
        pending.result()

    attempted = threading.Event()

    class Sender:
        def send_message(self, *, chat_id, text):
            attempted.set()
            raise RetryAfter(60)

    retry_queue = _queue(Sender(), worker_count=1)
    retry_queue.bind_metrics(metrics)
    metrics.register_outbound_queue_collectors(retry_queue, top_n=1)
    retrying = retry_queue.enqueue(QueueRequest("send_message", (), {"chat_id": 987654, "text": "retry"}, 987654))
    assert attempted.wait(1)
    expected_retry = 'etm_outbound_retries_total{operation="send_message",reason="rate_limit"} 1.0'
    deadline = time.monotonic() + 1
    while expected_retry not in generate_latest(metrics.registry).decode() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert expected_retry in generate_latest(metrics.registry).decode()
    retry_rendered = generate_latest(metrics.registry).decode()
    assert 'etm_outbound_destination_queue_depth{destination="rank_1"} 1.0' in retry_rendered
    assert "987654" not in retry_rendered
    retry_queue.stop()
    with pytest.raises(SchedulerStoppedError):
        retrying.result()

    rendered = generate_latest(metrics.registry).decode()
    assert 'etm_outbound_outcomes_total{operation="send_message",outcome="cancelled"} 2.0' in rendered
    assert "987654" not in rendered


def test_shutdown_after_oversize_primary_cleans_owned_upload_without_queuing_attachment(tmp_path) -> None:
    upload = tmp_path / "owned-upload.bin"
    upload.write_bytes(b"upload")
    primary_started = threading.Event()
    release_primary = threading.Event()
    full_text = "x" * int(MessageLimit.MAX_TEXT_LENGTH)

    class Sender:
        def send_message(self, *, chat_id, text):
            primary_started.set()
            assert release_primary.wait(1)
            return SimpleNamespace(message_id=7)

        def send_document(self, *_args, **_kwargs):
            pytest.fail("attachment must not start after shutdown begins")

    auxiliary = Mock(bot_id=10, disabled=False, check_membership_tri=Mock(return_value=True), peek_delay=Mock(return_value=0.0), try_acquire_limits=Mock(return_value=True))
    auxiliary.bot = Sender()
    pool = BotPool([auxiliary])
    queue = _queue(Mock(), worker_count=1, bot_pool=pool)
    try:
        waiter = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": full_text}, 1, slave_id="slave-a", required_sender_bot_id="10", cleanup=UploadCleanup((str(upload),))))
        assert primary_started.wait(1)
        stopper = threading.Thread(target=queue.stop)
        stopper.start()
        deadline = time.monotonic() + 1
        while queue.lifecycle is OutboundLifecycle.RUNNING and time.monotonic() < deadline:
            time.sleep(0.01)
        assert queue.lifecycle is OutboundLifecycle.STOPPING
        release_primary.set()
        stopper.join(1)
        assert not stopper.is_alive()
        with pytest.raises(SchedulerStoppedError):
            waiter.result()
        assert queue.destination_snapshot() == []
        assert not upload.exists()
        assert pool.preferred_sender("slave-a") is None
    finally:
        release_primary.set()
        queue.stop()


def test_stop_timeout_keeps_blocked_send_and_upload_owned_until_later_stop(tmp_path) -> None:
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")
    started = threading.Event()
    release = threading.Event()

    class Sender:
        def send_document(self, *, chat_id, document):
            started.set()
            assert release.wait(1)
            return document

    cancellation_states = []
    queue = OutboundQueue(
        Sender(),
        None,
        _Limiter(),
        worker_count=1,
        blocking_timeout=1,
        shutdown_drain_timeout=0.02,
        shutdown_join_grace=0.02,
        cancel_active_calls=lambda: cancellation_states.append(queue.lifecycle),
    )
    queue.start()
    waiter = queue.enqueue(QueueRequest("send_document", (), {"chat_id": 1, "document": upload.as_uri()}, 1, cleanup=UploadCleanup((str(upload),))))
    assert started.wait(1)

    with pytest.raises(OutboundShutdownTimeout):
        queue.stop()

    assert cancellation_states == [OutboundLifecycle.STOPPING]
    assert queue.lifecycle is OutboundLifecycle.STOPPING
    assert upload.exists()
    with pytest.raises(SchedulerStoppedError):
        queue.enqueue(QueueRequest("send_message", (), {"chat_id": 2, "text": "later"}, 2))
    release.set()
    assert waiter.result(1).message == upload.as_uri()
    queue.stop()
    assert queue.lifecycle is OutboundLifecycle.FINALIZED
    assert not any(thread.name.startswith("ETM-send") and thread.is_alive() for thread in threading.enumerate())
    assert not upload.exists()


def test_queue_cleans_owned_upload_when_shutdown_cancels_pending_call(tmp_path) -> None:
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")
    queue = OutboundQueue(Mock(), None, _Limiter(), worker_count=1, blocking_timeout=1, shutdown_drain_timeout=1, shutdown_join_grace=0.1)

    waiter = queue.enqueue(QueueRequest("send_document", (), {"chat_id": 1, "document": upload.as_uri()}, 1, cleanup=UploadCleanup((str(upload),))))
    queue.stop()

    with pytest.raises(Exception, match="Outbound queue stopped"):
        waiter.result()
    assert not upload.exists()
