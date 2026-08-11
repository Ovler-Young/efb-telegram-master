import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from telethon.events import MessageEdited

from tests.integration.helper import filters
from tests.integration.helper import helper as helper_module


class StalledTelegramClient:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def connect(self) -> None:
        await asyncio.Future()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class CompletingTelegramClient:
    def __init__(self) -> None:
        self.disconnect_calls = 0
        self.disconnected_observed = False
        self.disconnected = self._disconnected()

    async def _disconnected(self) -> None:
        self.disconnected_observed = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class HangingDisconnectTelegramClient:
    def __init__(self) -> None:
        self.disconnected = asyncio.Future()

    async def disconnect(self) -> None:
        return None


class FailingCleanupTelegramClient(StalledTelegramClient):
    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        raise RuntimeError("disconnect failed")


class RecordingTelegramClient:
    def __init__(self, *_args, **_kwargs) -> None:
        self.parse_mode = None
        self.added_handlers = []
        self.removed_handlers = []
        self.disconnect_calls = 0

    def add_event_handler(self, handler, event_builder) -> None:
        self.added_handlers.append((handler, event_builder))

    def remove_event_handler(self, handler, event_builder) -> None:
        self.removed_handlers.append((handler, event_builder))

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class DummyEditedMessageEvent(MessageEdited.Event):
    @property
    def chat_id(self) -> int:
        return 100

    def to_dict(self) -> dict[str, object]:
        return {}


def build_event_helper() -> helper_module.TelegramIntegrationTestHelper:
    test_helper = object.__new__(helper_module.TelegramIntegrationTestHelper)
    test_helper.queue = asyncio.Queue()
    test_helper.pending_events = []
    test_helper._event_sequence = 0
    test_helper._event_metadata = {}
    test_helper.message_chat_map = {}
    test_helper.chats = {100}
    test_helper._temporary_chat_counts = {}
    test_helper._temporary_chat_lock = threading.Lock()
    test_helper.logger = logging.getLogger(__name__)
    return test_helper


def test_helper_registers_and_removes_a_broad_edited_message_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper_module, "TelegramClient", RecordingTelegramClient)
    monkeypatch.setattr(helper_module, "StringSession", lambda _session: object())
    test_helper = helper_module.TelegramIntegrationTestHelper("session", 1, "hash", None, 2, chats={100})
    client = test_helper.client

    edited_builders = [event_builder for _handler, event_builder in client.added_handlers if isinstance(event_builder, MessageEdited)]
    assert len(edited_builders) == 1
    assert edited_builders[0].chats is None

    asyncio.run(test_helper._disconnect_client())

    assert client.removed_handlers == client.added_handlers
    assert client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_helper_queues_watched_edited_message_events_for_strict_id_correlation() -> None:
    test_helper = build_event_helper()
    event = object.__new__(DummyEditedMessageEvent)
    event.__dict__["_init"] = False
    event.message = SimpleNamespace(id=42)

    await test_helper.edited_message_handler(event)

    assert await test_helper.wait_for_event(filters.edited(42)) is event


@pytest.mark.asyncio
async def test_helper_disconnects_partially_started_client_after_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = StalledTelegramClient()
    test_helper = object.__new__(helper_module.TelegramIntegrationTestHelper)
    test_helper.client = client
    test_helper.logger = logging.getLogger(__name__)
    monkeypatch.setattr(helper_module, "CLIENT_START_TIMEOUT", 0.01)

    with pytest.raises(TimeoutError, match="client connect"):
        await test_helper.__aenter__()

    assert client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_helper_preserves_startup_failure_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FailingCleanupTelegramClient()
    test_helper = object.__new__(helper_module.TelegramIntegrationTestHelper)
    test_helper.client = client
    test_helper.logger = logging.getLogger(__name__)
    monkeypatch.setattr(helper_module, "CLIENT_START_TIMEOUT", 0.01)

    with pytest.raises(TimeoutError, match="client connect"):
        await test_helper.__aenter__()

    assert client.disconnect_calls == 1
    assert "Failed to clean up Telegram client after startup failure" in caplog.text


@pytest.mark.asyncio
async def test_helper_waits_for_telethon_disconnect_completion() -> None:
    client = CompletingTelegramClient()
    test_helper = object.__new__(helper_module.TelegramIntegrationTestHelper)
    test_helper.client = client

    await test_helper._disconnect_client()

    assert client.disconnect_calls == 1
    assert client.disconnected_observed


@pytest.mark.asyncio
async def test_helper_disconnect_completion_has_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = HangingDisconnectTelegramClient()
    test_helper = object.__new__(helper_module.TelegramIntegrationTestHelper)
    test_helper.client = client
    monkeypatch.setattr(helper_module, "CLIENT_STOP_TIMEOUT", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        await test_helper._disconnect_client()


@pytest.mark.asyncio
async def test_helper_retains_out_of_order_photo_and_title_events() -> None:
    test_helper = build_event_helper()
    photo = SimpleNamespace(kind="photo")
    title = SimpleNamespace(kind="title")
    await test_helper.queue.put(photo)
    await test_helper.queue.put(title)

    received_title = await test_helper.wait_for_event(lambda event: event.kind == "title")
    received_photo = await test_helper.wait_for_event(lambda event: event.kind == "photo")

    assert received_title is title
    assert received_photo is photo
    assert not test_helper.pending_events


@pytest.mark.asyncio
async def test_helper_retains_nonmatching_event_for_later_wait() -> None:
    test_helper = build_event_helper()
    unmatched = SimpleNamespace(kind="unmatched")
    matching = SimpleNamespace(kind="matching")
    await test_helper.queue.put(unmatched)
    await test_helper.queue.put(matching)

    assert await test_helper.wait_for_event(lambda event: event.kind == "matching") is matching
    assert test_helper.pending_events == [unmatched]
    assert await test_helper.wait_for_event(lambda event: event.kind == "unmatched") is unmatched


@pytest.mark.asyncio
async def test_helper_cursor_ignores_prior_events_without_discarding_them() -> None:
    test_helper = build_event_helper()
    prior = SimpleNamespace(kind="prior")
    response = SimpleNamespace(kind="response")
    await test_helper._queue_event(prior)
    cursor = test_helper.event_cursor()

    await test_helper._queue_event(response)
    assert await test_helper.wait_for_event(lambda event: event.kind in {"prior", "response"}, after_cursor=cursor) is response

    assert await test_helper.wait_for_event(lambda event: event.kind == "prior") is prior


@pytest.mark.asyncio
async def test_helper_concurrent_cursor_waits_preserve_interleaved_events() -> None:
    test_helper = build_event_helper()
    before_a = SimpleNamespace(kind="before-a")
    between_a_and_b = SimpleNamespace(kind="between-a-and-b")
    a_response = SimpleNamespace(kind="a")
    b_response = SimpleNamespace(kind="b")
    await test_helper._queue_event(before_a)
    a_cursor = test_helper.event_cursor()
    await test_helper._queue_event(between_a_and_b)
    b_cursor = test_helper.event_cursor()

    a_wait = asyncio.create_task(test_helper.wait_for_event(lambda event: event.kind == "a", after_cursor=a_cursor))
    b_wait = asyncio.create_task(test_helper.wait_for_event(lambda event: event.kind == "b", after_cursor=b_cursor))
    await asyncio.sleep(0)
    await test_helper._queue_event(a_response)
    await test_helper._queue_event(b_response)

    assert await a_wait is a_response
    assert await b_wait is b_response
    assert await test_helper.wait_for_event(lambda event: event.kind == "before-a") is before_a
    assert await test_helper.wait_for_event(lambda event: event.kind == "between-a-and-b") is between_a_and_b


@pytest.mark.asyncio
async def test_helper_prunes_expired_and_overflow_pending_events(monkeypatch: pytest.MonkeyPatch) -> None:
    test_helper = build_event_helper()
    monkeypatch.setattr(helper_module, "PENDING_EVENT_MAX_COUNT", 2)
    monkeypatch.setattr(helper_module, "PENDING_EVENT_MAX_AGE_SECONDS", 1.0)
    events = [SimpleNamespace(kind=index) for index in range(4)]
    cursor = test_helper.event_cursor()
    for event in events:
        await test_helper._queue_event(event)
    with pytest.raises(asyncio.TimeoutError):
        await test_helper.wait_for_event(lambda _: False, timeout=0.01, after_cursor=cursor)

    assert [event.kind for event in test_helper.pending_events] == [2, 3]
    metadata = test_helper._event_metadata[id(events[2])]
    test_helper._event_metadata[id(events[2])] = helper_module.EventMetadata(metadata.sequence, time.monotonic() - 2.0)
    test_helper.event_cursor()
    assert [event.kind for event in test_helper.pending_events] == [3]


@pytest.mark.asyncio
async def test_helper_pending_events_remain_bounded_across_scoped_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    test_helper = build_event_helper()
    monkeypatch.setattr(helper_module, "PENDING_EVENT_MAX_COUNT", 3)
    for batch in range(4):
        cursor = test_helper.event_cursor()
        for offset in range(3):
            await test_helper._queue_event(SimpleNamespace(kind=(batch, offset)))
        with pytest.raises(asyncio.TimeoutError):
            await test_helper.wait_for_event(lambda _: False, timeout=0.01, after_cursor=cursor)
        assert len(test_helper.pending_events) <= 3


@pytest.mark.asyncio
async def test_helper_bounds_raw_queue_and_metadata_without_waiters() -> None:
    test_helper = build_event_helper()

    for index in range(1_000):
        await test_helper._queue_event(SimpleNamespace(kind=index))

    assert test_helper.queue.qsize() == helper_module.PENDING_EVENT_MAX_COUNT
    assert len(test_helper._event_metadata) == helper_module.PENDING_EVENT_MAX_COUNT
    test_helper.clear_queue()
    assert not test_helper._event_metadata


@pytest.mark.asyncio
async def test_helper_discards_oldest_raw_events_before_accepting_newer_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    test_helper = build_event_helper()
    monkeypatch.setattr(helper_module, "PENDING_EVENT_MAX_COUNT", 3)

    for index in range(5):
        await test_helper._queue_event(SimpleNamespace(kind=index))

    assert [(await test_helper.wait_for_event(timeout=0.01)).kind for _ in range(3)] == [2, 3, 4]
    assert not test_helper._event_metadata


@pytest.mark.asyncio
async def test_helper_discards_expired_raw_events_before_filtering() -> None:
    test_helper = build_event_helper()
    expired = SimpleNamespace(kind="expired")
    fresh = SimpleNamespace(kind="fresh")
    await test_helper._queue_event(expired)
    metadata = test_helper._event_metadata[id(expired)]
    test_helper._event_metadata[id(expired)] = helper_module.EventMetadata(
        metadata.sequence,
        time.monotonic() - helper_module.PENDING_EVENT_MAX_AGE_SECONDS - 1.0,
    )
    await test_helper._queue_event(fresh)

    assert await test_helper.wait_for_event(lambda event: event.kind in {"expired", "fresh"}) is fresh
    assert id(expired) not in test_helper._event_metadata
    assert not test_helper._event_metadata


@pytest.mark.asyncio
async def test_helper_bounds_combined_raw_and_pending_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    test_helper = build_event_helper()
    monkeypatch.setattr(helper_module, "PENDING_EVENT_MAX_COUNT", 3)
    for index in range(3):
        await test_helper._queue_event(SimpleNamespace(kind=("pending", index)))
    with pytest.raises(asyncio.TimeoutError):
        await test_helper.wait_for_event(lambda _: False, timeout=0.01)

    for index in range(3):
        await test_helper._queue_event(SimpleNamespace(kind=("raw", index)))

    assert len(test_helper.pending_events) == 3
    assert test_helper.queue.qsize() == 3
    assert len(test_helper._event_metadata) == 6


def test_helper_clear_queue_discards_pending_events() -> None:
    test_helper = build_event_helper()
    test_helper.pending_events.append(SimpleNamespace(kind="stale"))

    test_helper.clear_queue()

    assert not test_helper.pending_events


def test_helper_keeps_a_dynamic_chat_watched_until_its_final_unwatch() -> None:
    test_helper = build_event_helper()

    test_helper.watch_chat(-200)
    test_helper.watch_chat(-200)
    test_helper.watch_chat(-300)

    assert test_helper._watches_chat(-200)
    assert test_helper._watches_chat(100)
    assert test_helper._watches_chat(-300)

    test_helper.unwatch_chat(-200)

    assert test_helper._watches_chat(-200)
    assert test_helper._watches_chat(-300)

    test_helper.unwatch_chat(-200)

    assert not test_helper._watches_chat(-200)
    assert test_helper._watches_chat(-300)

    test_helper.unwatch_chat(-300)

    assert not test_helper._watches_chat(-300)


@pytest.mark.asyncio
async def test_helper_records_a_bot_reply_from_a_scoped_dynamic_chat() -> None:
    test_helper = build_event_helper()
    test_helper.logger = logging.getLogger(__name__)
    event = SimpleNamespace(
        chat_id=-200,
        message=SimpleNamespace(id=3, get_input_chat=_async_input_chat),
        to_dict=lambda: {"message_id": 3},
    )

    await test_helper.new_message_handler(event)

    assert test_helper.queue.empty()

    test_helper.watch_chat(-200)
    await test_helper.new_message_handler(event)

    assert await test_helper.queue.get() is event


async def _async_input_chat() -> str:
    return "chat"


@pytest.mark.asyncio
async def test_private_response_uses_one_deadline_for_limiter_and_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps, received = [], []

    async def wait_for_slot(_, *, cap):
        caps.append(cap)

    async def trigger():
        return None

    async def receive(timeout):
        received.append(timeout)
        return "reply"

    monkeypatch.setattr(helper_module, "wait_for_limiter_slot", wait_for_slot)
    monotonic = iter((100.0, 100.0, 110.0)).__next__
    monkeypatch.setattr(helper_module, "time", SimpleNamespace(monotonic=monotonic))
    assert await helper_module.wait_for_private_response(lambda: 0.0, trigger, receive) == "reply"
    assert caps == [65.0]
    assert received == [55.0]


@pytest.mark.asyncio
async def test_private_response_deadline_includes_trigger() -> None:
    response_received = False

    async def trigger() -> None:
        await asyncio.Future()

    async def receive(_) -> None:
        nonlocal response_received
        response_received = True

    with pytest.raises(asyncio.TimeoutError):
        await helper_module.wait_for_private_response(lambda: 0.0, trigger, receive, cap=0.01)

    assert not response_received


@pytest.mark.asyncio
async def test_private_response_captures_cursor_before_fast_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    test_helper = build_event_helper()
    prior = SimpleNamespace(kind="prior")
    response = SimpleNamespace(kind="response")
    await test_helper._queue_event(prior)
    phases = []

    async def wait_for_slot(_, *, cap):
        phases.append(("limiter", cap))

    async def trigger():
        phases.append(("trigger", None))
        await test_helper._queue_event(response)

    async def receive(_):
        phases.append(("receive", None))
        return await test_helper.wait_for_event(lambda event: event.kind in {"prior", "response"})

    monkeypatch.setattr(helper_module, "wait_for_limiter_slot", wait_for_slot)
    assert await helper_module.wait_for_private_response(lambda: 0.0, trigger, receive, response_cursor=test_helper.event_cursor) is response
    assert [phase for phase, _ in phases] == ["limiter", "trigger", "receive"]
    assert await test_helper.wait_for_event(lambda event: event.kind == "prior") is prior


@pytest.mark.asyncio
async def test_concurrent_private_responses_keep_independent_cursors() -> None:
    test_helper = build_event_helper()
    before_a = SimpleNamespace(kind="before-a")
    between_a_and_b = SimpleNamespace(kind="between-a-and-b")
    a_response = SimpleNamespace(kind="a")
    b_response = SimpleNamespace(kind="b")
    a_trigger_started = asyncio.Event()
    release_a_trigger = asyncio.Event()
    await test_helper._queue_event(before_a)

    async def a_trigger() -> None:
        a_trigger_started.set()
        await release_a_trigger.wait()
        await test_helper._queue_event(a_response)

    async def b_trigger() -> None:
        await test_helper._queue_event(b_response)

    async def receive_a(_: float):
        return await test_helper.wait_for_event(lambda event: event.kind == "a")

    async def receive_b(_: float):
        return await test_helper.wait_for_event(lambda event: event.kind == "b")

    a_wait = asyncio.create_task(helper_module.wait_for_private_response(lambda: 0.0, a_trigger, receive_a, response_cursor=test_helper.event_cursor))
    await a_trigger_started.wait()
    await test_helper._queue_event(between_a_and_b)
    b_wait = asyncio.create_task(helper_module.wait_for_private_response(lambda: 0.0, b_trigger, receive_b, response_cursor=test_helper.event_cursor))
    await asyncio.sleep(0)
    release_a_trigger.set()

    assert await a_wait is a_response
    assert await b_wait is b_response
    assert await test_helper.wait_for_event(lambda event: event.kind == "before-a") is before_a
    assert await test_helper.wait_for_event(lambda event: event.kind == "between-a-and-b") is between_a_and_b


@pytest.mark.asyncio
async def test_wait_for_limiter_slot_caps_wait_at_65_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monotonic_values = iter((0.0, 0.0, 65.0))
    sleeps = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(helper_module, "time", SimpleNamespace(monotonic=lambda: next(monotonic_values)))
    monkeypatch.setattr(helper_module.asyncio, "sleep", sleep)
    with pytest.raises(TimeoutError, match="65 seconds"):
        await helper_module.wait_for_limiter_slot(lambda: 70.0)
    assert sleeps == [65.0]


class StateMessage:
    def __init__(self, button_count: int) -> None:
        self.button_count = button_count

    def to_dict(self) -> dict[str, int]:
        return {"buttons": self.button_count}


class StateClient:
    def __init__(self, messages: list[object | None]) -> None:
        self.messages = iter(messages)
        self.calls: list[tuple[int, int]] = []

    async def get_messages(self, chat_id: int, *, ids: int) -> object | None:
        self.calls.append((chat_id, ids))
        return next(self.messages)


@pytest.mark.asyncio
async def test_wait_for_message_state_returns_immediate_exact_message() -> None:
    message = StateMessage(button_count=0)
    client = StateClient([message])

    original_message_type = helper_module.Message
    helper_module.Message = StateMessage
    try:
        result = await helper_module.wait_for_message_state(client, 100, 42, lambda current: current.button_count == 0)
    finally:
        helper_module.Message = original_message_type

    assert result is message
    assert client.calls == [(100, 42)]


@pytest.mark.asyncio
async def test_wait_for_message_state_observes_a_later_exact_message(monkeypatch: pytest.MonkeyPatch) -> None:
    message = StateMessage(button_count=0)
    client = StateClient([None, message])
    waits: list[float] = []

    async def yield_control(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(helper_module.asyncio, "sleep", yield_control)
    monkeypatch.setattr(helper_module, "Message", StateMessage)
    result = await helper_module.wait_for_message_state(client, 100, 42, lambda current: current.button_count == 0)

    assert result is message
    assert client.calls == [(100, 42), (100, 42)]
    assert waits == [1.0]


@pytest.mark.asyncio
async def test_wait_for_message_state_reports_the_last_exact_state_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    client = StateClient([StateMessage(button_count=1)])
    monkeypatch.setattr(helper_module.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(helper_module, "Message", StateMessage)

    with pytest.raises(TimeoutError, match="last_state={'buttons': 1}"):
        await helper_module.wait_for_message_state(client, 100, 42, lambda current: current.button_count == 0, timeout=0.0)


@pytest.mark.asyncio
async def test_wait_for_message_state_propagates_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    class CancelledClient:
        async def get_messages(self, _chat_id: int, *, ids: int) -> object:
            raise asyncio.CancelledError(ids)

    monkeypatch.setattr(helper_module, "Message", StateMessage)
    with pytest.raises(asyncio.CancelledError):
        await helper_module.wait_for_message_state(CancelledClient(), 100, 42, lambda _current: True)


@pytest.mark.asyncio
async def test_wait_for_message_state_rejects_unexpected_list_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper_module, "Message", StateMessage)
    with pytest.raises(TypeError, match="list"):
        await helper_module.wait_for_message_state(StateClient([[StateMessage(button_count=0)]]), 100, 42, lambda _current: True)


@pytest.mark.asyncio
async def test_wait_for_message_state_treats_message_empty_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyMessage:
        pass

    monkeypatch.setattr(helper_module, "Message", StateMessage)
    monkeypatch.setattr(helper_module, "MessageEmpty", EmptyMessage)
    monkeypatch.setattr(helper_module.time, "monotonic", lambda: 100.0)
    with pytest.raises(TimeoutError, match="last_state=missing"):
        await helper_module.wait_for_message_state(StateClient([EmptyMessage()]), 100, 42, lambda _current: True, timeout=0.0)


@pytest.mark.asyncio
async def test_wait_for_message_state_propagates_predicate_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper_module, "Message", StateMessage)
    with pytest.raises(RuntimeError, match="predicate failed"):
        await helper_module.wait_for_message_state(StateClient([StateMessage(button_count=0)]), 100, 42, lambda _current: (_ for _ in ()).throw(RuntimeError("predicate failed")))
