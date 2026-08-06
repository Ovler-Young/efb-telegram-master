"""In-memory, per-chat serialized Telegram Bot API dispatch."""

from __future__ import annotations

import collections
import io
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Optional, TypeAlias

import telegram.constants
import telegram.error
from telegram.error import RetryAfter

from .rate_limiter import SlidingWindowRateLimiter

if TYPE_CHECKING:
    from .bot_pool import BotPool


QUEUED_OPERATIONS = frozenset(
    {
        "send_message",
        "send_document",
        "send_photo",
        "send_video",
        "send_animation",
        "send_voice",
        "send_sticker",
        "copy_message",
        "edit_message_text",
        "edit_message_caption",
        "edit_message_media",
        "delete_message",
        "edit_message_reply_markup",
        "send_location",
        "send_venue",
        "create_forum_topic",
        "edit_forum_topic",
        "reopen_forum_topic",
        "set_chat_title",
        "set_chat_photo",
        "pin_chat_message",
        "set_chat_description",
    }
)

_INTERNAL_KWARGS = frozenset({"prefix", "suffix", "_sender_bot_id", "_slave_id", "_force_main_bot"})
_CONTENT_SPECS = {
    "send_message": ("text", 1, int(telegram.constants.MessageLimit.MAX_TEXT_LENGTH)),
    "edit_message_text": ("text", 0, int(telegram.constants.MessageLimit.MAX_TEXT_LENGTH)),
    "send_voice": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "send_video": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "send_document": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "send_animation": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "send_photo": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "edit_message_caption": ("caption", 3, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
}

TelegramArgs: TypeAlias = tuple[object, ...]
TelegramKwargs: TypeAlias = dict[str, object]
QueueFuture: TypeAlias = Future["SendReceipt"]


class QueueError(RuntimeError):
    pass


class QueueEnqueueError(QueueError):
    pass


class SchedulerStoppedError(QueueError):
    pass


class ExecutorSubmitError(QueueError):
    pass


@dataclass(frozen=True)
class QueueRequest:
    operation: str
    args: TelegramArgs
    kwargs: TelegramKwargs
    telegram_chat_id: int
    slave_id: Optional[str] = None
    required_sender_bot_id: Optional[str] = None


@dataclass(frozen=True)
class QueuedCall:
    operation: str
    args: TelegramArgs
    kwargs: TelegramKwargs
    telegram_chat_id: int
    slave_id: Optional[str]
    required_sender_bot_id: Optional[str]


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


@dataclass
class _PendingCall:
    call: QueuedCall
    waiter: QueueFuture
    retry_at: float = 0.0


@dataclass
class _SubmittedCall:
    pending: _PendingCall
    selection: SenderSelection


@dataclass(frozen=True)
class SenderDecision:
    selection: Optional[SenderSelection]
    retry_at: Optional[float] = None
    error: Optional[str] = None


def retry_after_seconds(error: RetryAfter) -> float:
    value = error.retry_after
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


def _strip_private_queue_metadata(kwargs: Mapping[str, object]) -> TelegramKwargs:
    return {key: value for key, value in kwargs.items() if key not in _INTERNAL_KWARGS}


class SenderPolicy:
    """Choose available senders and enforce their rate limits and cooldowns."""

    MEMBERSHIP_RECHECK_SECONDS = 0.25

    def __init__(self, main_bot: object, bot_pool: Optional[BotPool], main_rate_limiter: SlidingWindowRateLimiter) -> None:
        self._main_bot = main_bot
        self._bot_pool = bot_pool
        self._main_rate_limiter = main_rate_limiter
        self._cooldowns: dict[tuple[Optional[str], int], float] = {}

    def select(self, call: QueuedCall, now: float) -> SenderDecision:
        required = call.required_sender_bot_id
        if required == "__main__":
            return self._available(SenderSelection(self._main_bot, None), call.telegram_chat_id, now)
        if required is not None:
            auxiliary = self._bot_pool.get_bot_by_id(required) if self._bot_pool else None
            if auxiliary is None or auxiliary.disabled:
                return SenderDecision(None, error="required_sender_unavailable")
            membership = auxiliary.check_membership_tri(call.telegram_chat_id)
            if membership is None:
                return SenderDecision(None, now + self.MEMBERSHIP_RECHECK_SECONDS)
            if not membership:
                return SenderDecision(None, error="required_sender_unavailable")
            return self._available(SenderSelection(auxiliary.bot, str(auxiliary.bot_id)), call.telegram_chat_id, now)

        candidates: list[tuple[int, str, SenderDecision]] = []
        main = SenderSelection(self._main_bot, None)
        candidates.append((1, "", self._available(main, call.telegram_chat_id, now)))
        membership_retry_at: Optional[float] = None
        if self._bot_pool:
            preferred = self._bot_pool.preferred_sender(call.slave_id)
            for auxiliary, membership in self._bot_pool.candidate_bots(call.telegram_chat_id):
                if membership is None:
                    deadline = now + self.MEMBERSHIP_RECHECK_SECONDS
                    membership_retry_at = deadline if membership_retry_at is None else min(membership_retry_at, deadline)
                elif membership:
                    candidate = SenderSelection(auxiliary.bot, str(auxiliary.bot_id))
                    decision = self._available(candidate, call.telegram_chat_id, now)
                    candidates.append((0 if preferred is auxiliary else 2, str(auxiliary.bot_id), decision))
        selectable = [candidate for candidate in candidates if candidate[2].selection is not None]
        if selectable:
            return min(selectable, key=lambda candidate: candidate[:2])[2]
        if membership_retry_at is not None:
            return SenderDecision(None, membership_retry_at)
        retries = [candidate[2].retry_at for candidate in candidates if candidate[2].retry_at is not None]
        return SenderDecision(None, min(retries) if retries else now + self.MEMBERSHIP_RECHECK_SECONDS)

    def _available(self, selection: SenderSelection, chat_id: int, now: float) -> SenderDecision:
        cooldown = self._cooldowns.get((selection.sender_bot_id, chat_id), 0.0)
        retry_at = max(cooldown, now + self._limiter_delay(selection, chat_id))
        return SenderDecision(selection) if retry_at <= now else SenderDecision(None, retry_at)

    def _limiter_delay(self, selection: SenderSelection, chat_id: int) -> float:
        if selection.sender_bot_id is None:
            return self._main_rate_limiter.peek_delay(chat_id)
        auxiliary = self._bot_pool.get_bot_by_id(selection.sender_bot_id) if self._bot_pool else None
        return 0.0 if auxiliary is None else auxiliary.peek_delay(chat_id)

    def acquire(self, selection: SenderSelection, chat_id: int) -> bool:
        if selection.sender_bot_id is None:
            return self._main_rate_limiter.try_acquire(chat_id)
        auxiliary = self._bot_pool.get_bot_by_id(selection.sender_bot_id) if self._bot_pool else None
        return auxiliary is not None and auxiliary.try_acquire_limits(chat_id)

    def record_retry_after(self, call: QueuedCall, error: RetryAfter, selection: SenderSelection) -> None:
        self._cooldowns[(selection.sender_bot_id, call.telegram_chat_id)] = time.monotonic() + retry_after_seconds(error)

    def cooldown_snapshot(self) -> dict[str, float]:
        now = time.monotonic()
        cooldowns = {"main": 0.0, "auxiliary": 0.0}
        for (sender_bot_id, _chat_id), deadline in self._cooldowns.items():
            kind = "main" if sender_bot_id is None else "auxiliary"
            cooldowns[kind] = max(cooldowns[kind], max(0.0, deadline - now))
        return cooldowns

    def rate_limit_occupancy_snapshot(self) -> dict[str, float]:
        occupancy = self._main_rate_limiter.occupancy_snapshot()
        if self._bot_pool:
            for scope, value in self._bot_pool.rate_limit_occupancy_snapshot().items():
                occupancy[scope] = max(occupancy[scope], value)
        return occupancy


class TelegramCallAdapter:
    """Adapt queued Telegram calls, including content fallback and sender affinity."""

    def __init__(self, bot_pool: Optional[BotPool]) -> None:
        self._bot_pool = bot_pool

    def execute(self, call: QueuedCall, selection: SenderSelection) -> SendReceipt:
        sender = selection.sender
        method = getattr(sender, call.operation)
        telegram_kwargs = _strip_private_queue_metadata(call.kwargs)
        telegram_args = call.args
        content_spec = _CONTENT_SPECS.get(call.operation)
        attachment: Optional[io.BytesIO] = None
        content_key: Optional[str] = None
        original_parse_mode = str(telegram_kwargs.get("parse_mode", "")).lower()
        if content_spec is not None:
            content_key, content_index, content_limit = content_spec
            full_content, positional = self._content_argument(telegram_args, telegram_kwargs, content_key, content_index)
            if full_content is not None and len(full_content) >= content_limit:
                attachment_content = self._attachment_content(full_content, original_parse_mode)
                attachment = io.BytesIO(attachment_content.encode("utf-8"))
                truncated = full_content[:100] + "\n...\n" + full_content[-100:]
                if positional:
                    telegram_args = (*telegram_args[:content_index], truncated, *telegram_args[content_index + 1 :])
                else:
                    telegram_kwargs[content_key] = truncated
        try:
            result = method(*telegram_args, **telegram_kwargs)
        except telegram.error.BadRequest as error:
            if not error.message.lower().startswith("can't parse entities") or "parse_mode" not in telegram_kwargs:
                raise
            telegram_kwargs.pop("parse_mode")
            self._rewind_files(telegram_args, telegram_kwargs)
            result = method(*telegram_args, **telegram_kwargs)
        if attachment is not None and content_key is not None and getattr(result, "message_id", None) is not None:
            extension = ".md" if original_parse_mode == "markdown" else ".html" if original_parse_mode == "html" else ".txt"
            label = "Message" if content_key == "text" else "Caption"
            getattr(sender, "send_document")(
                call.telegram_chat_id,
                attachment,
                filename=f"{call.telegram_chat_id}_{result.message_id}{extension}",
                reply_to_message_id=result.message_id,
                caption=f"{label} is truncated due to its length. Full message is sent as attachment.",
            )
        if selection.sender_bot_id is not None and self._bot_pool and call.slave_id:
            self._bot_pool.record_successful_auxiliary_send(call.slave_id, selection.sender_bot_id)
        return SendReceipt(result, selection.sender_bot_id)

    @staticmethod
    def _content_argument(args: TelegramArgs, kwargs: Mapping[str, object], key: str, index: int) -> tuple[Optional[str], bool]:
        content = args[index] if len(args) > index else kwargs.get(key)
        return (content if isinstance(content, str) else None, len(args) > index)

    @staticmethod
    def _attachment_content(content: str, parse_mode: str) -> str:
        if parse_mode == "html":
            return "<html><head><meta charset='utf-8'></head><body><pre style='white-space:pre-wrap'>" + content + "</pre></body></html>"
        return content

    @staticmethod
    def _rewind_files(args: TelegramArgs, kwargs: Mapping[str, object]) -> None:
        for value in (*args, *kwargs.values()):
            seek = getattr(value, "seek", None)
            if callable(seek):
                seek(0)


class OutboundQueue:
    """Schedule queued calls while preserving per-chat ordering and retry timing."""

    def __init__(
        self,
        main_bot: object,
        bot_pool: Optional[BotPool],
        main_rate_limiter: SlidingWindowRateLimiter,
        *,
        worker_count: int,
        blocking_timeout: float,
        shutdown_drain_timeout: float,
        shutdown_join_grace: float,
    ) -> None:
        self._sender_policy = SenderPolicy(main_bot, bot_pool, main_rate_limiter)
        self._call_adapter = TelegramCallAdapter(bot_pool)
        self._blocking_timeout = blocking_timeout
        self._shutdown_drain_timeout = shutdown_drain_timeout
        self._shutdown_join_grace = shutdown_join_grace
        self._executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ETM-send")
        self._pending: collections.deque[_PendingCall] = collections.deque()
        self._in_flight: dict[QueueFuture, _SubmittedCall] = {}
        self._in_flight_chats: set[int] = set()
        self._capacity = threading.BoundedSemaphore(worker_count)
        self._lock = threading.RLock()
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._started = False
        self._stopping = False
        self._resources_finalized = False
        self._worker = threading.Thread(target=self._run, name="ETM queued send worker", daemon=True)

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if self._stopping:
                raise SchedulerStoppedError("Outbound queue stopped.")
            self._started = True
        self._worker.start()

    def enqueue(self, request: QueueRequest) -> QueueFuture:
        if request.operation not in QUEUED_OPERATIONS:
            raise QueueEnqueueError(f"Unsupported queued operation: {request.operation}")
        waiter: QueueFuture = Future()
        pending = _PendingCall(
            QueuedCall(
                request.operation,
                request.args,
                dict(request.kwargs),
                request.telegram_chat_id,
                request.slave_id,
                request.required_sender_bot_id,
            ),
            waiter,
        )
        with self._lock:
            if self._stopping:
                raise SchedulerStoppedError("Outbound queue stopped.")
            self._pending.append(pending)
            self._wake_event.set()
        return waiter

    def enqueue_and_wait(self, request: QueueRequest) -> SendReceipt:
        waiter = self.enqueue(request)
        try:
            return waiter.result(timeout=self._blocking_timeout)
        except FutureTimeoutError as error:
            raise RuntimeError(f"Telegram call to chat {request.telegram_chat_id} timed out after {self._blocking_timeout:g}s") from error

    def stop(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            self._fail_pending_locked()
            self._stop_event.set()
            self._wake_event.set()
        if self._started and self._worker.is_alive():
            self._worker.join(timeout=self._shutdown_drain_timeout + self._shutdown_join_grace)
        self._finalize_resources()

    def destination_snapshot(self) -> list[tuple[str, int, Optional[float]]]:
        with self._lock:
            destinations: dict[int, list[float]] = {}
            for pending in self._pending:
                destinations.setdefault(pending.call.telegram_chat_id, []).append(pending.retry_at)
        return [(str(chat_id), len(retries), None) for chat_id, retries in destinations.items()]

    def worker_snapshot(self) -> tuple[bool, int]:
        with self._lock:
            return self._worker.is_alive(), len(self._in_flight)

    def cooldown_snapshot(self) -> dict[str, float]:
        return self._sender_policy.cooldown_snapshot()

    def rate_limit_occupancy_snapshot(self) -> dict[str, float]:
        return self._sender_policy.rate_limit_occupancy_snapshot()

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                self._harvest_completed()
                self._dispatch_once()
                self._wake_event.wait(timeout=self._sleep_timeout())
                self._wake_event.clear()
        finally:
            self._drain_in_flight()
            self._finalize_resources()

    def _sleep_timeout(self) -> float:
        with self._lock:
            deadlines = [pending.retry_at for pending in self._pending if pending.retry_at > 0]
        if not deadlines:
            return 0.25
        return max(0.0, min(0.25, min(deadlines) - time.monotonic()))

    def _dispatch_once(self) -> None:
        now = time.monotonic()
        with self._lock:
            for _ in range(len(self._pending)):
                if not self._capacity.acquire(blocking=False):
                    return
                pending = self._pending.popleft()
                if pending.retry_at > now or pending.call.telegram_chat_id in self._in_flight_chats:
                    self._pending.append(pending)
                    self._capacity.release()
                    continue
                decision = self._sender_policy.select(pending.call, now)
                if decision.error is not None:
                    pending.waiter.set_exception(QueueError(decision.error.replace("_", " ")))
                    self._capacity.release()
                    continue
                if decision.selection is None:
                    pending.retry_at = decision.retry_at or now + 0.1
                    self._pending.append(pending)
                    self._capacity.release()
                    continue
                if not self._sender_policy.acquire(decision.selection, pending.call.telegram_chat_id):
                    pending.retry_at = now + 0.1
                    self._pending.append(pending)
                    self._capacity.release()
                    continue
                try:
                    future = self._executor.submit(self._call_adapter.execute, pending.call, decision.selection)
                except BaseException as error:
                    pending.waiter.set_exception(ExecutorSubmitError(str(error)))
                    self._capacity.release()
                    continue
                self._in_flight[future] = _SubmittedCall(pending, decision.selection)
                self._in_flight_chats.add(pending.call.telegram_chat_id)

    def _harvest_completed(self) -> None:
        with self._lock:
            completed = [future for future in self._in_flight if future.done()]
            for future in completed:
                submitted = self._in_flight.pop(future)
                pending = submitted.pending
                self._in_flight_chats.discard(pending.call.telegram_chat_id)
                self._capacity.release()
                if pending.waiter.done():
                    continue
                try:
                    pending.waiter.set_result(future.result())
                except RetryAfter as error:
                    self._sender_policy.record_retry_after(pending.call, error, submitted.selection)
                    pending.retry_at = time.monotonic() + retry_after_seconds(error)
                    if not self._stopping:
                        self._pending.append(pending)
                    else:
                        pending.waiter.set_exception(SchedulerStoppedError("Outbound queue stopped."))
                except BaseException as error:
                    pending.waiter.set_exception(error)
            if completed:
                self._wake_event.set()

    def _drain_in_flight(self) -> None:
        deadline = time.monotonic() + self._shutdown_drain_timeout
        while time.monotonic() < deadline:
            self._harvest_completed()
            with self._lock:
                if not self._in_flight:
                    return
            time.sleep(0.01)
        with self._lock:
            error = SchedulerStoppedError("Outbound queue stopped.")
            for submitted in self._in_flight.values():
                if not submitted.pending.waiter.done():
                    submitted.pending.waiter.set_exception(error)

    def _fail_pending_locked(self) -> None:
        error = SchedulerStoppedError("Outbound queue stopped.")
        while self._pending:
            pending = self._pending.popleft()
            if not pending.waiter.done():
                pending.waiter.set_exception(error)

    def _finalize_resources(self) -> None:
        with self._lock:
            if self._resources_finalized:
                return
            self._resources_finalized = True
        self._executor.shutdown(wait=False)
