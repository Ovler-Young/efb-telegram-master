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
    MTProtoMediaLimitError,
    MTProtoRetryableError,
    MTProtoSessionOwnershipError,
    normalize_receipts,
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
        self.uploaded: object | None = None
        self.download_chunks = [b"first", b"second"]
        self.media_limit = 1024
        self.sent_files: list[tuple[object, ...]] = []
        self.response_messages: list[object] | None = None

    async def connect(self) -> None:
        self.connect_calls += 1
        self.connected = True
        self.session_path.with_suffix(".session").touch()

    async def start(self, *, bot_token: str) -> None:
        self.start_tokens.append(bot_token)

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def __call__(self, request: object) -> object:
        if self.request_error is not None:
            raise self.request_error
        self.requests.append(request)
        if type(request).__name__ == "GetConfigRequest":
            return SimpleNamespace(document_size_max=self.media_limit)
        if self.response_messages is not None:
            return SimpleNamespace(messages=self.response_messages)
        return SimpleNamespace(messages=[SimpleNamespace(id=message_id, chat_id=1) for message_id in request.ids])

    async def iter_download(self, media: object, *, request_size: int):
        assert media == "media"
        assert request_size in {512, 64 * 1024}
        for chunk in self.download_chunks:
            yield chunk

    async def upload_file(self, stream: object, *, file_size: int | None = None) -> object:
        self.uploaded = (stream, file_size)
        return "uploaded"

    async def send_file(self, chat_id: int, uploaded: object, **kwargs: object) -> object:
        self.sent_files.append((chat_id, uploaded, kwargs))
        return SimpleNamespace(id=44, chat_id=chat_id)


class FakeFloodWaitError(Exception):
    def __init__(self, seconds: int):
        self.seconds = seconds


class FakeServerError(Exception):
    pass


class LifecycleMTProto:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


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
async def test_disabled_client_never_constructs_or_connects_telethon(tmp_path: Path):
    factory_called = False

    def factory(session_path: Path, config: MTProtoConfig) -> FakeClient:
        nonlocal factory_called
        factory_called = True
        return FakeClient(session_path, config)

    client = MTProtoClient(MTProtoConfig(enabled=False), "bot-token", tmp_path, client_factory=factory)

    await client.connect()
    await client.disconnect()

    assert factory_called is False


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
async def test_connect_authenticates_once_and_protects_session_files(tmp_path: Path):
    clients: list[FakeClient] = []

    def factory(session_path: Path, config: MTProtoConfig) -> FakeClient:
        client = FakeClient(session_path, config)
        clients.append(client)
        return client

    client = MTProtoClient(enabled_config(), "bot-token", tmp_path, client_factory=factory)

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
async def test_session_has_one_local_owner(tmp_path: Path):
    first = MTProtoClient(enabled_config(), "bot-token", tmp_path, client_factory=FakeClient)
    second = MTProtoClient(enabled_config(), "bot-token", tmp_path, client_factory=FakeClient)

    await first.connect()
    with pytest.raises(MTProtoSessionOwnershipError):
        await second.connect()
    await first.disconnect()


@pytest.mark.asyncio
async def test_get_messages_builds_ascending_batches_of_at_most_100(tmp_path: Path):
    def request_factory(channel: object, ids: list[int]) -> object:
        return SimpleNamespace(channel=channel, ids=ids)

    client = MTProtoClient(
        enabled_config(),
        "bot-token",
        tmp_path,
        client_factory=FakeClient,
        get_messages_request_factory=request_factory,
    )
    await client.connect()

    responses = await client.get_channel_messages("channel", list(range(205, 0, -1)))

    requests = client.client.requests
    assert [request.ids for request in requests] == [list(range(1, 101)), list(range(101, 201)), list(range(201, 206))]
    assert all(len(request.ids) <= 100 for request in requests)
    assert len(responses) == 205
    await client.disconnect()


def test_get_messages_factory_uses_telethon_channels_request():
    from telethon.tl.functions.channels import GetMessagesRequest

    request = MTProtoClient._build_get_messages_request("channel", [1, 2])

    assert isinstance(request, GetMessagesRequest)
    assert request.id == [1, 2]


@pytest.mark.asyncio
async def test_request_errors_use_project_owned_retryable_types(tmp_path: Path):
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path, client_factory=FakeClient)
    await client.connect()

    client.client.request_error = FakeFloodWaitError(17)
    with pytest.raises(MTProtoFloodWaitError, match="17") as caught:
        await client.get_channel_messages("channel", [1])
    assert caught.value.retry_after == 17
    assert isinstance(translate_mtproto_error(FakeServerError()), MTProtoRetryableError)
    await client.disconnect()


@pytest.mark.asyncio
async def test_media_transfer_streams_without_buffering(tmp_path: Path):
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path, client_factory=FakeClient)
    await client.connect()

    assert [chunk async for chunk in client.iter_download("media", chunk_size=512)] == [b"first", b"second"]
    stream = object()
    assert await client.upload_stream(stream, file_size=10) == "uploaded"
    assert client.client.uploaded == (stream, 10)
    await client.disconnect()


@pytest.mark.asyncio
async def test_large_media_send_uses_uploaded_stream_and_normalizes_receipt(tmp_path: Path):
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path, client_factory=FakeClient)
    await client.connect()
    stream = Mock()
    stream.read.side_effect = AssertionError("adapter must not read the complete file")

    receipt = await client.send_media_stream(
        77, stream, file_size=512, caption="caption", reply_to=9,
        force_document=True, supports_streaming=False, silent=True,
    )

    assert receipt.chat_id == 77
    assert receipt.message_id == 44
    assert client.client.uploaded == (stream, 512)
    assert client.client.sent_files == [(77, "uploaded", {
        "caption": "caption", "parse_mode": "html", "reply_to": 9, "force_document": True,
        "supports_streaming": False, "silent": True,
    })]
    stream.read.assert_not_called()
    await client.disconnect()


@pytest.mark.asyncio
async def test_large_media_send_rejects_telegram_config_limit(tmp_path: Path):
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path, client_factory=FakeClient)
    await client.connect()
    client.client.media_limit = 128

    with pytest.raises(MTProtoMediaLimitError, match="128"):
        await client.send_media_stream(
            77, object(), file_size=129, caption="", reply_to=None,
            force_document=True, supports_streaming=False, silent=False,
        )
    assert client.client.uploaded is None
    await client.disconnect()


@pytest.mark.asyncio
async def test_large_media_download_writes_chunks_without_reading_destination(tmp_path: Path):
    class WriteOnlyDestination:
        def __init__(self) -> None:
            self.chunks: list[bytes] = []

        def write(self, chunk: bytes) -> None:
            self.chunks.append(chunk)

        def read(self, *_args: object) -> bytes:
            raise AssertionError("download must not materialize the file")

    client = MTProtoClient(
        enabled_config(), "bot-token", tmp_path, client_factory=FakeClient,
        get_messages_request_factory=lambda channel, ids: SimpleNamespace(channel=channel, ids=ids),
    )
    await client.connect()
    async def get_entity(_chat_id: int) -> object:
        return "channel"
    client.client.get_input_entity = get_entity
    client.client.response_messages = [SimpleNamespace(media="media")]
    destination = WriteOnlyDestination()

    await client.download_message_media(77, 12, destination)

    assert destination.chunks == [b"first", b"second"]
    await client.disconnect()


def test_normalize_receipts_and_retry_translation():
    peer = SimpleNamespace(channel_id=77)
    result = SimpleNamespace(messages=[SimpleNamespace(id=5, peer_id=peer), SimpleNamespace(id=6, chat_id=88)])

    assert [receipt.chat_id for receipt in normalize_receipts(result)] == [77, 88]
    assert [receipt.message_id for receipt in normalize_receipts(result)] == [5, 6]
    flood = translate_mtproto_error(FakeFloodWaitError(9))
    assert isinstance(flood, MTProtoFloodWaitError)
    assert flood.retry_after == 9
