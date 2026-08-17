import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from tests.integration.helper import helper as helper_module


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


class StalledTelegramClient:
    def __init__(self) -> None:
        self.disconnect_calls = 0

    async def connect(self) -> None:
        await asyncio.Future()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class HangingDisconnectTelegramClient:
    def __init__(self) -> None:
        self.disconnected = asyncio.Future()

    async def disconnect(self) -> None:
        return None


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


def test_helper_cleanup_removes_every_registered_event_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper_module, "TelegramClient", RecordingTelegramClient)
    monkeypatch.setattr(helper_module, "StringSession", lambda _session: object())
    test_helper = helper_module.TelegramIntegrationTestHelper("session", 1, "hash", None, 2, chats={100})

    asyncio.run(test_helper._disconnect_client())

    assert test_helper.client.removed_handlers == test_helper.client.added_handlers
    assert test_helper.client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_event_queue_discards_oldest_events_and_returns_only_the_newer_cursor_response(monkeypatch: pytest.MonkeyPatch) -> None:
    test_helper = build_event_helper()
    monkeypatch.setattr(helper_module, "PENDING_EVENT_MAX_COUNT", 3)
    for index in range(5):
        await test_helper._queue_event(SimpleNamespace(kind=index))
    pre_cursor_response = SimpleNamespace(kind="response")
    await test_helper._queue_event(pre_cursor_response)
    cursor = test_helper.event_cursor()
    response = SimpleNamespace(kind="response")
    await test_helper._queue_event(response)

    assert test_helper.queue.qsize() == 3
    assert await test_helper.wait_for_event(lambda event: event.kind == "response", after_cursor=cursor) is response
    assert [event.kind for event in test_helper.pending_events] == [4, "response"]
    assert test_helper.pending_events[1] is pre_cursor_response


def test_temporary_chat_watch_remains_until_the_final_consumer_unwatches():
    test_helper = build_event_helper()

    test_helper.watch_chat(-200)
    test_helper.watch_chat(-200)
    test_helper.unwatch_chat(-200)

    assert test_helper._watches_chat(200)
    test_helper.unwatch_chat(-200)
    assert not test_helper._watches_chat(200)


@pytest.mark.asyncio
async def test_client_startup_timeout_disconnects_the_partially_started_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = StalledTelegramClient()
    test_helper = object.__new__(helper_module.TelegramIntegrationTestHelper)
    test_helper.client = client
    test_helper.logger = logging.getLogger(__name__)
    monkeypatch.setattr(helper_module, "CLIENT_START_TIMEOUT", 0.01)

    with pytest.raises(TimeoutError, match="client connect"):
        await test_helper.__aenter__()

    assert client.disconnect_calls == 1


@pytest.mark.asyncio
async def test_client_disconnect_times_out_when_telethon_never_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    test_helper = object.__new__(helper_module.TelegramIntegrationTestHelper)
    test_helper.client = HangingDisconnectTelegramClient()
    monkeypatch.setattr(helper_module, "CLIENT_STOP_TIMEOUT", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        await test_helper._disconnect_client()


class StateMessage:
    def __init__(self, button_count: int) -> None:
        self.button_count = button_count

    def to_dict(self) -> dict[str, int]:
        return {"buttons": self.button_count}


class StateClient:
    def __init__(self, messages: list[object | None]) -> None:
        self.messages = iter(messages)

    async def get_messages(self, _chat_id: int, *, ids: int) -> object | None:
        return next(self.messages)


class RecentMessage(StateMessage):
    def __init__(self, message_id: int, button_count: int) -> None:
        super().__init__(button_count)
        self.id = message_id


class RecentMessageClient:
    def __init__(self, messages: list[list[object]]) -> None:
        self.messages = iter(messages)
        self.calls: list[tuple[int, int, int, int, bool]] = []

    async def get_messages(self, chat_id: int, *, min_id: int, offset_id: int, limit: int, reverse: bool) -> list[object]:
        self.calls.append((chat_id, min_id, offset_id, limit, reverse))
        return next(self.messages)


@pytest.mark.asyncio
async def test_wait_for_new_message_after_advances_after_a_capped_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [[RecentMessage(message_id, button_count=0) for message_id in range(first, first + helper_module.NEW_MESSAGE_PAGE_SIZE)] for first in (13, 33, 53)]
    response = RecentMessage(73, button_count=1)
    client = RecentMessageClient([*pages, [response]])
    waits: list[float] = []

    async def yield_control(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(helper_module, "Message", RecentMessage)
    monkeypatch.setattr(helper_module.asyncio, "sleep", yield_control)

    assert await helper_module.wait_for_new_message_after(client, 100, 12, lambda current: current.button_count == 1) is response
    assert client.calls == [
        (100, 12, 12, helper_module.NEW_MESSAGE_PAGE_SIZE, True),
        (100, 12, 32, helper_module.NEW_MESSAGE_PAGE_SIZE, True),
        (100, 12, 52, helper_module.NEW_MESSAGE_PAGE_SIZE, True),
        (100, 12, 72, helper_module.NEW_MESSAGE_PAGE_SIZE, True),
    ]
    assert waits == [1.0]


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
async def test_private_response_passes_its_remaining_deadline_to_exact_message_state(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = StateMessage(button_count=0)
    client = StateClient([expected])
    received_timeouts: list[float] = []

    async def wait_for_slot(_, *, cap: float) -> None:
        assert cap == 65.0

    async def trigger() -> None:
        return None

    async def receive(timeout: float) -> StateMessage:
        received_timeouts.append(timeout)
        return await helper_module.wait_for_message_state(client, 34, 12, lambda current: current.button_count == 0, timeout=timeout)

    monotonic = iter((100.0, 100.0, 110.0, 110.0)).__next__
    monkeypatch.setattr(helper_module, "Message", StateMessage)
    monkeypatch.setattr(helper_module, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(helper_module, "wait_for_limiter_slot", wait_for_slot)

    assert await helper_module.wait_for_private_response(lambda: 0.0, trigger, receive) is expected
    assert received_timeouts == [55.0]


@pytest.mark.asyncio
async def test_wait_for_message_state_rejects_unexpected_list_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper_module, "Message", StateMessage)

    with pytest.raises(TypeError, match="list"):
        await helper_module.wait_for_message_state(StateClient([[StateMessage(button_count=0)]]), 100, 42, lambda _current: True)
