import asyncio
import logging
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
