from concurrent.futures import ThreadPoolExecutor

from telegram.error import RetryAfter

from efb_telegram_master.outbound import (
    OutboundQueueScheduler,
    QueueRequest,
    SenderSelection,
    SenderSelectionResult,
)


def test_scheduler_serializes_calls_for_one_chat_and_returns_results():
    executed: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = OutboundQueueScheduler(
            executor,
            2,
            lambda _call, _now: SenderSelectionResult(SenderSelection(object(), None)),
            lambda _sender, _chat_id: True,
            lambda call, _sender: executed.append(call.kwargs["text"]) or call.kwargs["text"],
            lambda *_args: None,
        )
        first = scheduler.enqueue(QueueRequest("send_message", (), {"text": "first"}, 1))
        second = scheduler.enqueue(QueueRequest("send_message", (), {"text": "second"}, 1))
        while not first.done() or not second.done():
            scheduler.dispatch_once()
            scheduler.harvest_completed()

    assert first.result() == "first"
    assert second.result() == "second"
    assert executed == ["first", "second"]


def test_scheduler_retries_retry_after_before_finishing_waiter():
    attempts = 0
    retry_events = []

    def execute(_call, _sender):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryAfter(0)
        return "sent"

    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(
            executor,
            1,
            lambda _call, _now: SenderSelectionResult(SenderSelection(object(), None)),
            lambda _sender, _chat_id: True,
            execute,
            lambda *args: retry_events.append(args),
        )
        waiter = scheduler.enqueue(QueueRequest("send_message", (), {}, 1))
        while not waiter.done():
            scheduler.dispatch_once()
            scheduler.harvest_completed()

    assert waiter.result() == "sent"
    assert attempts == 2
    assert len(retry_events) == 1
