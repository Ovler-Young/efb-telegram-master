import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types
from telethon.utils import get_peer_id

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
async def test_helper_receives_watched_message_when_other_dialogs_expire(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    now = datetime.now(timezone.utc)
    bot = types.User(id=101, access_hash=1, first_name="Bot", bot=True)
    watched = types.Channel(id=202, access_hash=2, title="Watched", megagroup=True,
                            photo=types.ChatPhotoEmpty(), date=now)
    unrelated = [types.Channel(id=300 + index, access_hash=3, title="Other",
                               photo=types.ChatPhotoEmpty(), date=now)
                 for index in range(54)]
    chat_id = get_peer_id(watched)
    test_helper = helper_module.TelegramIntegrationTestHelper(
        "", 123, "offline", loop, bot.id, chats=[bot.id, chat_id],
    )
    client = test_helper.client
    client._mb_entity_cache.set_self_user(999, False, 9)
    client._mb_entity_cache.extend([bot], [watched, *unrelated])
    client.session.process_entities(types.contacts.ResolvedPeer(None, [bot], [watched, *unrelated]))
    client._message_box.set_state(types.updates.State(pts=1, qts=0, date=now, seq=0,
                                                     unread_count=0))

    async def discover_dialogs():
        for channel in [watched, *unrelated]:
            client._message_box.try_set_channel_state(channel.id, 1)
            if channel is not watched:
                client._message_box.map[channel.id].deadline = loop.time() - 1
        client._message_box.next_deadline = unrelated[0].id

    async def unexpected_request(*args, **kwargs):
        raise AssertionError("Unrelated channel catch-up delayed the watched message")

    monkeypatch.setattr(client, "connect", AsyncMock())
    monkeypatch.setattr(client, "get_me", AsyncMock(return_value=types.User(id=999)))
    monkeypatch.setattr(client, "get_dialogs", discover_dialogs)
    monkeypatch.setattr(client, "disconnect", AsyncMock())
    monkeypatch.setattr(client, "is_connected", lambda: True)
    monkeypatch.setattr(type(client), "__call__", unexpected_request)
    await test_helper.__aenter__()
    incoming = types.Message(id=7, peer_id=types.PeerChannel(watched.id),
                             from_id=types.PeerUser(bot.id), date=now,
                             message="Animation caption", out=False)
    await client._updates_queue.put(types.Updates(
        updates=[types.UpdateNewChannelMessage(incoming, pts=2, pts_count=1)],
        users=[bot], chats=[watched], date=now, seq=0,
    ))
    update_loop = asyncio.create_task(client._update_loop())
    try:
        received = await test_helper.wait_for_message(
            helper_module.filters.in_chats(chat_id), timeout=0.5,
        )
        assert received.raw_text == incoming.message
    finally:
        update_loop.cancel()
        await update_loop


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
        await helper_module.wait_for_private_response(
            lambda: 0.0, trigger, receive, cap=0.01
        )

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
