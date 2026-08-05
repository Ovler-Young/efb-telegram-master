import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master import TelegramChannel
from efb_telegram_master.mtproto import (
    MTProtoClient,
    MTProtoConfig,
    MTProtoRetryableError,
    translate_mtproto_error,
)


class FakeClient:
    def __init__(self, session_path: Path, _config: MTProtoConfig):
        self.connected = False
        self.requests: list[object] = []
        self.session_path = session_path

    async def connect(self) -> None:
        self.connected = True

    async def start(self, *, bot_token: str) -> None:
        assert bot_token == "bot-token"

    async def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        return SimpleNamespace(messages=[SimpleNamespace(id=message_id) for message_id in request.id])


class LifecycleMTProto:
    enabled = True
    connected = False

    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False


def enabled_config() -> MTProtoConfig:
    return MTProtoConfig.from_mapping({"enabled": True, "api_id": 123, "api_hash": "hash"})


def test_config_defaults_to_disabled_and_rejects_invalid_enabled_values():
    assert MTProtoConfig.from_mapping(None) == MTProtoConfig(enabled=False)
    for config in (
        {"enabled": "yes"},
        {"enabled": True, "api_id": 0, "api_hash": "hash"},
        {"enabled": True, "api_id": 123, "api_hash": ""},
        {"enabled": True, "api_id": 123, "api_hash": "hash", "scan_ceiling": 0},
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
    channel = SimpleNamespace(
        bot_manager=SimpleNamespace(bot_pool=SimpleNamespace(bots=[auxiliary])),
        mtproto=mtproto,
        chat_binding=SimpleNamespace(resume_pending_msglog_ingestions=Mock()),
        logger=Mock(),
    )
    runtime = SimpleNamespace(async_runtime=Mock())

    await TelegramChannel._telegram_runtime_started(channel, runtime)
    await TelegramChannel._telegram_runtime_stopped(channel, runtime)

    assert (mtproto.connect_calls, mtproto.disconnect_calls) == (1, 1)
    auxiliary.bind_runtime.assert_called_once_with(runtime.async_runtime)
    channel.chat_binding.resume_pending_msglog_ingestions.assert_called_once_with()


@pytest.mark.asyncio
async def test_get_messages_builds_ascending_batches_of_at_most_100(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(FakeClient))
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path)
    await client.connect()

    responses = await client.get_channel_messages("channel", list(range(205, 0, -1)))

    assert [request.id for request in client.client.requests] == [
        list(range(1, 101)),
        list(range(101, 201)),
        list(range(201, 206)),
    ]
    assert len(responses) == 205
    await client.disconnect()


def test_telethon_retryable_errors_use_the_base_exception():
    class FakeServerError(Exception):
        pass

    assert isinstance(translate_mtproto_error(FakeServerError()), MTProtoRetryableError)
