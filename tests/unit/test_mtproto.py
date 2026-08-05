import os
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master.mtproto import (
    MTProtoClient,
    MTProtoConfig,
    MTProtoFloodWaitError,
    MTProtoRetryableError,
    MTProtoSessionOwnershipError,
    translate_mtproto_error,
)
from efb_telegram_master.bot_manager import TelegramBotManager


class FakeClient:
    def __init__(self, session_path: Path, config: MTProtoConfig):
        self.session_path = session_path
        self.config = config
        self.connected = False
        self.connect_calls = 0
        self.start_tokens: list[str] = []
        self.disconnect_calls = 0
        self.requests: list[object] = []
        self.request_error: BaseException | None = None
        self.response_messages: list[object] | None = None
        self.connect_error: BaseException | None = None
        self.start_error: BaseException | None = None

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        self.session_path.with_suffix(".session").touch()

    async def start(self, *, bot_token: str) -> None:
        self.start_tokens.append(bot_token)
        if self.start_error is not None:
            raise self.start_error

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def __call__(self, request: object) -> object:
        if self.request_error is not None:
            raise self.request_error
        self.requests.append(request)
        if self.response_messages is not None:
            return SimpleNamespace(messages=self.response_messages)
        return SimpleNamespace(messages=[SimpleNamespace(id=message_id, chat_id=1) for message_id in request.id])


class FakeFloodWaitError(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds


class FakeServerError(Exception):
    pass


class FakeRpcCallFailError(Exception):
    pass


class FakeTimedOutError(Exception):
    pass


class FakeInterdcCallError(Exception):
    pass


class LifecycleMTProto:
    def __init__(self, connect_error: BaseException | None = None) -> None:
        self.enabled = True
        self.connected = False
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


def enabled_config() -> MTProtoConfig:
    return MTProtoConfig.from_mapping({"enabled": True, "api_id": 123, "api_hash": "hash"})


def test_config_defaults_to_disabled_and_rejects_invalid_enabled_values():
    assert MTProtoConfig.from_mapping(None) == MTProtoConfig(enabled=False)
    assert enabled_config().scan_ceiling == 100_000

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
async def test_disabled_client_never_constructs_or_connects_telethon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    factory = Mock(side_effect=AssertionError("disabled client must not construct Telethon"))
    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(factory))
    client = MTProtoClient(MTProtoConfig(enabled=False), "bot-token", tmp_path)

    await client.connect()
    await client.disconnect()

    factory.assert_not_called()


@pytest.mark.asyncio
async def test_bot_lifecycle_starts_and_stops_the_request_only_client():
    mtproto = LifecycleMTProto()
    runtime = SimpleNamespace(bind_loop=Mock(), clear_loop=Mock())
    manager = SimpleNamespace(
        _runtime=runtime,
        bot_pool=None,
        _shutdown_complete_event=threading.Event(),
        channel=SimpleNamespace(mtproto=mtproto),
        logger=Mock(),
    )

    await TelegramBotManager._post_init(manager, object())
    await TelegramBotManager._post_shutdown(manager, object())

    assert mtproto.connect_calls == 1
    assert mtproto.disconnect_calls == 1
    assert manager._shutdown_complete_event.is_set()


@pytest.mark.asyncio
async def test_bot_lifecycle_keeps_msglog_ingestion_pending_for_retryable_mtproto_startup_failure():
    mtproto = LifecycleMTProto(MTProtoFloodWaitError("wait", retry_after=17))
    runtime = SimpleNamespace(bind_loop=Mock(), clear_loop=Mock())
    chat_binding = SimpleNamespace(resume_pending_msglog_ingestions=Mock())
    logger = Mock()
    manager = SimpleNamespace(
        _runtime=runtime,
        bot_pool=None,
        _shutdown_complete_event=threading.Event(),
        channel=SimpleNamespace(mtproto=mtproto, chat_binding=chat_binding),
        logger=logger,
    )

    await TelegramBotManager._post_init(manager, object())

    runtime.bind_loop.assert_called_once()
    chat_binding.resume_pending_msglog_ingestions.assert_not_called()
    logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_bot_lifecycle_preserves_fatal_mtproto_startup_failures():
    mtproto = LifecycleMTProto(ValueError("invalid bot token"))
    manager = SimpleNamespace(
        _runtime=SimpleNamespace(bind_loop=Mock(), clear_loop=Mock()),
        bot_pool=None,
        _shutdown_complete_event=threading.Event(),
        channel=SimpleNamespace(mtproto=mtproto),
        logger=Mock(),
    )

    with pytest.raises(ValueError, match="invalid bot token"):
        await TelegramBotManager._post_init(manager, object())


@pytest.mark.asyncio
async def test_connect_authenticates_once_and_protects_session_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    clients: list[FakeClient] = []

    def factory(session_path: Path, config: MTProtoConfig) -> FakeClient:
        client = FakeClient(session_path, config)
        clients.append(client)
        return client

    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(factory))
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path)

    await client.connect()
    await client.connect()

    session_directory = tmp_path / "mtproto"
    session_file = session_directory / "bot.session"
    assert clients[0].connect_calls == 1
    assert clients[0].start_tokens == ["bot-token"]
    assert os.stat(session_directory).st_mode & 0o777 == 0o700
    assert os.stat(session_file).st_mode & 0o777 == 0o600
    assert os.stat(session_directory / "owner.lock").st_mode & 0o777 == 0o600
    assert not hasattr(clients[0], "event_handlers")

    await client.disconnect()
    assert clients[0].disconnect_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["connect", "start"])
@pytest.mark.parametrize(
    ("error_type", "expected_type", "retry_after"),
    [
        (FakeServerError, MTProtoRetryableError, None),
        (FakeRpcCallFailError, MTProtoRetryableError, None),
        (FakeTimedOutError, MTProtoRetryableError, None),
        (FakeInterdcCallError, MTProtoRetryableError, None),
        (FakeFloodWaitError, MTProtoFloodWaitError, 17),
    ],
)
async def test_connect_translates_retryable_telethon_startup_errors_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
    error_type: type[BaseException],
    expected_type: type[MTProtoRetryableError],
    retry_after: float | None,
):
    clients: list[FakeClient] = []

    def factory(session_path: Path, config: MTProtoConfig) -> FakeClient:
        client = FakeClient(session_path, config)
        setattr(client, f"{phase}_error", error_type(17) if error_type is FakeFloodWaitError else error_type())
        clients.append(client)
        return client

    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(factory))
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path)

    with pytest.raises(expected_type) as caught:
        await client.connect()

    assert caught.value.retry_after == retry_after
    assert clients[0].disconnect_calls == 1
    assert client.connected is False


@pytest.mark.asyncio
async def test_session_has_one_local_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(FakeClient))
    first = MTProtoClient(enabled_config(), "bot-token", tmp_path)
    second = MTProtoClient(enabled_config(), "bot-token", tmp_path)

    await first.connect()
    with pytest.raises(MTProtoSessionOwnershipError):
        await second.connect()
    await first.disconnect()


@pytest.mark.asyncio
async def test_get_messages_builds_ascending_batches_of_at_most_100(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(FakeClient))
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path)
    await client.connect()

    responses = await client.get_channel_messages("channel", list(range(205, 0, -1)))

    requests = client.client.requests
    assert [request.id for request in requests] == [list(range(1, 101)), list(range(101, 201)), list(range(201, 206))]
    assert all(len(request.id) <= 100 for request in requests)
    assert len(responses) == 205
    await client.disconnect()


@pytest.mark.asyncio
async def test_request_errors_use_project_owned_retryable_types(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(FakeClient))
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path)
    await client.connect()

    client.client.request_error = FakeFloodWaitError(17)
    with pytest.raises(MTProtoFloodWaitError, match="17") as caught:
        await client.get_channel_messages("channel", [1])
    assert caught.value.retry_after == 17
    assert isinstance(translate_mtproto_error(FakeServerError()), MTProtoRetryableError)
    await client.disconnect()
