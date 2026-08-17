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
    cursor = test_helper.event_cursor()
    response = SimpleNamespace(kind="response")
    await test_helper._queue_event(response)

    assert test_helper.queue.qsize() == 3
    assert await test_helper.wait_for_event(lambda event: event.kind == "response", after_cursor=cursor) is response
    assert [event.kind for event in test_helper.pending_events] == [3, 4]


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
