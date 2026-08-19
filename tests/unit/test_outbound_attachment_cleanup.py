import threading
import time
from types import SimpleNamespace

import pytest
from prometheus_client import generate_latest
from telegram.constants import MessageLimit
from telegram.error import RetryAfter

from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.outbound import OutboundQueue
from efb_telegram_master.outbound_types import QueueRequest, UploadCleanup
from tests.support.outbound_queue import _Limiter, _queue


def test_oversize_attachment_terminal_failure_does_not_resend_primary() -> None:
    primary_calls = 0
    full_text = "x" * int(MessageLimit.MAX_TEXT_LENGTH)
    attachment_started = threading.Event()
    release_attachment = threading.Event()
    attachment_failed = threading.Event()

    class Sender:
        def send_message(self, *, chat_id, text):
            nonlocal primary_calls
            primary_calls += 1
            return SimpleNamespace(message_id=7)

        def send_document(self, _chat_id, _attachment, **_kwargs):
            attachment_started.set()
            assert release_attachment.wait(1)
            attachment_failed.set()
            raise ValueError("attachment failed")

    metrics = Metrics()
    queue = _queue(Sender(), worker_count=1)
    queue.bind_metrics(metrics)
    try:
        waiter = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": full_text}, 1))
        assert attachment_started.wait(1)
        assert not waiter.done()
        release_attachment.set()
        assert attachment_failed.wait(1)
        with pytest.raises(ValueError, match="attachment failed"):
            waiter.result(1)
        assert primary_calls == 1
        rendered = generate_latest(metrics.registry).decode()
        assert 'etm_outbound_outcomes_total{operation="send_message",outcome="enqueued"} 1.0' in rendered
        assert 'etm_outbound_outcomes_total{operation="send_message",outcome="attachment_failure"} 1.0' in rendered
        assert 'operation="send_document"' not in rendered
    finally:
        release_attachment.set()
        queue.stop()


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
