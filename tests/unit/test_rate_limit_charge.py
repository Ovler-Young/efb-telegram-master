"""Rate-limit capacity must only be spent on calls Telegram is asked to make."""
from concurrent.futures import ThreadPoolExecutor

from efb_telegram_master.outbound import OutboundQueue, OutboundQueueScheduler, QueueRequest
from tests.unit.test_outbound import Adapter
from tests.unit.test_outbound_queue_runtime_evidence import send_document


def send_message(chat_id: int, text: str):
    return chat_id, text


class RecordingAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.acquired: list[int] = []

    def acquire_sender_limits(self, selection, telegram_chat_id):
        self.acquired.append(telegram_chat_id)
        return super().acquire_sender_limits(selection, telegram_chat_id)


def test_row_discarded_for_missing_media_spends_no_rate_limit_capacity(tmp_path):
    external = tmp_path / "tdlib-temporary.bin"
    external.write_bytes(b"temporary media")
    queue = OutboundQueue(tmp_path)
    queue.enqueue_many([QueueRequest(
        "send_document", (151, external.as_uri()), {}, cleanup_files=(str(external),),
    )], lambda _operation: send_document)
    queue.close()
    external.unlink()  # TDLib removes its temporary files on restart.

    queue = OutboundQueue(tmp_path)
    adapter = RecordingAdapter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        try:
            scheduler.dispatch_once()
            assert queue.heads() == [], "the undeliverable row should be discarded"
            assert adapter.acquired == [], "a discarded row consumed rate-limit capacity"
        finally:
            queue.close()


def test_dispatchable_row_spends_rate_limit_capacity_once(tmp_path):
    queue = OutboundQueue(tmp_path)
    queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": 7, "text": "sent"})],
                       lambda _operation: send_message)
    adapter = RecordingAdapter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        try:
            scheduler.dispatch_once()
            assert adapter.acquired == [7]
        finally:
            queue.close()
