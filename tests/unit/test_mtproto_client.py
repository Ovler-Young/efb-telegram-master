import asyncio
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master.runtime.bot_manager import TelegramBotManager
from efb_telegram_master.runtime.mtproto import (
    MAX_SCAN_CONCURRENCY,
    MTProtoClient,
    MTProtoConfig,
    MTProtoRetryableError,
    MTProtoSessionOwnershipError,
    translate_mtproto_error,
)
from tests.unit.mtproto_support import FakeClient


class LifecycleMTProto:
    enabled = True
    connected = False

    def __init__(self, connect_error: BaseException | None = None) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connect_error = connect_error

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False


class LifecycleScanScheduler:
    def __init__(self) -> None:
        self.resume_calls = 0

    def resume(self) -> None:
        self.resume_calls += 1


class LifecycleAPI:
    def __init__(self, auxiliary: Mock) -> None:
        self.bot_pool = SimpleNamespace(bots=[auxiliary])

    def send_message(self, _chat_id: int, _text: str, **_kwargs: object) -> None:
        return None


class LifecycleRuntime:
    def __init__(self) -> None:
        self.async_runtime = Mock()


def enabled_config() -> MTProtoConfig:
    return MTProtoConfig.from_mapping({"enabled": True, "api_id": 123, "api_hash": "hash"})


def test_config_defaults_to_disabled_and_rejects_invalid_enabled_values():
    assert MTProtoConfig.from_mapping(None) == MTProtoConfig(enabled=False)
    assert MTProtoConfig.from_mapping({"enabled": True, "api_id": 123, "api_hash": "hash", "scan_concurrency": 2}).scan_concurrency == 2
    assert MTProtoConfig.from_mapping({"enabled": True, "api_id": 123, "api_hash": "hash", "scan_concurrency": MAX_SCAN_CONCURRENCY}).scan_concurrency == MAX_SCAN_CONCURRENCY
    for config in (
        {"enabled": "yes"},
        {"enabled": True, "api_id": 0, "api_hash": "hash"},
        {"enabled": True, "api_id": 123, "api_hash": ""},
        {"enabled": True, "api_id": 123, "api_hash": "hash", "scan_ceiling": 0},
        {"enabled": True, "api_id": 123, "api_hash": "hash", "scan_concurrency": 0},
        {"enabled": True, "api_id": 123, "api_hash": "hash", "scan_concurrency": MAX_SCAN_CONCURRENCY + 1},
    ):
        with pytest.raises(ValueError):
            MTProtoConfig.from_mapping(config)


def test_telethon_factory_disables_entity_storage_and_updates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    created: dict[str, object] = {}

    class FakeSession:
        def __init__(self, value: str):
            created["session_path"] = value
            self.save_entities = True
            created["session"] = self

    class FakeTelegramClient:
        def __init__(self, session: FakeSession, api_id: int, api_hash: str, **kwargs: object):
            created["client"] = (session, api_id, api_hash, kwargs)

    telethon_module = ModuleType("telethon")
    telethon_module.TelegramClient = FakeTelegramClient
    sessions_module = ModuleType("telethon.sessions")
    sessions_module.SQLiteSession = FakeSession
    monkeypatch.setitem(sys.modules, "telethon", telethon_module)
    monkeypatch.setitem(sys.modules, "telethon.sessions", sessions_module)

    MTProtoClient._build_telethon_client(tmp_path / "bot", enabled_config())

    assert created["session"].save_entities is False
    assert created["client"][3] == {"receive_updates": False, "sequential_updates": False}


@pytest.mark.asyncio
async def test_bot_lifecycle_starts_and_stops_the_request_only_client():
    mtproto = LifecycleMTProto()
    auxiliary = Mock()
    scan_scheduler = LifecycleScanScheduler()
    service = object.__new__(TelegramBotManager)
    service.mtproto = mtproto
    service.msglog_scan = scan_scheduler
    service.api = LifecycleAPI(auxiliary)
    service.logger = Mock()
    runtime = LifecycleRuntime()

    await service.runtime_started(runtime)
    await service.runtime_stopped(runtime)

    assert (mtproto.connect_calls, mtproto.disconnect_calls) == (1, 1)
    auxiliary.bind_runtime.assert_called_once_with(runtime.async_runtime)
    assert scan_scheduler.resume_calls == 1


@pytest.mark.asyncio
async def test_bot_lifecycle_keeps_polling_running_when_another_process_owns_mtproto_session():
    mtproto = LifecycleMTProto(MTProtoSessionOwnershipError("session owned"))
    auxiliary = Mock()
    scan_scheduler = LifecycleScanScheduler()
    service = object.__new__(TelegramBotManager)
    service.mtproto = mtproto
    service.msglog_scan = scan_scheduler
    service.api = LifecycleAPI(auxiliary)
    service.logger = Mock()
    runtime = LifecycleRuntime()

    await service.runtime_started(runtime)

    auxiliary.bind_runtime.assert_called_once_with(runtime.async_runtime)
    assert scan_scheduler.resume_calls == 0
    service.logger.warning.assert_called_once()
    assert service.logger.warning.call_args.args[0] == "MTProto startup is unavailable; MsgLog ingestion remains pending (%s)."

    mtproto.connect_error = None
    await service.runtime_started(runtime)

    assert mtproto.connect_calls == 2
    assert scan_scheduler.resume_calls == 1


@pytest.mark.asyncio
async def test_mtproto_reconnects_an_existing_disconnected_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(FakeClient))
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path)
    await client.connect()
    existing_client = client.client
    existing_client.connected = False

    await client.connect()

    assert client.client is existing_client
    assert existing_client.connect_calls == 2
    await client.disconnect()


@pytest.mark.asyncio
async def test_mtproto_serializes_concurrent_reconnects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    reconnect_started = asyncio.Event()
    complete_reconnect = asyncio.Event()

    class BlockingClient(FakeClient):
        async def connect(self) -> None:
            self.connect_calls += 1
            if self.connect_calls == 2:
                reconnect_started.set()
                await complete_reconnect.wait()
            self.connected = True

    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(BlockingClient))
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path)
    await client.connect()
    client.client.connected = False

    first = asyncio.create_task(client.connect())
    await reconnect_started.wait()
    second = asyncio.create_task(client.connect())
    complete_reconnect.set()
    await asyncio.gather(first, second)

    assert client.client.connect_calls == 2
    await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX session permissions and file locking")
async def test_mtproto_session_has_one_owner_and_protects_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    clients: list[FakeClient] = []

    def factory(session_path: Path, config: MTProtoConfig) -> FakeClient:
        client = FakeClient(session_path, config)
        clients.append(client)
        return client

    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(factory))
    first = MTProtoClient(enabled_config(), "bot-token", tmp_path)
    second = MTProtoClient(enabled_config(), "bot-token", tmp_path)

    await first.connect()
    await first.connect()
    with pytest.raises(MTProtoSessionOwnershipError):
        await second.connect()
    await second.disconnect()
    with pytest.raises(MTProtoSessionOwnershipError):
        await MTProtoClient(enabled_config(), "bot-token", tmp_path).connect()

    session_directory = tmp_path / "mtproto"
    assert clients[0].connect_calls == 1
    assert os.stat(session_directory).st_mode & 0o777 == 0o700
    assert os.stat(session_directory / "bot.session").st_mode & 0o777 == 0o600
    assert os.stat(session_directory / "owner.lock").st_mode & 0o777 == 0o600

    await first.disconnect()
    await first.disconnect()
    await second.connect()
    assert clients[0].disconnect_calls == 1
    await second.disconnect()


@pytest.mark.asyncio
async def test_mtproto_connect_failure_releases_session_ownership(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    class FailingClient(FakeClient):
        async def connect(self) -> None:
            await super().connect()
            raise ConnectionError("unavailable")

    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(FailingClient))
    failing = MTProtoClient(enabled_config(), "bot-token", tmp_path)

    with pytest.raises(MTProtoRetryableError, match="unavailable"):
        await failing.connect()

    if os.name == "posix":
        assert os.stat(tmp_path / "mtproto" / "bot.session").st_mode & 0o777 == 0o600

    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(FakeClient))
    recovered = MTProtoClient(enabled_config(), "bot-token", tmp_path)
    await recovered.connect()
    await recovered.disconnect()


def test_telethon_retryable_errors_use_the_base_exception():
    class FakeServerError(Exception):
        pass

    assert isinstance(translate_mtproto_error(FakeServerError()), MTProtoRetryableError)
