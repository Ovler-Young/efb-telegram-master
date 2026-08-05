"""Request-only MTProto operations used alongside the Bot API client."""

import os
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_session_owners: set[Path] = set()
_session_owners_lock = threading.Lock()


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


class MTProtoSessionOwnershipError(RuntimeError):
    """Raised when a second local client attempts to use the MTProto session."""


class MTProtoRetryableError(RuntimeError):
    """A Telethon request failure that can be retried by the MsgLog scan."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class MTProtoFloodWaitError(MTProtoRetryableError):
    """A Telegram-imposed request delay."""


class MTProtoNotConnectedError(MTProtoRetryableError):
    """A local availability failure raised before an MTProto request is submitted."""


def translate_mtproto_error(error: BaseException) -> BaseException:
    """Map Telethon transport and rate-limit failures to adapter-owned errors."""
    error_name = type(error).__name__
    if error_name.endswith("FloodWaitError"):
        seconds = getattr(error, "seconds", None)
        retry_after = float(seconds) if isinstance(seconds, (int, float)) else None
        message = f"MTProto FloodWait: {retry_after} seconds" if retry_after is not None else str(error)
        return MTProtoFloodWaitError(message, retry_after=retry_after)
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
        self._owns_session = False
        self._session_lock_fd: Optional[int] = None

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
            raise MTProtoNotConnectedError("MTProto client is not connected")
        return self._client

    @property
    def session_directory(self) -> Path:
        return self._database_base_path / self._SESSION_DIRECTORY

    @property
    def session_path(self) -> Path:
        return self.session_directory / self._SESSION_NAME

    @property
    def session_file(self) -> Path:
        return self.session_path.with_suffix(".session")

    async def connect(self) -> None:
        if not self.enabled or self._client is not None:
            return
        self._prepare_session_directory()
        self._claim_session()
        try:
            self._client = self._build_telethon_client(self.session_path, self.config)
            await self._client.connect()
            await self._client.start(bot_token=self._bot_token)
            self._protect_session_file()
        except BaseException as error:
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except BaseException:
                    pass
            self._client = None
            self._release_session()
            translated = translate_mtproto_error(error)
            if translated is error:
                raise
            raise translated from error

    async def disconnect(self) -> None:
        if self._client is None:
            self._release_session()
            return
        try:
            is_connected = getattr(self._client, "is_connected", None)
            if not callable(is_connected) or is_connected():
                await self._client.disconnect()
        finally:
            self._protect_session_file()
            self._client = None
            self._release_session()

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


    def _prepare_session_directory(self) -> None:
        self.session_directory.mkdir(parents=True, exist_ok=True)
        self._chmod(self.session_directory, 0o700)

    def _protect_session_file(self) -> None:
        if self.session_file.exists():
            self._chmod(self.session_file, 0o600)

    def _claim_session(self) -> None:
        session_path = self.session_path.resolve()
        with _session_owners_lock:
            if session_path in _session_owners:
                raise MTProtoSessionOwnershipError("MTProto session is already owned by this process")
            _session_owners.add(session_path)
            self._owns_session = True
        try:
            lock_path = self.session_directory / "owner.lock"
            self._session_lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            self._chmod(lock_path, 0o600)
            if os.name == "posix":
                import fcntl

                fcntl.flock(self._session_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as error:
            self._release_session()
            raise MTProtoSessionOwnershipError("MTProto session is already owned by another process") from error

    def _release_session(self) -> None:
        if not self._owns_session:
            return
        with _session_owners_lock:
            _session_owners.discard(self.session_path.resolve())
            self._owns_session = False
        if self._session_lock_fd is not None:
            try:
                if os.name == "posix":
                    import fcntl

                    fcntl.flock(self._session_lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._session_lock_fd)
                self._session_lock_fd = None

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        if os.name == "posix":
            path.chmod(mode)

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
