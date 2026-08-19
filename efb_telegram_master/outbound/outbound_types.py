from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TypeAlias

import telegram

logger = logging.getLogger(__name__)

TelegramArgs: TypeAlias = tuple[object, ...]
TelegramKwargs: TypeAlias = dict[str, object]


class QueueError(RuntimeError):
    pass


class QueueEnqueueError(QueueError):
    pass


class SchedulerStoppedError(QueueError):
    pass


class ExecutorSubmitError(QueueError):
    pass


class OutboundShutdownTimeout(QueueError):
    pass


class OutboundLifecycle(Enum):
    RUNNING = "running"
    STOPPING = "stopping"
    QUIESCENT = "quiescent"
    FINALIZED = "finalized"


@dataclass
class UploadCleanup:
    paths: tuple[str, ...] = ()
    _cleaned: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def claim_paths(self) -> tuple[str, ...]:
        with self._lock:
            if self._cleaned:
                return ()
            self._cleaned = True
            return self.paths


def rewind_uploads(args: Sequence[object], kwargs: Mapping[str, object]) -> None:
    seen: set[int] = set()

    def visit(value: object) -> None:
        if id(value) in seen:
            return
        seen.add(id(value))
        seek = getattr(value, "seek", None)
        if callable(seek):
            seek(0)
        elif isinstance(value, telegram.InputFile):
            visit(value.input_file_content)
        elif isinstance(value, telegram.InputMedia):
            visit(value.media)
            thumbnail = getattr(value, "thumbnail", None)
            if thumbnail is not None:
                visit(thumbnail)
        elif isinstance(value, Mapping):
            for nested in value.values():
                visit(nested)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for nested in value:
                visit(nested)

    for value in (*args, *kwargs.values()):
        visit(value)


def cleanup_upload_paths(cleanup: UploadCleanup) -> None:
    for path in set(cleanup.claim_paths()):
        try:
            os.unlink(path)
        except OSError as error:
            logger.warning("Failed to remove queued upload %s (%s).", path, type(error).__name__)


@dataclass(frozen=True)
class QueueRequest:
    operation: str
    args: TelegramArgs
    kwargs: TelegramKwargs
    telegram_chat_id: int
    slave_id: Optional[str] = None
    required_sender_bot_id: Optional[str] = None
    cleanup: UploadCleanup = field(default_factory=UploadCleanup)


@dataclass(frozen=True)
class QueuedCall:
    operation: str
    args: TelegramArgs
    kwargs: TelegramKwargs
    telegram_chat_id: int
    slave_id: Optional[str]
    required_sender_bot_id: Optional[str]
    cleanup: UploadCleanup = field(default_factory=UploadCleanup)


@dataclass(frozen=True)
class SenderSelection:
    sender: object
    sender_bot_id: Optional[str]


@dataclass(frozen=True)
class SendReceipt:
    message: object
    sender_bot_id: Optional[str] = None

    def __getattr__(self, item: str) -> object:
        return getattr(self.message, item)

    def __bool__(self) -> bool:
        return self.message is not None


QueueFuture: TypeAlias = Future[SendReceipt]


@dataclass(frozen=True)
class SenderDecision:
    selection: Optional[SenderSelection]
    retry_at: Optional[float] = None
    error: Optional[str] = None
