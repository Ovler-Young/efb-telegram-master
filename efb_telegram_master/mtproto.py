"""Request-only MTProto operations used alongside the Bot API client."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

@dataclass(frozen=True)
class MTProtoConfig:
    """Validated configuration for the optional request-only MTProto client."""

    enabled: bool
    api_id: Optional[int] = None
    api_hash: Optional[str] = None
    scan_ceiling: int = 100_000

    @classmethod
    def from_mapping(cls, value: object) -> "MTProtoConfig":
        if value is None:
            return cls(enabled=False)
        if not isinstance(value, Mapping):
            raise ValueError("mtproto must be a mapping")

        enabled = value.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("mtproto.enabled must be a boolean")
        if not enabled:
            return cls(enabled=False)

        api_id = value.get("api_id")
        if isinstance(api_id, bool) or not isinstance(api_id, int) or api_id <= 0:
            raise ValueError("mtproto.api_id must be a positive integer when enabled")
        api_hash = value.get("api_hash")
        if not isinstance(api_hash, str) or not api_hash.strip():
            raise ValueError("mtproto.api_hash must be a non-empty string when enabled")
        scan_ceiling = value.get("scan_ceiling", 100_000)
        if isinstance(scan_ceiling, bool) or not isinstance(scan_ceiling, int) or scan_ceiling <= 0:
            raise ValueError("mtproto.scan_ceiling must be a positive integer")
        return cls(enabled=True, api_id=api_id, api_hash=api_hash, scan_ceiling=scan_ceiling)


class MTProtoRetryableError(RuntimeError):
    """A Telethon request failure that can be retried by the MsgLog scan."""


def translate_mtproto_error(error: BaseException) -> BaseException:
    """Map Telethon transport and rate-limit failures to adapter-owned errors."""
    error_name = type(error).__name__
    if error_name.endswith("FloodWaitError"):
        seconds = getattr(error, "seconds", None)
        message = f"MTProto FloodWait: {seconds} seconds" if isinstance(seconds, (int, float)) else str(error)
        return MTProtoRetryableError(message)
    retryable_suffixes = (
        "ServerError",
        "RpcCallFailError",
        "TimedOutError",
        "InterdcCallError",
        "InterdcCallRichError",
    )
    if error_name.endswith(retryable_suffixes) or isinstance(error, (ConnectionError, TimeoutError, OSError)):
        return MTProtoRetryableError(str(error))
    return error


class MTProtoClient:
    """Own one bot-authenticated Telethon client without subscribing to updates."""

    _SESSION_DIRECTORY = "mtproto"
    _SESSION_NAME = "bot"

    def __init__(
        self,
        config: MTProtoConfig,
        bot_token: str,
        database_base_path: Path,
    ) -> None:
        if config.enabled and not bot_token:
            raise ValueError("MTProto requires a non-empty bot token")
        self.config = config
        self._bot_token = bot_token
        self._database_base_path = Path(database_base_path)
        self._client: Any = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def connected(self) -> bool:
        is_connected = getattr(self._client, "is_connected", None)
        return self._client is not None and (not callable(is_connected) or bool(is_connected()))

    @property
    def client(self) -> Any:
        if not self.connected:
            raise MTProtoRetryableError("MTProto client is not connected")
        return self._client

    @property
    def session_directory(self) -> Path:
        return self._database_base_path / self._SESSION_DIRECTORY

    @property
    def session_path(self) -> Path:
        return self.session_directory / self._SESSION_NAME

    async def connect(self) -> None:
        if not self.enabled or self._client is not None:
            return
        self.session_directory.mkdir(parents=True, exist_ok=True)
        try:
            self._client = self._build_telethon_client(self.session_path, self.config)
            await self._client.connect()
            await self._client.start(bot_token=self._bot_token)
        except BaseException as error:
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except BaseException:
                    pass
            self._client = None
            translated = translate_mtproto_error(error)
            if translated is error:
                raise
            raise translated from error

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            is_connected = getattr(self._client, "is_connected", None)
            if not callable(is_connected) or is_connected():
                await self._client.disconnect()
        finally:
            self._client = None

    async def get_channel_messages(self, channel: object, message_ids: Sequence[int]) -> list[object]:
        """Request channel messages in ascending batches accepted by channels.getMessages."""
        if not self.enabled:
            raise RuntimeError("MTProto is disabled")
        if any(isinstance(message_id, bool) or not isinstance(message_id, int) for message_id in message_ids):
            raise ValueError("message ids must be integers")
        from telethon.tl.functions.channels import GetMessagesRequest

        ordered_ids = sorted(set(message_ids))

        messages: list[object] = []
        for index in range(0, len(ordered_ids), 100):
            request = GetMessagesRequest(channel=channel, id=ordered_ids[index:index + 100])
            try:
                response = await self.client(request)
            except BaseException as error:
                translated = translate_mtproto_error(error)
                if translated is error:
                    raise
                raise translated from error
            response_messages = getattr(response, "messages", ())
            messages.extend(response_messages)
        return messages

    async def get_input_channel(self, chat_id: int) -> object:
        """Resolve a channel peer without reading message history."""
        try:
            return await self.client.get_input_entity(chat_id)
        except BaseException as error:
            translated = translate_mtproto_error(error)
            if translated is error:
                raise
            raise translated from error

    @staticmethod
    def _build_telethon_client(session_path: Path, config: MTProtoConfig) -> Any:
        from telethon import TelegramClient
        from telethon.sessions import SQLiteSession

        session = SQLiteSession(str(session_path))
        session.save_entities = False
        return TelegramClient(
            session,
            config.api_id,
            config.api_hash,
            receive_updates=False,
            sequential_updates=False,
        )
