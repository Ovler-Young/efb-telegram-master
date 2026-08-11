import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

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


def build_event_helper() -> helper_module.TelegramIntegrationTestHelper:
    test_helper = object.__new__(helper_module.TelegramIntegrationTestHelper)
    test_helper.queue = asyncio.Queue()
    test_helper.pending_events = []
    test_helper.message_chat_map = {}
    test_helper.chats = {100}
    test_helper._temporary_chat_counts = {}
    test_helper._temporary_chat_lock = threading.Lock()
    return test_helper


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
