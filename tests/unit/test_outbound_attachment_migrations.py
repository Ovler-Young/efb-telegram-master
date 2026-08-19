import threading
import time
from types import SimpleNamespace

import pytest
from prometheus_client import generate_latest
from telegram.constants import MessageLimit
from telegram.error import ChatMigrated

from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.outbound_types import QueueError, QueueRequest, UploadCleanup
from tests.support.outbound_queue import _queue


def test_queue_migrates_oversize_attachment_without_resending_primary() -> None:
    primary_calls = 0
    attachment_chat_ids: list[int] = []
    full_text = "x" * int(MessageLimit.MAX_TEXT_LENGTH)

    class Sender:
        def send_message(self, *, chat_id, text):
            nonlocal primary_calls
            primary_calls += 1
            assert chat_id == 1
            return SimpleNamespace(message_id=7)

        def send_document(self, chat_id, attachment, **_kwargs):
            attachment_chat_ids.append(chat_id)
            assert attachment.read() == full_text.encode()
            if len(attachment_chat_ids) == 1:
                raise ChatMigrated(2)
            return SimpleNamespace(message_id=8)

    queue = _queue(Sender(), worker_count=1)
    try:
        receipt = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": full_text}, 1)).result(1)
        assert receipt.message.message_id == 7
        assert primary_calls == 1
        deadline = time.monotonic() + 1
        while attachment_chat_ids != [1, 2] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert attachment_chat_ids == [1, 2]
    finally:
        queue.stop()


def test_attachment_migration_preserves_order_for_old_and_new_destinations() -> None:
    attachment_retried = threading.Event()
    release_attachment = threading.Event()
    events: list[str] = []
    full_text = "x" * int(MessageLimit.MAX_TEXT_LENGTH)
    attachment_calls = 0

    class Sender:
        def send_message(self, *, chat_id, text):
            events.append(f"message:{chat_id}:{text}")
            return SimpleNamespace(message_id=chat_id)

        def send_document(self, chat_id, _attachment, **_kwargs):
            nonlocal attachment_calls
            attachment_calls += 1
            events.append(f"attachment:{chat_id}:{attachment_calls}")
            if attachment_calls == 1:
                raise ChatMigrated(2)
            attachment_retried.set()
            assert release_attachment.wait(1)
            return SimpleNamespace(message_id=8)

    queue = _queue(Sender(), worker_count=2)
    try:
        oversized = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": full_text}, 1))
        assert attachment_retried.wait(1)
        old_destination = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "old"}, 1))
        new_destination = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 2, "text": "new"}, 2))
        other_destination = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 3, "text": "other"}, 3))

        assert other_destination.result(1).message.message_id == 3
        assert not old_destination.done()
        assert not new_destination.done()
        release_attachment.set()
        assert oversized.result(1).message.message_id == 1
        assert old_destination.result(1).message.message_id == 2
        assert new_destination.result(1).message.message_id == 2
        assert events.index("message:3:other") < events.index("message:2:old") < events.index("message:2:new")
        post_completion = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "post-completion"}, 1))
        assert post_completion.result(1).message.message_id == 1
    finally:
        queue.stop()


def test_repeated_attachment_migration_fails_without_resending_primary() -> None:
    primary_calls = 0
    attachment_calls = 0
    message_chat_ids: list[int] = []
    full_text = "x" * int(MessageLimit.MAX_TEXT_LENGTH)

    class Sender:
        def send_message(self, *, chat_id, text):
            nonlocal primary_calls
            primary_calls += 1
            message_chat_ids.append(chat_id)
            return SimpleNamespace(message_id=7)

        def send_document(self, _chat_id, _attachment, **_kwargs):
            nonlocal attachment_calls
            attachment_calls += 1
            raise ChatMigrated(attachment_calls + 1)

    queue = _queue(Sender(), worker_count=1)
    try:
        waiter = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": full_text}, 1))
        with pytest.raises(QueueError, match="migrated repeatedly"):
            waiter.result(1)
        assert primary_calls == 1
        assert attachment_calls == 2
        assert queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "post-failure"}, 1)).result(1).message.message_id == 7
        assert message_chat_ids == [1, 1]
    finally:
        queue.stop()


def test_queue_records_primary_migration_retry_and_preserves_owned_upload(tmp_path) -> None:
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")

    class Sender:
        def send_document(self, *, chat_id, document):
            raise ChatMigrated(2)

    metrics = Metrics()
    queue = _queue(Sender(), worker_count=1)
    queue.bind_metrics(metrics)
    try:
        waiter = queue.enqueue(QueueRequest("send_document", (), {"chat_id": 1, "document": upload.as_uri()}, 1, cleanup=UploadCleanup((str(upload),))))
        with pytest.raises(ChatMigrated):
            waiter.result(1)
        assert upload.exists()
        rendered = generate_latest(metrics.registry).decode()
        assert 'etm_outbound_retries_total{operation="send_document",reason="migration"} 1.0' in rendered
        assert 'etm_outbound_outcomes_total{operation="send_document",outcome="failure"} 1.0' in rendered
        assert 'etm_outbound_latency_seconds_count{operation="send_document",outcome="failure"} 1.0' in rendered
    finally:
        queue.stop()
