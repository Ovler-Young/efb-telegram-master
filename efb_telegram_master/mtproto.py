"""Request-only MTProto operations used alongside the Bot API client."""

import mimetypes
import os
import re
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, TypeAlias


ClientFactory: TypeAlias = Callable[[Path, "MTProtoConfig"], Any]
GetMessagesRequestFactory: TypeAlias = Callable[[object, list[int]], object]

# Telegram's MTProto configuration normally advertises this as
# ``document_size_max``.  It is the fallback used before that request is
# available, and keeps oversized-media routing bounded at 2 GiB.
PROJECT_MEDIA_LIMIT = 2 * 1024 * 1024 * 1024

_MEDIA_DEFAULT_SUFFIXES = {
    "photo": ".jpg",
    "video": ".mp4",
    "animation": ".gif",
    "document": ".bin",
}


def _media_filename(name: str, media_name: str, mime_type: Optional[str]) -> str:
    """Return a portable filename that Telethon can use for media inference."""
    if not isinstance(name, str) or not name:
        name = "upload"
    base_name = name.replace("\\", "/").rsplit("/", 1)[-1]
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", base_name).strip(".")
    stem, suffix = os.path.splitext(sanitized)
    if not stem:
        stem = "upload"
    if not suffix:
        normalized_mime = mime_type.split(";", 1)[0].strip().lower() if mime_type else ""
        suffix = mimetypes.guess_extension(normalized_mime) or _MEDIA_DEFAULT_SUFFIXES[media_name]
    return f"{stem[:200]}{suffix.lower()}"


def _telethon_media_attributes(
    file_name: str, media_name: str, mime_type: Optional[str], supports_streaming: bool
) -> list[object]:
    """Build explicit document attributes so queued media does not depend on artifact paths."""
    if media_name == "photo":
        return []
    from telethon.tl import types

    attributes: list[object] = [types.DocumentAttributeFilename(file_name)]
    is_video = media_name == "video" or (
        media_name == "animation" and isinstance(mime_type, str)
        and mime_type.split(";", 1)[0].strip().lower().startswith("video/")
    )
    if is_video:
        attributes.append(types.DocumentAttributeVideo(
            duration=0, w=1, h=1, supports_streaming=supports_streaming,
        ))
    if media_name == "animation":
        attributes.append(types.DocumentAttributeAnimated())
    return attributes

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


@dataclass(frozen=True)
class MTProtoReceipt:
    chat_id: int
    message_id: int


@dataclass(frozen=True)
class MTProtoMediaDescriptor:
    """Versioned, reopenable input for one durable MTProto media request."""

    version: int
    path: str
    file_size: int
    caption: str
    reply_to: Optional[int]
    force_document: bool
    supports_streaming: bool
    silent: bool
    media_name: str
    mime_type: Optional[str]
    file_name: str = ""

    VERSION = 1

    @classmethod
    def from_stream(
        cls,
        stream: object,
        *,
        file_size: int,
        caption: str,
        reply_to: Optional[int],
        force_document: bool,
        supports_streaming: bool,
        silent: bool,
        media_name: str,
        mime_type: Optional[str],
    ) -> "MTProtoMediaDescriptor":
        name = getattr(stream, "name", None)
        if not isinstance(name, (str, os.PathLike)):
            raise ValueError("MTProto durable media requires a path-backed file.")
        path = Path(name).expanduser().resolve(strict=True)
        descriptor = cls(
            cls.VERSION, str(path), file_size, caption, reply_to, force_document,
            supports_streaming, silent, media_name, mime_type,
            _media_filename(path.name, media_name, mime_type),
        )
        descriptor.validate()
        return descriptor

    def validate(self) -> None:
        if self.version != self.VERSION:
            raise ValueError("MTProto media descriptor has an unknown version.")
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("MTProto media descriptor requires a file path.")
        if isinstance(self.file_size, bool) or not isinstance(self.file_size, int) or self.file_size < 0:
            raise ValueError("MTProto media descriptor has an invalid file size.")
        if not isinstance(self.caption, str) or self.media_name not in {"document", "photo", "video", "animation"}:
            raise ValueError("MTProto media descriptor has invalid metadata.")
        if self.reply_to is not None and (isinstance(self.reply_to, bool) or not isinstance(self.reply_to, int)):
            raise ValueError("MTProto media descriptor has an invalid reply target.")
        if not all(isinstance(value, bool) for value in (
            self.force_document, self.supports_streaming, self.silent,
        )):
            raise ValueError("MTProto media descriptor has invalid flags.")
        if self.mime_type is not None and not isinstance(self.mime_type, str):
            raise ValueError("MTProto media descriptor has an invalid MIME type.")
        file_name = getattr(self, "file_name", "")
        if not isinstance(file_name, str):
            raise ValueError("MTProto media descriptor has an invalid file name.")
        if file_name and (
            "/" in file_name
            or "\\" in file_name
            or file_name != _media_filename(file_name, self.media_name, self.mime_type)
        ):
            raise ValueError("MTProto media descriptor has an invalid file name.")
        path = Path(self.path)
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ValueError("MTProto media descriptor cannot reopen the original file.") from error
        if (
            not path.is_absolute()
            or resolved != path
            or not path.is_file()
            or path.stat().st_size != self.file_size
        ):
            raise ValueError("MTProto media descriptor cannot reopen the original file.")

    def open(self):
        self.validate()
        return Path(self.path).open("rb")

    def media_filename(self) -> str:
        """Return the preserved name, or derive a safe legacy fallback."""
        return _media_filename(
            getattr(self, "file_name", "") or Path(self.path).name,
            self.media_name,
            self.mime_type,
        )


class MTProtoSessionOwnershipError(RuntimeError):
    """Raised when a second local client attempts to use the MTProto session."""


class MTProtoRetryableError(RuntimeError):
    """A Telethon request failure that may be retried by durable queue callers."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class MTProtoFloodWaitError(MTProtoRetryableError):
    """A Telegram-imposed request delay."""


class MTProtoNotConnectedError(MTProtoRetryableError):
    """A local availability failure raised before an MTProto request is submitted."""


class MTProtoMediaLimitError(ValueError):
    """A media transfer exceeds the configured MTProto file limit."""


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


def normalize_receipts(result: object) -> tuple[MTProtoReceipt, ...]:
    """Extract message identifiers from Telethon response objects without Bot API types."""
    messages = getattr(result, "messages", result)
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        messages = (messages,)

    receipts: list[MTProtoReceipt] = []
    for message in messages:
        message_id = getattr(message, "id", None)
        chat_id = getattr(message, "chat_id", None)
        peer = getattr(message, "peer_id", None)
        if chat_id is None and peer is not None:
            for attribute in ("channel_id", "chat_id", "user_id"):
                chat_id = getattr(peer, attribute, None)
                if chat_id is not None:
                    break
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise ValueError("MTProto response message has no integer id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, int):
            raise ValueError("MTProto response message has no integer chat id")
        receipts.append(MTProtoReceipt(chat_id=chat_id, message_id=message_id))
    return tuple(receipts)


class MTProtoClient:
    """Own one bot-authenticated Telethon client without subscribing to updates."""

    _SESSION_DIRECTORY = "mtproto"
    _SESSION_NAME = "bot"

    def __init__(
        self,
        config: MTProtoConfig,
        bot_token: str,
        database_base_path: Path,
        *,
        client_factory: Optional[ClientFactory] = None,
        get_messages_request_factory: Optional[GetMessagesRequestFactory] = None,
    ) -> None:
        if config.enabled and not bot_token:
            raise ValueError("MTProto requires a non-empty bot token")
        self.config = config
        self._bot_token = bot_token
        self._database_base_path = Path(database_base_path)
        self._client_factory = client_factory or self._build_telethon_client
        self._get_messages_request_factory = get_messages_request_factory or self._build_get_messages_request
        self._client: Any = None
        self._owns_session = False
        self._session_lock_fd: Optional[int] = None
        self._media_limit: Optional[int] = None

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
            self._client = self._client_factory(self.session_path, self.config)
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
        ordered_ids = sorted(set(message_ids))

        messages: list[object] = []
        for index in range(0, len(ordered_ids), 100):
            request = self._get_messages_request_factory(channel, ordered_ids[index:index + 100])
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

    async def iter_download(self, media: object, *, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        """Yield media chunks from Telethon without collecting the complete download."""
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        try:
            async for chunk in self.client.iter_download(media, request_size=chunk_size):
                yield bytes(chunk)
        except BaseException as error:
            translated = translate_mtproto_error(error)
            if translated is error:
                raise
            raise translated from error

    async def upload_stream(
        self, stream: object, *, file_size: Optional[int] = None, file_name: Optional[str] = None
    ) -> object:
        """Pass a caller-owned stream to Telethon without reading it into adapter memory."""
        try:
            return await self.client.upload_file(stream, file_size=file_size, file_name=file_name)
        except BaseException as error:
            translated = translate_mtproto_error(error)
            if translated is error:
                raise
            raise translated from error

    async def media_limit(self) -> int:
        """Return Telegram's advertised file limit, with a bounded fallback."""
        if self._media_limit is not None:
            return self._media_limit
        try:
            from telethon.tl.functions.help import GetConfigRequest

            config = await self.client(GetConfigRequest())
            value = getattr(config, "document_size_max", None)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                self._media_limit = min(value, PROJECT_MEDIA_LIMIT)
                return self._media_limit
        except ImportError:
            pass
        except MTProtoNotConnectedError:
            raise
        except BaseException as error:
            translated = translate_mtproto_error(error)
            if translated is not error:
                raise translated from error
        self._media_limit = PROJECT_MEDIA_LIMIT
        return self._media_limit

    async def send_media_stream(
        self,
        chat_id: int,
        stream: object,
        *,
        file_size: int,
        caption: str,
        reply_to: Optional[int],
        force_document: bool,
        supports_streaming: bool,
        silent: bool,
        media_name: Optional[str] = None,
        mime_type: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> MTProtoReceipt:
        """Upload a stream and send it, retaining only Telethon's input-file handle."""
        limit = await self.media_limit()
        if file_size > limit:
            raise MTProtoMediaLimitError(
                f"Attachment is {file_size} bytes; MTProto allows at most {limit} bytes."
            )
        uploaded = await self.upload_stream(stream, file_size=file_size, file_name=file_name)
        try:
            send_kwargs: dict[str, object] = {
                "caption": caption,
                "parse_mode": "html",
                "reply_to": reply_to,
                "force_document": force_document,
                "supports_streaming": supports_streaming,
                "silent": silent,
            }
            if file_name is not None:
                if media_name not in {"document", "photo", "video", "animation"}:
                    raise ValueError("MTProto media has an invalid media name.")
                send_kwargs["mime_type"] = mime_type
                send_kwargs["attributes"] = _telethon_media_attributes(
                    file_name, media_name, mime_type, supports_streaming,
                )
            result = await self.client.send_file(
                chat_id,
                uploaded,
                **send_kwargs,
            )
        except BaseException as error:
            translated = translate_mtproto_error(error)
            if translated is error:
                raise
            raise translated from error
        receipts = normalize_receipts(result)
        if len(receipts) != 1:
            raise ValueError("MTProto media send returned an unexpected number of receipts")
        return receipts[0]

    async def send_media_descriptor(self, chat_id: int, descriptor: MTProtoMediaDescriptor) -> MTProtoReceipt:
        """Reopen a queued media file only for the duration of its streamed upload."""
        descriptor.validate()
        with descriptor.open() as stream:
            return await self.send_media_stream(
                chat_id,
                stream,
                file_size=descriptor.file_size,
                caption=descriptor.caption,
                reply_to=descriptor.reply_to,
                force_document=descriptor.force_document,
                supports_streaming=descriptor.supports_streaming,
                silent=descriptor.silent,
                media_name=descriptor.media_name,
                mime_type=descriptor.mime_type,
                file_name=descriptor.media_filename(),
            )

    async def download_message_media(self, chat_id: int, message_id: int, destination: object) -> None:
        """Write a message's media to a caller-owned file one Telethon chunk at a time."""
        channel = await self.get_input_channel(chat_id)
        messages = await self.get_channel_messages(channel, [message_id])
        if len(messages) != 1 or getattr(messages[0], "media", None) is None:
            raise ValueError("MTProto message has no downloadable media")
        write = getattr(destination, "write", None)
        if not callable(write):
            raise TypeError("MTProto download destination must provide write()")
        async for chunk in self.iter_download(getattr(messages[0], "media")):
            write(chunk)

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

    @staticmethod
    def _build_get_messages_request(channel: object, message_ids: list[int]) -> object:
        from telethon.tl.functions.channels import GetMessagesRequest

        return GetMessagesRequest(channel=channel, id=message_ids)
