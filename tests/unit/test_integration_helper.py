import asyncio
import logging

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
