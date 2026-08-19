import threading
import time
from types import SimpleNamespace

from prometheus_client import generate_latest
from telegram.constants import MessageLimit
from telegram.error import RetryAfter

from efb_telegram_master.core.etm_metrics import Metrics
from efb_telegram_master.outbound.outbound import OutboundQueue
from efb_telegram_master.outbound.outbound_types import QueueRequest
from tests.support.outbound_queue import _Limiter, _queue


def test_queue_retries_oversize_attachment_without_resending_primary() -> None:
    primary_calls = 0
    attachment_calls = 0
    full_text = "x" * int(MessageLimit.MAX_TEXT_LENGTH)

    class Sender:
        def send_message(self, *, chat_id, text):
            nonlocal primary_calls
            primary_calls += 1
            return SimpleNamespace(message_id=7)

        def send_document(self, _chat_id, _attachment, **_kwargs):
            nonlocal attachment_calls
            attachment_calls += 1
            if attachment_calls == 1:
                raise RetryAfter(0)
            return SimpleNamespace(message_id=8)

    queue = _queue(Sender(), worker_count=1)
    try:
        receipt = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": full_text}, 1)).result(1)
        assert receipt.message.message_id == 7
        assert primary_calls == 1
        deadline = time.monotonic() + 1
        while attachment_calls != 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert attachment_calls == 2
    finally:
        queue.stop()


def test_oversize_attachment_success_records_one_terminal_outcome_for_original_operation() -> None:
    full_text = "x" * int(MessageLimit.MAX_TEXT_LENGTH)

    class Sender:
        def send_message(self, *, chat_id, text):
            return SimpleNamespace(message_id=7)

        def send_document(self, _chat_id, _attachment, **_kwargs):
            return SimpleNamespace(message_id=8)

    metrics = Metrics()
    queue = _queue(Sender(), worker_count=1)
    queue.bind_metrics(metrics)
    try:
        receipt = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": full_text}, 1)).result(1)
        assert receipt.message.message_id == 7
        rendered = generate_latest(metrics.registry).decode()
        assert 'etm_outbound_outcomes_total{operation="send_message",outcome="enqueued"} 1.0' in rendered
        assert 'etm_outbound_outcomes_total{operation="send_message",outcome="success"} 1.0' in rendered
        assert 'operation="send_document"' not in rendered
    finally:
        queue.stop()


def test_attachment_retry_blocks_later_same_chat_call_but_not_other_chat() -> None:
    first_attachment = threading.Event()
    events: list[str] = []
    full_text = "x" * int(MessageLimit.MAX_TEXT_LENGTH)
    attachment_calls = 0

    class MainSender:
        def send_message(self, *, chat_id, text):
            events.append(f"main:{chat_id}:{text}")
            return SimpleNamespace(message_id=chat_id)

    class AuxiliarySender:
        def send_message(self, *, chat_id, text):
            events.append(f"auxiliary:{chat_id}:{text}")
            return SimpleNamespace(message_id=chat_id)

        def send_document(self, _chat_id, _attachment, **_kwargs):
            nonlocal attachment_calls
            attachment_calls += 1
            events.append(f"attachment:{attachment_calls}")
            if attachment_calls == 1:
                first_attachment.set()
                raise RetryAfter(0.1)
            return SimpleNamespace(message_id=10)

    auxiliary = SimpleNamespace(
        bot=AuxiliarySender(),
        bot_id=9,
        disabled=False,
        check_membership_tri=lambda _chat_id: True,
        peek_delay=lambda _chat_id: 0.0,
        try_acquire_limits=lambda _chat_id: True,
    )

    class BotPool:
        def get_bot_by_id(self, bot_id):
            return auxiliary if bot_id == "9" else None

        def candidate_bots(self, _chat_id):
            return []

        def preferred_sender(self, _slave_id):
            return None

    queue = OutboundQueue(MainSender(), BotPool(), _Limiter(), worker_count=1, blocking_timeout=1, shutdown_drain_timeout=1, shutdown_join_grace=0.1)
    queue.start()
    try:
        oversized = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": full_text}, 1, required_sender_bot_id="9"))
        assert first_attachment.wait(1)
        same_chat = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 1, "text": "later"}, 1))
        other_chat = queue.enqueue(QueueRequest("send_message", (), {"chat_id": 2, "text": "other"}, 2))

        assert other_chat.result(1).message.message_id == 2
        assert not same_chat.done()
        assert oversized.result(1).message.message_id == 1
        assert same_chat.result(1).message.message_id == 1
        assert events.index("main:2:other") < events.index("attachment:2") < events.index("main:1:later")
    finally:
        queue.stop()
