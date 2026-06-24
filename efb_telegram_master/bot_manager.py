# coding=utf-8
import asyncio
import collections
import html
import io
import logging
import os
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from typing import TYPE_CHECKING, Callable, Collection, Coroutine, Deque, List, Literal, Mapping, NamedTuple, Optional, ParamSpec, Protocol, TypeAlias, Tuple, TypeVar, cast
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import url2pathname
from unittest.mock import Mock, patch

import telegram.constants
import telegram.error
from retrying import retry
from telegram import File, ForumTopic, InlineKeyboardMarkup, InputFile, Update, User
from telegram import Message as TelegramMessage
from telegram.ext import Application, CallbackContext, MessageHandler, TypeHandler
from telegram.ext import _applicationbuilder as ptb_applicationbuilder
from telegram.request import HTTPXRequest

from .auxiliary_bot import AuxiliaryBot
from .bot_pool import BotPool
from .locale_mixin import LocaleMixin
from .msg_type import get_msg_type
from .ptb_compat import Filters
from .utils import TelegramChatID, TelegramMessageID, message_id_to_str


SendTarget: TypeAlias = Tuple[str, int]


class QueuedSendTask(NamedTuple):
    """Represents an in-memory FIFO send task."""
    target: SendTarget
    function: Callable
    args: tuple
    kwargs: dict
    task_id: str
    arrival_seq: int = 0
    cleanup_files: tuple[str, ...] = ()

    @property
    def slave_id(self) -> str:
        return self.target[0]

    @property
    def chat_id(self) -> int:
        return self.target[1]

if TYPE_CHECKING:
    from . import TelegramChannel

MAX_CALLBACK_QUERY_ANSWER_LENGTH = 200
P = ParamSpec("P")
T = TypeVar("T")
BotMethod: TypeAlias = Callable[..., object]
_QUEUED_DATABASE_UPDATE_DROPPED = object()


class SyncBotProtocol(Protocol):
    def __getattr__(self, item: str) -> BotMethod:
        ...


class ChatIdentifier(Protocol):
    id: int


class ReplyTarget(Protocol):
    chat: ChatIdentifier
    message_id: int


class _UnusedJobQueueStub:
    """Prevent PTB from eagerly instantiating an unused JobQueue during builder setup."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class AsyncTelegramRuntime:
    """Thread-safe bridge into the PTB 22 event loop."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._loop_thread_id: Optional[int] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._owns_loop_thread = False
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop, *, owns_thread: bool = False):
        old_loop = None
        old_thread = None
        with self._lock:
            if self._loop is loop:
                self._loop_thread_id = threading.get_ident()
                self._loop_thread = threading.current_thread()
                self._owns_loop_thread = owns_thread
                self._ready.set()
                return
            if self._owns_loop_thread and self._loop is not None:
                old_loop = self._loop
                old_thread = self._loop_thread
            self._loop = loop
            self._loop_thread_id = threading.get_ident()
            self._loop_thread = threading.current_thread()
            self._owns_loop_thread = owns_thread
            self._ready.set()
        if old_loop is not None and old_thread is not None and old_thread.ident != threading.get_ident():
            old_loop.call_soon_threadsafe(old_loop.stop)
            old_thread.join(timeout=5)

    def clear_loop(self, expected_loop: Optional[asyncio.AbstractEventLoop] = None):
        with self._lock:
            if expected_loop is not None and self._loop is not expected_loop:
                self.logger.debug(
                    "Skipping clear_loop for stale loop %r; runtime is bound to %r.",
                    expected_loop,
                    self._loop,
                )
                return
            self._loop = None
            self._loop_thread_id = None
            self._loop_thread = None
            self._owns_loop_thread = False
            self._ready.clear()

    def _ensure_background_loop(self):
        with self._lock:
            if self._ready.is_set() and self._loop is not None:
                return
            if self._loop_thread is not None and self._loop_thread.is_alive():
                return

            started = threading.Event()

            def runner():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.bind_loop(loop, owns_thread=True)
                started.set()
                try:
                    loop.run_forever()
                finally:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.close()
                    self.clear_loop(loop)

            self._loop_thread = threading.Thread(
                target=runner,
                daemon=True,
                name="ETMAsyncTelegramRuntime",
            )
            self._loop_thread.start()

        if not started.wait(timeout=30):
            raise RuntimeError("Failed to start Telegram runtime loop thread.")

    def shutdown(self):
        loop = None
        thread = None
        with self._lock:
            if self._owns_loop_thread and self._loop is not None:
                loop = self._loop
                thread = self._loop_thread
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.ident != threading.get_ident():
            thread.join(timeout=5)
        if loop is None:
            self.clear_loop()

    def call_soon(self, callback: Callable[..., object], *args: object) -> bool:
        with self._lock:
            loop = self._loop
        if loop is None:
            return False
        loop.call_soon_threadsafe(callback, *args)
        return True

    def call(self, coroutine: Coroutine[object, object, T], timeout: Optional[float] = None) -> T:
        startup_wait_timeout = 2.0
        if not self._ready.wait(timeout=startup_wait_timeout):
            self.logger.debug(
                "Telegram runtime is not ready after %.1fs; starting the background runtime loop.",
                startup_wait_timeout,
            )
            self._ensure_background_loop()
        with self._lock:
            loop = self._loop
            loop_thread_id = self._loop_thread_id
        if loop is None:
            self._ensure_background_loop()
            with self._lock:
                loop = self._loop
                loop_thread_id = self._loop_thread_id
        if loop is None:
            raise RuntimeError("Telegram runtime loop is unavailable.")
        if threading.get_ident() == loop_thread_id:
            raise RuntimeError("Synchronous bot wrapper invoked from the PTB event loop thread.")
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout)
        except FutureTimeoutError:
            future.cancel()
            raise


class SyncBotFacade:
    """Expose PTB 22 async Bot methods through synchronous wrappers."""

    def __init__(self, bot: telegram.Bot, runtime: AsyncTelegramRuntime):
        self._bot = bot
        self._runtime = runtime

    def __getattr__(self, item: str) -> BotMethod:
        attr = getattr(self._bot, item)
        if not callable(attr):
            raise AttributeError(f"{type(self._bot).__name__}.{item} is not callable")

        def wrapper(*args: object, **kwargs: object) -> object:
            return self._runtime.call(cast(Coroutine[object, object, object], attr(*args, **kwargs)))

        return wrapper


@dataclass
class QueuedSendPlaceholder:
    chat_id: int
    message_id: int
    date: int
    text: str
    task_id: str
    is_queued: bool = True
    _queued_execution_pending: bool = True
    sender_bot_id: Optional[str] = None

    def __post_init__(self):
        self.chat = Mock()
        self.chat.id = self.chat_id


@dataclass
class SendReceipt:
    message: object
    sender_bot_id: Optional[str] = None
    queued: bool = False
    task_id: Optional[str] = None
    manager: Optional["TelegramBotManager"] = None

    def __getattr__(self, item: str):
        return getattr(self.message, item)

    def __bool__(self):
        return self.message is not None

    @property
    def chat(self) -> ChatIdentifier:
        return cast(ReplyTarget, self.message).chat

    @property
    def message_id(self) -> int:
        return cast(ReplyTarget, self.message).message_id

    def reply_text(self, text: str, **kwargs):
        if self.manager is None:
            raise RuntimeError("SendReceipt is detached from TelegramBotManager.")
        return self.manager.send_message(
            self.chat.id,
            text=text,
            reply_to_message_id=self.message_id,
            **kwargs,
        )

    def reply_html(self, text: str, **kwargs):
        kwargs.setdefault("parse_mode", "HTML")
        return self.reply_text(text, **kwargs)


def _has_callback_keyboard(reply_markup) -> bool:
    """Check if a reply_markup contains InlineKeyboardButtons with callback_data."""
    if not isinstance(reply_markup, InlineKeyboardMarkup):
        return False
    for row in reply_markup.inline_keyboard:
        for button in row:
            if button.callback_data and button.callback_data != "void":
                return True
    return False


def _clone_file_argument(value):
    """Copy file-like send arguments so queued tasks don't depend on caller-owned handles."""
    if isinstance(value, InputFile):
        return InputFile(io.BytesIO(value.input_file_content), filename=value.filename)
    if hasattr(value, 'read') and hasattr(value, 'seek'):
        current_pos = None
        try:
            current_pos = value.tell()
        except (AttributeError, OSError):
            pass
        try:
            value.seek(0)
            data = value.read()
        finally:
            if current_pos is not None:
                try:
                    value.seek(current_pos)
                except OSError:
                    pass
        return io.BytesIO(data)
    return value


def _clone_media_argument(value):
    """Copy Telegram media objects that wrap caller-owned file handles."""
    if not hasattr(value, 'media'):
        return value
    kwargs = {
        'caption': getattr(value, 'caption', None),
        'parse_mode': getattr(value, 'parse_mode', None),
        'caption_entities': getattr(value, 'caption_entities', None),
    }
    optional_attrs = (
        'filename', 'has_spoiler', 'show_caption_above_media',
        'disable_content_type_detection', 'thumbnail', 'width', 'height',
        'duration', 'supports_streaming', 'performer', 'title', 'api_kwargs',
    )
    for attr in optional_attrs:
        if hasattr(value, attr):
            attr_value = getattr(value, attr)
            if attr_value:
                kwargs[attr] = _clone_file_argument(attr_value) if attr == 'thumbnail' else attr_value
    try:
        return value.__class__(_clone_file_argument(value.media), **kwargs)
    except TypeError:
        return value


class TelegramBotManager(LocaleMixin):
    """
    This is a wrapper of Telegram's message sending and editing methods.
    Used to deal with text/caption length overflow, parse_mode, document fallback, etc.

    Attributes:
        me (telegram.User): Telegram User
        admins (List[int]): List of admin user IDs.
        application (telegram.ext.Application): PTB application of the bot.
        dispatcher (telegram.ext.Application): Dispatcher-compatible application instance.
    """

    webhook = False
    logger = logging.getLogger(__name__)
    DEFAULT_SEND_WORKER_COUNT = 8
    DEFAULT_HTTPX_POOL_MULTIPLIER = 2.0
    HTTPX_POOL_MULTIPLIER_ENV = "ETM_HTTPX_POOL_MULTIPLIER"

    # Type declarations for instance attributes assigned in __init__
    application: Application
    _bot: SyncBotProtocol
    _async_bot: telegram.Bot
    me: Optional[User]
    admins: List[int]
    dispatcher: Application
    bot_pool: Optional['BotPool']
    _send_worker_stop: threading.Event
    _send_queues_lock: threading.Lock
    _cleanup_tls: threading.local
    _tls: threading.local
    _aux_recent_use: dict[int, float]

    class Decorators:
        logger = logging.getLogger(__name__)

        enable_retry = False

        @classmethod
        def exception_filter(cls, exception: Exception):
            cls.logger.exception("Exception: %s while sending request to Telegram server.", exception)
            return isinstance(exception, telegram.error.TimedOut)

        @classmethod
        def retry_on_timeout(cls, fn: Callable):
            """Infinitely retry for timed-out exceptions."""
            if not cls.enable_retry:
                return fn
            cls.logger.debug("Trying to call %s with infinite retry.", fn)
            return retry(wait_exponential_multiplier=1e3, wait_exponential_max=180e3,
                         retry_on_exception=cls.exception_filter)(fn)

        @classmethod
        def rate_limit_decorator(cls, fn: Callable):
            """Apply rate limiting and sender routing for outbound API calls."""
            @wraps(fn)
            def rate_limit_wrapper(self: 'TelegramBotManager', *args, **kwargs):
                is_edit_method = fn.__name__.startswith('edit_message_')

                # Bypass: caller already reserved a slot and set _using_bot
                if kwargs.pop('_bypass_rate_limit', False):
                    return fn(self, *args, **kwargs)

                sender_bot_id = kwargs.pop('_sender_bot_id', None)
                slave_id = kwargs.pop('_slave_id', None)
                send_mode = kwargs.pop('_send_mode', 'blocking')
                force_main_bot = kwargs.pop('_force_main_bot', False)
                force_sender_known = False
                forced_sender_bot_id = None

                chat_id = None
                if args:
                    chat_id = args[0]
                elif 'chat_id' in kwargs:
                    chat_id = kwargs['chat_id']
                has_callback = _has_callback_keyboard(kwargs.get('reply_markup'))

                if self._send_worker_stop.is_set():
                    self.logger.warning(f"Queued send worker is stopped. Not scheduling new tasks for chat {chat_id}.")
                    return None

                reply_to_message_id = kwargs.get('reply_to_message_id')
                if sender_bot_id is None and chat_id and reply_to_message_id and hasattr(self, 'channel'):
                    try:
                        target_log = self.channel.db.get_msg_log(
                            master_msg_id=message_id_to_str(
                                TelegramChatID(int(chat_id)),
                                TelegramMessageID(int(reply_to_message_id)),
                            )
                        )
                    except Exception as e:
                        self.logger.debug(
                            "Failed to resolve reply target sender for %s.%s: %s",
                            chat_id, reply_to_message_id, e,
                        )
                    else:
                        if target_log is not None:
                            force_sender_known = True
                            forced_sender_bot_id = target_log.sender_bot_id

                if sender_bot_id and self.bot_pool and not has_callback and chat_id is not None:
                    chat_id_int = int(chat_id)
                    bot, chosen_sender_bot_id, wait_seconds = self._select_forced_sender(
                        chat_id_int, sender_bot_id,
                    )
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    if chosen_sender_bot_id:
                        try:
                            with self._using_bot(bot):
                                result = fn(self, *args, **kwargs)
                            return self._make_send_receipt(result, sender_bot_id=chosen_sender_bot_id)
                        except telegram.error.Forbidden:
                            aux_bot = self.bot_pool.get_bot_by_id(chosen_sender_bot_id)
                            if aux_bot:
                                aux_bot.update_membership(chat_id_int, False)
                            self.logger.warning(
                                "Auxiliary bot %s got Forbidden in chat %s during forced-route API call; "
                                "marking it as non-member for this chat.",
                                chosen_sender_bot_id, chat_id,
                            )
                    else:
                        self.logger.warning(
                            "Auxiliary bot %s is unavailable for forced-route API call in chat %s; "
                            "falling back to main bot.",
                            sender_bot_id, chat_id,
                        )
                        self._calculate_rate_limit_delay(chat_id_int)
                        return self._make_send_receipt(fn(self, *args, **kwargs))
                    aux_bot = self.bot_pool.get_bot_by_id(sender_bot_id)
                    if aux_bot:
                        aux_bot.update_membership(chat_id_int, False)
                    wait_seconds, _, _ = self._calculate_rate_limit_delay(chat_id_int)
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    return self._make_send_receipt(fn(self, *args, **kwargs))

                if chat_id:
                    cleanup_files = getattr(self._cleanup_tls, 'pending_cleanup', [])[:]
                    self._cleanup_tls.pending_cleanup = []

                    if send_mode == 'eventual' and not is_edit_method and not has_callback and slave_id:
                        if force_sender_known:
                            kwargs = dict(kwargs)
                            kwargs['_force_sender_known'] = True
                            kwargs['_force_sender_bot_id'] = forced_sender_bot_id
                        return self._enqueue_eventual_send(
                            str(slave_id),
                            int(chat_id),
                            fn,
                            (self,) + args,
                            kwargs,
                            cleanup_files=cleanup_files,
                        )

                    message_thread_id = kwargs.get('message_thread_id')
                    if force_main_bot or has_callback or is_edit_method:
                        bot, chosen_sender_bot_id, wait_seconds = self._bot, None, 0.0
                    elif force_sender_known:
                        bot, chosen_sender_bot_id, wait_seconds = self._select_forced_sender(
                            chat_id, forced_sender_bot_id,
                        )
                    else:
                        bot, chosen_sender_bot_id, wait_seconds = self._select_sender(
                            chat_id,
                            slave_id=str(slave_id) if slave_id else None,
                            has_callback=has_callback,
                            message_thread_id=message_thread_id,
                        )
                    if chosen_sender_bot_id is None:
                        wait_seconds, _, _ = self._calculate_rate_limit_delay(chat_id)
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                    try:
                        with self._using_bot(bot):
                            result = fn(self, *args, **kwargs)
                        if chosen_sender_bot_id is not None:
                            self._record_aux_use(chat_id)
                        return self._make_send_receipt(result, sender_bot_id=chosen_sender_bot_id)
                    except telegram.error.Forbidden:
                        if chosen_sender_bot_id and self.bot_pool:
                            aux_bot = self.bot_pool.get_bot_by_id(chosen_sender_bot_id)
                            if aux_bot:
                                aux_bot.update_membership(int(chat_id), False)
                                self.logger.warning(
                                    "Auxiliary bot %s got Forbidden in chat %s during pool-route API call; "
                                    "marking it as non-member for this chat and retrying with main bot.",
                                    chosen_sender_bot_id, chat_id,
                                )
                                wait_seconds, _, _ = self._calculate_rate_limit_delay(chat_id)
                                if wait_seconds > 0:
                                    time.sleep(wait_seconds)
                                return self._make_send_receipt(fn(self, *args, **kwargs))
                        raise

                return self._make_send_receipt(fn(self, *args, **kwargs))

            return rate_limit_wrapper

        @classmethod
        def handle_rate_limit_error(cls, fn: Callable):
            """Handle Telegram flood limits.

            ``RetryAfter`` is always retried (honours ``retry_after`` seconds from Telegram).
            Broader heuristic retries for other rate-limit signals require ``retry_on_error``.
            """
            @wraps(fn)
            def rate_limit_error_handler(self: 'TelegramBotManager', *args, **kwargs):
                max_retries = 3
                skip_rate_limit_retry = kwargs.pop('_skip_rate_limit_retry', False)

                # Extract chat_id from arguments for logging
                chat_id = None
                if args:
                    chat_id = args[0]
                elif 'chat_id' in kwargs:
                    chat_id = kwargs['chat_id']

                # Get recent timestamps for debugging
                def get_timestamp_info():
                    if not (chat_id and hasattr(self, '_rate_limiter')):
                        return ""
                    chat_count, global_count = self._rate_limiter.get_counts(chat_id)
                    return f" [chat: {chat_count}/{self.CHAT_LIMIT}, global: {global_count}/{self.GLOBAL_LIMIT}]"

                for attempt in range(max_retries + 1):
                    try:
                        return fn(self, *args, **kwargs)
                    except telegram.error.RetryAfter as e:
                        if skip_rate_limit_retry:
                            raise
                        timestamp_info = get_timestamp_info()
                        if attempt >= max_retries:
                            cls.logger.error(f"Max retries exceeded for rate limit error: {e} (chat_id: {chat_id}){timestamp_info}")
                            raise

                        retry_after_value = e.retry_after
                        if isinstance(retry_after_value, timedelta):
                            retry_after = retry_after_value.total_seconds()
                        else:
                            retry_after = float(retry_after_value)
                        cls.logger.warning(f"Rate limit hit, waiting {retry_after}s before retry {attempt + 1}/{max_retries} (chat_id: {chat_id}){timestamp_info}")

                        # Use interruptible sleep for rate limit waits
                        if hasattr(self, '_send_worker_stop'):
                            # Sleep in small chunks to allow for interruption during shutdown
                            remaining_seconds = retry_after
                            while remaining_seconds > 0 and not self._send_worker_stop.is_set():
                                sleep_chunk = min(1.0, remaining_seconds)
                                time.sleep(sleep_chunk)
                                remaining_seconds -= sleep_chunk
                        else:
                            time.sleep(retry_after)
                    except telegram.error.TelegramError as e:
                        if not cls.enable_retry:
                            raise
                        if "Too Many Requests" in str(e) or "429" in str(e) or "Flood" in str(e):
                            if skip_rate_limit_retry:
                                raise
                            timestamp_info = get_timestamp_info()
                            if attempt >= max_retries:
                                cls.logger.error(f"Max retries exceeded for rate limit error: {e} (chat_id: {chat_id}){timestamp_info}")
                                raise

                            delay = 60
                            cls.logger.warning(f"Rate limit detected, waiting {delay}s before retry {attempt + 1}/{max_retries} (chat_id: {chat_id}){timestamp_info}")

                            # Use interruptible sleep for rate limit waits
                            if hasattr(self, '_send_worker_stop'):
                                # Sleep in small chunks to allow for interruption during shutdown
                                remaining_seconds = float(delay)
                                while remaining_seconds > 0 and not self._send_worker_stop.is_set():
                                    sleep_chunk = min(1.0, remaining_seconds)
                                    time.sleep(sleep_chunk)
                                    remaining_seconds -= sleep_chunk
                            else:
                                time.sleep(delay)
                        else:
                            raise

                return fn(self, *args, **kwargs)

            return rate_limit_error_handler

        @classmethod
        def caption_strip_class_on_failure(cls, fn: Callable):
            @wraps(fn)
            def caption_strip_class_on_failure_wrapper(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except telegram.error.BadRequest as e:
                    if e.message.lower().startswith("can't parse entities") and 'parse_mode' in kwargs:
                        kwargs.pop("parse_mode")
                        for i in args:
                            if callable(getattr(i, 'seek', None)):
                                i.seek(0)
                        for i in kwargs.values():
                            if callable(getattr(i, 'seek', None)):
                                i.seek(0)
                        return fn(*args, **kwargs)
                    else:
                        raise e

            return caption_strip_class_on_failure_wrapper

        @classmethod
        def caption_affix_decorator(cls, fn: Callable):
            fn = cls.caption_strip_class_on_failure(fn)

            @wraps(fn)
            def caption_affix(self, *args, **kwargs):
                prefix = kwargs.pop('prefix', '')
                suffix = kwargs.pop('suffix', '')
                text = kwargs.pop('caption', '')

                file = args[1] if len(args) >= 2 else kwargs.get('file', None)
                chat = args[0] if len(args) >= 1 else kwargs.get('chat_id', None)
                message_thread_id = kwargs.get('message_thread_id', None)

                if file:
                    is_empty = self._detect_empty_file(file, chat, text, prefix, suffix, message_thread_id)

                    if is_empty:
                        return is_empty

                prefix = (prefix and (prefix + "\n")) or prefix
                suffix = (suffix and ("\n" + suffix)) or suffix

                if str(kwargs.get('parse_mode', '')).lower() == "html":
                    prefix = html.escape(prefix)
                    suffix = html.escape(suffix)

                if len(prefix + text + suffix) >= telegram.constants.MessageLimit.CAPTION_LENGTH:
                    full_message = io.StringIO(prefix + text + suffix)
                    truncated = prefix + text[:100] + "\n…\n" + text[-100:] + suffix
                    kwargs['caption'] = truncated
                    msg = fn(self, *args, **kwargs)
                    chat_id = kwargs.get("chat_id", args[0] if len(args) > 0 else "")
                    filename = "%s_%s.txt" % (chat_id, msg.message_id)
                    self._active_bot.send_document(chat_id, full_message,
                                                   filename=filename,
                                                   reply_to_message_id=msg.message_id,
                                                   caption=self._("Caption is truncated due to its length. "
                                                                  "Full message is sent as attachment."))
                    return msg
                else:
                    kwargs['caption'] = prefix + text + suffix
                    return fn(self, *args, **kwargs)

            return caption_affix

        @classmethod
        def skip_on_rate_limit(cls, fn: Callable):
            """Skip execution silently if messages are queued or pool is in
            high-volume mode for the target chat.
            For non-essential calls like typing indicators."""
            AUX_USE_RECENCY = 5.0  # seconds

            @wraps(fn)
            def skip_wrapper(self: 'TelegramBotManager', *args, **kwargs):
                with self._send_queues_lock:
                    if any(self._send_queues.values()) or self._send_in_flight:
                        return None

                # Suppress when aux bots were recently used for this chat
                chat_id = args[0] if args else kwargs.get('chat_id')
                if chat_id and self._aux_recent_use.get(chat_id, 0) > time.time() - AUX_USE_RECENCY:
                    return None

                try:
                    return fn(self, *args, **kwargs)
                except telegram.error.RetryAfter:
                    return None

            return skip_wrapper

        @classmethod
        def retry_on_chat_migration(cls, fn: Callable):
            @wraps(fn)
            def retry_on_chat_migration_wrap(self: 'TelegramBotManager', *args, **kwargs):
                try:
                    return fn(self, *args, **kwargs)
                except telegram.error.ChatMigrated as e:
                    if 'chat_id' in kwargs:
                        chat_id = kwargs['chat_id']
                        self.channel.chat_binding.chat_migration_by_id(chat_id, e.new_chat_id)
                        kwargs['chat_id'] = e.new_chat_id
                        return fn(self, *args, **kwargs)
                    else:
                        args
                        chat_id = args[0]
                        self.channel.chat_binding.chat_migration_by_id(chat_id, e.new_chat_id)
                        args = (e.new_chat_id, *args[1:])
                        return fn(self, *args, **kwargs)

            return retry_on_chat_migration_wrap

    @classmethod
    def _default_connection_pool_size(cls, config: Mapping[str, object]) -> int:
        multiplier = cls.DEFAULT_HTTPX_POOL_MULTIPLIER
        multiplier_value = os.getenv(cls.HTTPX_POOL_MULTIPLIER_ENV)
        if multiplier_value:
            try:
                parsed_multiplier = float(multiplier_value)
            except ValueError:
                parsed_multiplier = multiplier
            if parsed_multiplier > 0:
                multiplier = parsed_multiplier
        return max(1, int(round(cls.DEFAULT_SEND_WORKER_COUNT * multiplier)))

    def __init__(self, channel: 'TelegramChannel'):
        self.channel: 'TelegramChannel' = channel
        config = self.channel.config

        req_kwargs = {
            'read_timeout': 15.0,
            'connection_pool_size': self._default_connection_pool_size(config),
        }
        conf_req_kwargs = config.get('request_kwargs')
        if isinstance(conf_req_kwargs, collections.abc.Mapping):
            req_kwargs.update(conf_req_kwargs)
        request_kwargs = self._normalize_request_kwargs(req_kwargs)
        self._request_kwargs = dict(request_kwargs)
        self._bot_identity_kwargs: dict[str, str] = {"token": config['token']}

        self.logger.debug("Setting up Telegram application and sync runtime...")
        if channel.flag('api_base_url'):
            self._bot_identity_kwargs["base_url"] = channel.flag('api_base_url')
        if channel.flag('api_base_file_url'):
            self._bot_identity_kwargs["base_file_url"] = channel.flag('api_base_file_url')
        self._local_mode = bool(channel.flag('local_tdlib_api'))
        request = self._build_request()
        get_updates_request = self._build_request()

        self._runtime = AsyncTelegramRuntime(self.logger)
        self._async_bot = self._build_bot(request=request, get_updates_request=get_updates_request)
        self._bot = SyncBotFacade(self._async_bot, self._runtime)
        with patch.object(ptb_applicationbuilder, "JobQueue", _UnusedJobQueueStub):
            self.application = (
                Application.builder()
                .bot(self._async_bot)
                # This channel does not use PTB's JobQueue features.
                .job_queue(None)
                .post_init(self._post_init)
                .post_shutdown(self._post_shutdown)
                .build()
            )

        if isinstance(config.get('webhook'), dict):
            self.logger.debug("Setting up webhook...")
            self.webhook = True
            self.logger.debug("Webhook is set...")

        self.logger.debug("Checking connection to Telegram bot API...")
        validation_bot = self._build_bot(
            request=self._build_request(),
            get_updates_request=self._build_request(),
        )
        me = self._runtime.call(validation_bot.get_me())
        assert me, "Invalid bot credential provided."
        self.me = me
        self.logger.debug("Connection to Telegram bot API is OK...")
        self.admins = config['admins']
        self.dispatcher = self.application

        # Initialize sliding window rate limiting — shared implementation
        from .rate_limiter import SlidingWindowRateLimiter
        self.GLOBAL_LIMIT = 30    # messages per second
        self.GLOBAL_WINDOW = 1.0
        self.CHAT_LIMIT = 20      # messages per minute per chat
        self.CHAT_WINDOW = 60.0
        self._rate_limiter = SlidingWindowRateLimiter(
            global_limit=self.GLOBAL_LIMIT,
            global_window=self.GLOBAL_WINDOW,
            chat_limit=self.CHAT_LIMIT,
            chat_window=self.CHAT_WINDOW,
        )

        self._cleanup_tls = threading.local()  # Thread-local for pending cleanup files
        self._tls = threading.local()  # Thread-local for bot override (_active_bot)
        self._shutdown_complete_event = threading.Event()
        self._manual_polling_stop_event: Optional[asyncio.Event] = None
        self._aux_recent_use: dict[int, float] = {}  # chat_id -> timestamp of last aux bot use
        self.logger.debug("Rate limiter initialized...")

        # Initialize auxiliary bot pool
        self.bot_pool: Optional[BotPool] = None
        aux_configs = config.get('auxiliary_bots', [])
        if aux_configs:
            self._init_bot_pool(aux_configs, config, channel)
        self.logger.debug("Bot pool initialization complete...")

        # ── Outbound send pipeline ────────────────────────────────────
        # Per-target FIFO queues + thread pool for concurrent dispatch.
        # Same (slave_id, chat_id): sends are serial (ordering guarantee).
        # Different targets: sends run in parallel threads.
        from collections import deque as _deque
        from concurrent.futures import ThreadPoolExecutor

        self._send_queues: dict[SendTarget, _deque[QueuedSendTask]] = {}
        self._send_queues_lock = threading.Lock()
        self._tasks_enqueued = 0  # monotonic counter for diagnostics
        self._send_worker_stop = threading.Event()

        # Per-target concurrency tracking
        self._send_in_flight: dict[SendTarget, tuple] = {}  # target -> (Future, task, sender_bot_id)
        self._bot_chat_disabled_until: dict[tuple, float] = {}  # (bot_id|None, chat_id) -> RetryAfter deadline

        # Per-chat send interval (200ms safety gap)
        self._last_send_by_chat: dict[int, float] = {}
        self.CHAT_SEND_INTERVAL = 0.2  # seconds

        # Thread pool for non-blocking sends
        self._send_worker_count = self.DEFAULT_SEND_WORKER_COUNT
        self._send_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=self._send_worker_count, thread_name_prefix="ETM-send",
        )

        # Rendezvous dicts for queued DB updates — with timestamps for TTL cleanup
        # task_id -> (etm_msg, old_msg_id, registered_at)
        self._pending_queued_logs: dict[str, tuple] = {}
        # task_id -> (real_tg_msg, sender_bot_id, completed_at), or a dropped marker
        self._completed_queued_results: dict[str, tuple] = {}
        self._pending_logs_lock = threading.Lock()
        self._rendezvous_ttl = 600.0          # 10 minutes
        self._rendezvous_cleanup_interval = 300.0  # 5 minutes
        self._last_rendezvous_cleanup = time.time()

        # DB write retry queue
        self._db_retry_queue: list[tuple] = []
        self._db_retry_lock = threading.Lock()
        self._db_max_retries = 5

        self._send_worker_thread = threading.Thread(
            target=self._queued_send_worker,
            name="ETM queued send worker",
            daemon=True
        )
        self._send_worker_thread.start()
        self.logger.debug("Queued send system initialized...")

        self.logger.debug("Adding base dispatchers...")
        self._add_base_dispatchers()
        self.Decorators.enable_retry = channel.flag('retry_on_error')
        self.logger.debug("Base dispatchers added...")

    @staticmethod
    def _normalize_request_kwargs(request_kwargs: Mapping[str, object]) -> dict[str, object]:
        """Translate ETM's legacy PTB 13 request settings to PTB 22 HTTPXRequest kwargs."""
        normalized: dict[str, object] = {}
        allowed = {
            "read_timeout",
            "write_timeout",
            "connect_timeout",
            "pool_timeout",
            "media_write_timeout",
            "http_version",
            "socket_options",
            "httpx_kwargs",
            "connection_pool_size",
        }
        for key in allowed:
            if key in request_kwargs:
                normalized[key] = request_kwargs[key]

        proxy = request_kwargs.get("proxy") or request_kwargs.get("proxy_url")
        username = request_kwargs.get("username")
        password = request_kwargs.get("password")
        proxy_auth_raw = request_kwargs.get("urllib3_proxy_kwargs")
        proxy_auth = proxy_auth_raw if isinstance(proxy_auth_raw, Mapping) else {}
        if username is None:
            username = proxy_auth.get("username")
        if password is None:
            password = proxy_auth.get("password")
        if proxy:
            parsed = urlparse(str(proxy))
            if username and password and "@" not in parsed.netloc:
                auth_netloc = f"{quote(str(username))}:{quote(str(password))}@{parsed.hostname or ''}"
                if parsed.port:
                    auth_netloc += f":{parsed.port}"
                proxy = urlunparse(
                    (
                        parsed.scheme,
                        auth_netloc,
                        parsed.path,
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                )
            normalized["proxy"] = proxy
        return normalized

    def _build_request(self) -> HTTPXRequest:
        socket_options = self._request_kwargs.get("socket_options")
        http_version = self._request_kwargs.get("http_version")
        httpx_kwargs = self._request_kwargs.get("httpx_kwargs")
        proxy = self._request_kwargs.get("proxy")
        return HTTPXRequest(
            connection_pool_size=cast(int, self._request_kwargs.get("connection_pool_size", 1)),
            read_timeout=cast(Optional[float], self._request_kwargs.get("read_timeout")),
            write_timeout=cast(Optional[float], self._request_kwargs.get("write_timeout")),
            connect_timeout=cast(Optional[float], self._request_kwargs.get("connect_timeout")),
            pool_timeout=cast(Optional[float], self._request_kwargs.get("pool_timeout")),
            media_write_timeout=cast(Optional[float], self._request_kwargs.get("media_write_timeout")),
            http_version=cast(Literal["1.1", "2.0", "2"], http_version or "1.1"),
            socket_options=cast(
                Optional[
                    Collection[
                        tuple[int, int, int]
                        | tuple[int, int, bytes | bytearray]
                        | tuple[int, int, None, int]
                    ]
                ],
                socket_options,
            ),
            proxy=cast(Optional[str], proxy),
            httpx_kwargs=cast(Optional[dict[str, object]], httpx_kwargs),
        )

    def _build_bot(self, *, request: HTTPXRequest, get_updates_request: HTTPXRequest) -> telegram.Bot:
        base_url = self._bot_identity_kwargs.get("base_url")
        base_file_url = self._bot_identity_kwargs.get("base_file_url")
        if base_url is not None and base_file_url is not None:
            return telegram.Bot(
                token=self._bot_identity_kwargs["token"],
                base_url=base_url,
                base_file_url=base_file_url,
                local_mode=self._local_mode,
                request=request,
                get_updates_request=get_updates_request,
            )
        if base_url is not None:
            return telegram.Bot(
                token=self._bot_identity_kwargs["token"],
                base_url=base_url,
                local_mode=self._local_mode,
                request=request,
                get_updates_request=get_updates_request,
            )
        if base_file_url is not None:
            return telegram.Bot(
                token=self._bot_identity_kwargs["token"],
                base_file_url=base_file_url,
                local_mode=self._local_mode,
                request=request,
                get_updates_request=get_updates_request,
            )
        return telegram.Bot(
            token=self._bot_identity_kwargs["token"],
            local_mode=self._local_mode,
            request=request,
            get_updates_request=get_updates_request,
        )

    async def _post_init(self, application: Application):
        self._runtime.bind_loop(asyncio.get_running_loop())
        for aux_bot in (self.bot_pool.bots if self.bot_pool else []):
            aux_bot.bind_runtime(self._runtime)
        self._shutdown_complete_event.clear()


    async def _post_shutdown(self, application: Application):
        self._runtime.clear_loop()
        self._shutdown_complete_event.set()
        self.logger.debug("Telegram runtime loop is cleared; shutdown complete.")

    async def _shutdown_ptb_application(self):
        self.application.stop_running()

    async def _run_application_lifecycle(
        self,
        *,
        drop_pending_updates: bool,
        timeout: int | timedelta,
    ) -> None:
        """Own the asyncio loop for polling (replaces ``run_polling``).

        Matches PTB 22 ``Application.run_polling`` ordering: initialize → post_init →
        ``updater.start_polling`` → ``start`` → wait → ``updater.stop`` → ``stop`` →
        ``post_stop`` → ``shutdown`` → ``post_shutdown``. Teardown runs in ``finally`` so
        ``await application.shutdown()`` (HTTPX close) completes before this coroutine returns.
        """
        # Publish the stop event only after ``post_init`` binds the runtime loop.
        stop_event = asyncio.Event()
        try:
            await self.application.initialize()
            post_init = self.application.post_init
            if post_init:
                await post_init(self.application)

            self._manual_polling_stop_event = stop_event

            updater = self.application.updater
            if updater is None:
                raise RuntimeError("Application.run_polling requires an Updater.")

            def error_callback(exc: telegram.error.TelegramError) -> None:
                self.application.create_task(
                    self.application.process_error(error=exc, update=None)
                )

            await updater.start_polling(
                poll_interval=0.0,
                timeout=timeout,
                bootstrap_retries=0,
                allowed_updates=None,
                drop_pending_updates=drop_pending_updates,
                error_callback=error_callback,
            )
            await self.application.start()
            self.logger.debug("Application started; awaiting stop signal")
            await stop_event.wait()
            self.logger.debug("Stop signal received; tearing down")
        finally:
            self._manual_polling_stop_event = None
            try:
                updater = self.application.updater
                if updater is not None and updater.running:
                    await updater.stop()
                for task in asyncio.all_tasks() - {asyncio.current_task()}:
                    task.cancel()
                await asyncio.sleep(0)
            except Exception:
                self.logger.exception("Error during updater.stop")

            try:
                if self.application.running:
                    await self.application.stop()
            except Exception:
                self.logger.exception("Error during application.stop")

            try:
                post_stop = self.application.post_stop
                if post_stop:
                    await post_stop(self.application)
            except Exception:
                self.logger.exception("Error during post_stop")

            try:
                await self.application.shutdown()
            except Exception:
                self.logger.exception("Error during application.shutdown")

            try:
                post_shutdown = self.application.post_shutdown
                if post_shutdown:
                    await post_shutdown(self.application)
            except Exception:
                self.logger.exception("Error during post_shutdown")

    def as_async_callback(self, callback: Callable[P, T]) -> Callable[P, Coroutine[object, object, T]]:
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await asyncio.to_thread(callback, *args, **kwargs)

        return wrapper

    def _add_base_dispatchers(self):
        whitelist_filter = ~Filters.user(user_id=self.admins)
        self.dispatcher.add_handler(
            MessageHandler(whitelist_filter, self.as_async_callback(lambda update, context: None))
        )
        # Register update_locale in a negative group so it runs BEFORE group 0
        # handlers and does NOT block them. PTB 22 runs one matching handler
        # per group; group 0 is the default and is where commands live.
        self.dispatcher.add_handler(
            TypeHandler(Update, self.as_async_callback(self.channel.update_locale)),
            group=-1,
        )

    def _make_send_receipt(
        self,
        message: object,
        sender_bot_id: Optional[str] = None,
        *,
        queued: bool = False,
        task_id: Optional[str] = None,
    ) -> SendReceipt:
        return SendReceipt(
            message=message,
            sender_bot_id=sender_bot_id,
            queued=queued,
            task_id=task_id,
            manager=self,
        )

    def _enqueue_eventual_send(
        self,
        slave_id: str,
        chat_id: int,
        function: Callable,
        args: tuple,
        kwargs: dict,
        *,
        cleanup_files: Optional[list] = None,
    ) -> SendReceipt:
        kwargs = dict(kwargs)
        for key in ('photo', 'document', 'video', 'animation', 'audio', 'voice', 'sticker'):
            if key in kwargs:
                kwargs[key] = _clone_file_argument(kwargs[key])
        if 'media' in kwargs:
            kwargs['media'] = _clone_media_argument(kwargs['media'])
        if len(args) >= 3:
            args = args[:2] + (_clone_file_argument(args[2]),) + args[3:]

        task_id = self._enqueue_send_task(
            target=(slave_id, int(chat_id)),
            function=function,
            args=args,
            kwargs=kwargs,
            cleanup_files=cleanup_files,
        )
        placeholder = self._create_queued_message_placeholder(chat_id, task_id)
        return self._make_send_receipt(placeholder, queued=True, task_id=task_id)

    def _select_sender(self, chat_id: int, *, slave_id: Optional[str] = None,
                       has_callback: bool = False, message_thread_id: Optional[int] = None):
        """Choose the earliest sender using the current main/aux heuristics."""
        main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)
        if main_delay <= 0:
            return self._bot, None, main_delay
        if self.bot_pool and not has_callback:
            affinity_key = slave_id or (chat_id, message_thread_id)
            slot = self.bot_pool.acquire_send_slot(
                chat_id,
                max_delay=main_delay,
                affinity_key=affinity_key,
                notify_admin=(main_delay > 0),
            )
            if slot is not None:
                aux_bot, aux_delay = slot
                return aux_bot.bot, str(aux_bot.bot_id), aux_delay
        return self._bot, None, main_delay

    def _select_forced_sender(self, chat_id: int, sender_bot_id: Optional[str]):
        """Select the bot that sent the reply target message."""
        if sender_bot_id and self.bot_pool:
            aux_bot = self.bot_pool.get_bot_by_id(sender_bot_id)
            if aux_bot and not aux_bot.disabled:
                delay = aux_bot.reserve_slot(chat_id)
                return aux_bot.bot, str(sender_bot_id), delay
            self.logger.warning(
                "Reply target sender bot %s is unavailable for chat %s; falling back to main bot.",
                sender_bot_id, chat_id,
            )
        main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)
        return self._bot, None, main_delay

    def _select_available_sender(self, chat_id: int, *, slave_id: Optional[str] = None,
                                has_callback: bool = False,
                                message_thread_id: Optional[int] = None, now: float = 0.0):
        """Select an immediately available sender, skipping Telegram-disabled bot/chat pairs.

        Returns ``(bot, bot_id, 0.0)`` when a slot is reserved.  A non-zero
        delay means no sender is currently available; callers must requeue and
        try again instead of storing that delay on the task.
        """
        now = now or time.time()
        main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)

        if self.bot_pool and not has_callback:
            slot = self.bot_pool.acquire_send_slot(
                chat_id,
                max_delay=1e-9,
                skip_bot=lambda aux_bot: self._bot_chat_disabled_until.get((str(aux_bot.bot_id), chat_id), 0.0) > now,
                affinity_key=slave_id or (chat_id, message_thread_id),
                notify_admin=(main_delay > 0),
            )
            if slot is not None:
                aux_bot_obj, aux_delay = slot
                return aux_bot_obj.bot, str(aux_bot_obj.bot_id), aux_delay

        main_disabled = self._bot_chat_disabled_until.get((None, chat_id), 0.0)
        if main_disabled > now:
            return None, None, main_disabled - now

        if main_delay <= 0:
            self._calculate_rate_limit_delay(chat_id)  # reserve
            return self._bot, None, 0.0
        return None, None, main_delay

    def _select_forced_available_sender(self, chat_id: int, sender_bot_id: Optional[str], *, now: float = 0.0):
        """Select the reply target's sender bot when it is immediately available."""
        now = now or time.time()
        if sender_bot_id and self.bot_pool:
            disabled_until = self._bot_chat_disabled_until.get((str(sender_bot_id), chat_id), 0.0)
            if disabled_until > now:
                return None, None, disabled_until - now
            aux_bot = self.bot_pool.get_bot_by_id(sender_bot_id)
            if aux_bot and not aux_bot.disabled:
                delay = aux_bot.peek_delay(chat_id)
                if delay <= 0:
                    aux_bot.reserve_slot(chat_id)
                    return aux_bot.bot, str(sender_bot_id), 0.0
                return None, None, delay
            self.logger.warning(
                "Reply target sender bot %s is unavailable for queued chat %s; falling back to main bot.",
                sender_bot_id, chat_id,
            )

        main_disabled = self._bot_chat_disabled_until.get((None, chat_id), 0.0)
        if main_disabled > now:
            return None, None, main_disabled - now
        main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)
        if main_delay > 0:
            return None, None, main_delay
        self._calculate_rate_limit_delay(chat_id)
        return self._bot, None, 0.0

    def _requeue_send_task(self, task: QueuedSendTask):
        """Put a task back at the front of its target FIFO."""
        with self._send_queues_lock:
            q = self._send_queues.setdefault(task.target, collections.deque())
            q.appendleft(task)

    def _init_bot_pool(self, aux_configs: list, config: dict, channel: 'TelegramChannel'):
        """Initialize the auxiliary bot pool from config."""
        req_kwargs = {
            'read_timeout': 15.0,
            'connection_pool_size': self._default_connection_pool_size(config),
        }
        conf_req_kwargs = config.get('request_kwargs')
        if isinstance(conf_req_kwargs, collections.abc.Mapping):
            req_kwargs.update(conf_req_kwargs)
        request_kwargs = self._normalize_request_kwargs(req_kwargs)

        main_token = config['token']
        seen_tokens = {main_token}
        aux_bots: list = []

        for entry in aux_configs:
            if not isinstance(entry, dict) or not isinstance(entry.get('token'), str):
                self.logger.warning("Invalid auxiliary_bots entry (missing token string), skipping: %s", entry)
                continue
            token = entry['token']
            if token in seen_tokens:
                self.logger.warning("Duplicate bot token in auxiliary_bots, skipping")
                continue
            seen_tokens.add(token)

            aux_bot = AuxiliaryBot(
                token=token,
                request_kwargs=request_kwargs,
                base_url=channel.flag('api_base_url') or None,
                base_file_url=channel.flag('api_base_file_url') or None,
                local_mode=self._local_mode,
                global_limit=self.GLOBAL_LIMIT,
                global_window=self.GLOBAL_WINDOW,
                chat_limit=self.CHAT_LIMIT,
                chat_window=self.CHAT_WINDOW,
            )
            aux_bot.bind_runtime(self._runtime)
            if aux_bot.initialize():
                aux_bots.append(aux_bot)
            else:
                self.logger.error("Skipping auxiliary bot with invalid token")

        if aux_bots:
            self.bot_pool = BotPool(aux_bots, self)
            self.logger.info("Initialized bot pool with %d auxiliary bot(s)", len(aux_bots))

    @property
    def _active_bot(self):
        """Return the bot to use for the current send operation.
        Thread-safe: each thread has its own override slot."""
        return cast(SyncBotProtocol, getattr(self._tls, 'override_bot', None) or self._bot)

    @contextmanager
    def _using_bot(self, bot: object):
        """Context manager to temporarily route sends through a different bot."""
        old = getattr(self._tls, 'override_bot', None)
        self._tls.override_bot = bot
        try:
            yield
        finally:
            self._tls.override_bot = old

    def _record_aux_use(self, chat_id: int):
        """Record that an aux bot was used for a chat (for typing suppression)."""
        self._aux_recent_use[chat_id] = time.time()

    def _notify_admin_disabled_bot(self, aux_bot: AuxiliaryBot):
        """Send one-shot notification to admin that an aux bot was disabled."""
        def _notify():
            try:
                admin_id = self.admins[0]
                self._bot.send_message(
                    admin_id,
                    f"⚠️ Auxiliary bot @{aux_bot.username} (id={aux_bot.bot_id}) has been "
                    f"disabled: {aux_bot._disable_reason}. Please check the token in config."
                )
            except Exception as e:
                self.logger.warning("Failed to notify admin about disabled aux bot: %s", e)

        threading.Thread(target=_notify, daemon=True, name="AuxBotDisabledNotify").start()

    def send_blocking_migration(self, chat_id: int, send_callable: Callable,
                                timeout: float = 60.0):
        """Block until any bot (main or aux) has a free slot, then send through it.

        The callable receives a single ``_bypass_rate_limit=True`` kwarg that
        the caller must forward to the decorated send method so the decorator
        skips its own routing/reservation.

        Returns whatever the send_callable returns.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)

            # Try aux bots first if they can beat the main bot
            if self.bot_pool:
                slot = self.bot_pool.acquire_send_slot(chat_id, max_delay=max(main_delay, 0.01))
                if slot is not None:
                    aux_bot, aux_delay = slot
                    if aux_delay > 0:
                        time.sleep(aux_delay)
                    try:
                        with self._using_bot(aux_bot.bot):
                            return send_callable(_bypass_rate_limit=True)
                    except telegram.error.Forbidden:
                        aux_bot.update_membership(chat_id, False)
                        self.logger.warning(
                            "Auxiliary bot %s got Forbidden in chat %s during migration send; "
                            "marking it as non-member for this chat.",
                            aux_bot.bot_id, chat_id,
                        )

            # Try main bot
            if main_delay == 0.0:
                self._calculate_rate_limit_delay(chat_id)  # reserve
                return send_callable(_bypass_rate_limit=True)

            time.sleep(0.2)

        # Timeout fallback: reserve on main and send anyway
        self.logger.warning("send_blocking_migration timed out for chat %d, sending on main bot", chat_id)
        self._calculate_rate_limit_delay(chat_id)
        return send_callable(_bypass_rate_limit=True)

    def _calculate_rate_limit_delay(self, chat_id: int, peek_only: bool = False):
        """
        Calculate rate limiting delay using the shared sliding window limiter.

        Args:
            chat_id: Telegram chat ID
            peek_only: If True, compute delay without reserving a slot.

        Returns:
            tuple: (wait_seconds, chat_count, global_count)
        """
        if peek_only:
            sleep_time = self._rate_limiter.peek_delay(chat_id)
        else:
            sleep_time = self._rate_limiter.reserve_slot(chat_id)

        chat_count, global_count = self._rate_limiter.get_counts(chat_id)

        if sleep_time > 0:
            self.logger.info(
                "Rate limit reached, need to delay %.2fs for chat %d. Chat: %d/%d, Global: %d/%d",
                sleep_time, chat_id, chat_count, self.CHAT_LIMIT, global_count, self.GLOBAL_LIMIT,
            )
        else:
            self.logger.debug(
                "Rate limit not reached for chat %d. Chat: %d/%d, Global: %d/%d",
                chat_id, chat_count, self.CHAT_LIMIT, global_count, self.GLOBAL_LIMIT,
            )

        return sleep_time, chat_count, global_count

    def _create_queued_message_placeholder(self, chat_id: int, task_id: str):
        """Create a placeholder message object for queued execution."""
        placeholder = QueuedSendPlaceholder(
            chat_id=chat_id,
            message_id=int(time.time() * 1000),
            date=int(time.time()),
            text="[Message queued for delivery]",
            task_id=task_id,
        )
        self.logger.debug("Created queued message placeholder for chat %s", chat_id)
        return placeholder

    def _enqueue_send_task(self, target: SendTarget, function: Callable,
                           args: tuple, kwargs: dict,
                           cleanup_files: Optional[list] = None) -> str:
        """Append a task to the per-target FIFO queue."""
        slave_id, chat_id = target
        with self._send_queues_lock:
            self._tasks_enqueued += 1
            arrival_seq = self._tasks_enqueued
            task_id = f"{slave_id}_{chat_id}_{arrival_seq}_{time.time_ns()}"
            task = QueuedSendTask(
                target=target,
                function=function,
                args=args,
                kwargs=kwargs,
                task_id=task_id,
                arrival_seq=arrival_seq,
                cleanup_files=tuple(cleanup_files or ())
            )
            q = self._send_queues.setdefault(target, collections.deque())
            q.append(task)

        self.logger.debug("Queued send task %s for target %s", task_id, target)
        return task_id

    # ── Async-dispatch queued send worker ──────────────────────

    def _dispatch_ready_send_tasks(self, now: float):
        with self._send_queues_lock:
            dispatchable_targets = [
                target for target, q in self._send_queues.items()
                if q and target not in self._send_in_flight
            ]

        for target in dispatchable_targets:
            if self._send_worker_stop.is_set():
                break

            with self._send_queues_lock:
                q = self._send_queues.get(target)
                if not q:
                    continue
                task = q.popleft()
                if not q:
                    del self._send_queues[target]

            if task.kwargs.get('_force_sender_known'):
                sender_bot, sender_bot_id, wait_time = self._select_forced_available_sender(
                    task.chat_id,
                    task.kwargs.get('_force_sender_bot_id'),
                    now=now,
                )
            else:
                sender_bot, sender_bot_id, wait_time = self._select_available_sender(
                    task.chat_id,
                    slave_id=task.slave_id,
                    has_callback=_has_callback_keyboard(task.kwargs.get('reply_markup')),
                    message_thread_id=task.kwargs.get('message_thread_id'),
                    now=now,
                )
            if sender_bot is None or wait_time > 0:
                self._requeue_send_task(task)
                continue

            self._dispatch_send(task, sender_bot, sender_bot_id)

    def _queued_send_worker(self):
        """Worker thread: dispatch sends to a thread pool.

        Same (slave_id, chat_id) → serial (one in-flight at a time, preserves order).
        Different targets → parallel (thread pool).
        The worker thread itself never blocks on HTTP; it only orchestrates.
        """
        self.logger.debug("Queued send worker started")

        while not self._send_worker_stop.is_set():
            try:
                now = time.time()

                # ── 1. Harvest completed sends ──
                self._harvest_completed_sends()

                # ── 2. Dispatch ready tasks ──
                self._dispatch_ready_send_tasks(now)

                # ── 3. Housekeeping ──
                self._process_db_retry_queue()

                # Purge expired disabled bot/chat entries
                if self._bot_chat_disabled_until:
                    expired = [k for k, v in self._bot_chat_disabled_until.items() if v <= now]
                    for k in expired:
                        del self._bot_chat_disabled_until[k]
                if now - self._last_rendezvous_cleanup > self._rendezvous_cleanup_interval:
                    self._cleanup_stale_rendezvous()
                    self._last_rendezvous_cleanup = now

                self._send_worker_stop.wait(timeout=0.05)

            except Exception as e:
                self.logger.exception(f"Error in queued send worker: {e}")
                self._send_worker_stop.wait(timeout=1)

        # Shutdown: wait for in-flight sends to finish
        for _target, (future, task, _bot_id) in list(self._send_in_flight.items()):
            try:
                future.result(timeout=10)
            except Exception:
                pass
        self._send_executor.shutdown(wait=False)
        self.logger.debug("Queued send worker stopped")

    def _dispatch_send(self, task: QueuedSendTask, sender_bot, sender_bot_id: Optional[str]):
        """Submit a send operation to the thread pool."""

        def _do_send():
            send_kwargs = dict(task.kwargs)
            send_kwargs.pop('_force_sender_known', None)
            send_kwargs.pop('_force_sender_bot_id', None)
            send_kwargs.pop('_slave_id', None)
            send_kwargs['_skip_rate_limit_retry'] = True
            with self._using_bot(sender_bot):
                return task.function(*task.args, **send_kwargs)

        future = self._send_executor.submit(_do_send)
        self._send_in_flight[task.target] = (future, task, sender_bot_id)

    def _release_reserved_slot(self, sender_bot_id: Optional[str], chat_id: int):
        if sender_bot_id and self.bot_pool:
            aux_bot = self.bot_pool.get_bot_by_id(sender_bot_id)
            if aux_bot and hasattr(aux_bot, "release_slot"):
                aux_bot.release_slot(chat_id)
            return
        if hasattr(self, "_rate_limiter"):
            self._rate_limiter.release_slot(chat_id)

    @staticmethod
    def _rate_limit_retry_after_seconds(error: Exception) -> Optional[float]:
        if isinstance(error, telegram.error.RetryAfter):
            retry_after_value = error.retry_after
            if isinstance(retry_after_value, timedelta):
                return retry_after_value.total_seconds()
            return float(retry_after_value)
        error_text = str(error)
        if "Too Many Requests" in error_text or "429" in error_text or "Flood" in error_text:
            return 60.0
        return None

    def _requeue_after_telegram_rate_limit(self, task: QueuedSendTask,
                                           sender_bot_id: Optional[str],
                                           retry_after: float):
        self.logger.warning(
            "Telegram rate limit for bot %s in chat %d (task %s): %.2fs; other bots can still send",
            sender_bot_id or "main", task.chat_id, task.task_id, retry_after,
        )
        self._bot_chat_disabled_until[(sender_bot_id, task.chat_id)] = time.time() + retry_after
        self._release_reserved_slot(sender_bot_id, task.chat_id)
        self._requeue_send_task(task)

    def _harvest_completed_sends(self):
        """Check all in-flight futures; handle success / errors."""
        completed = [(target, ft) for target, ft in self._send_in_flight.items() if ft[0].done()]

        for target, (future, task, sender_bot_id) in completed:
            del self._send_in_flight[target]
            should_cleanup = True

            try:
                result = future.result()  # already done, won't block

                if sender_bot_id is not None:
                    self._record_aux_use(task.chat_id)

                self._last_send_by_chat[task.chat_id] = time.time()
                self.logger.debug("Queued send task %s completed successfully", task.task_id)

                if result and hasattr(result, 'message_id'):
                    self._handle_queued_database_update(
                        task.task_id, result, sender_bot_id=sender_bot_id,
                    )
                else:
                    TelegramBotManager._drop_queued_database_update(self, task.task_id)

            except telegram.error.RetryAfter as e:
                should_cleanup = False
                retry_after = TelegramBotManager._rate_limit_retry_after_seconds(e)
                TelegramBotManager._requeue_after_telegram_rate_limit(
                    self, task, sender_bot_id, retry_after or 60.0
                )

            except telegram.error.BadRequest as e:
                retry_after = TelegramBotManager._rate_limit_retry_after_seconds(e)
                if retry_after is not None:
                    should_cleanup = False
                    TelegramBotManager._requeue_after_telegram_rate_limit(
                        self, task, sender_bot_id, retry_after
                    )
                    continue
                self._release_reserved_slot(sender_bot_id, task.chat_id)
                self.logger.warning(
                    "Non-retryable BadRequest for queued task %s, dropping: %s "
                    "(chat_id=%s, reply_to_message_id=%s, message_thread_id=%s, method=%s)",
                    task.task_id, e, task.chat_id,
                    task.kwargs.get("reply_to_message_id"),
                    task.kwargs.get("message_thread_id"),
                    getattr(task.function, "__name__", repr(task.function)),
                )
                TelegramBotManager._drop_queued_database_update(self, task.task_id)

            except (telegram.error.TimedOut, telegram.error.NetworkError) as e:
                retry_after = TelegramBotManager._rate_limit_retry_after_seconds(e)
                if retry_after is not None:
                    should_cleanup = False
                    TelegramBotManager._requeue_after_telegram_rate_limit(
                        self, task, sender_bot_id, retry_after
                    )
                    continue
                should_cleanup = False
                self.logger.warning(
                    "Transient error for queued task %s, retrying: %s",
                    task.task_id, e,
                )
                self._release_reserved_slot(sender_bot_id, task.chat_id)
                self._requeue_send_task(task)

            except telegram.error.Forbidden as e:
                self._release_reserved_slot(sender_bot_id, task.chat_id)
                if sender_bot_id and self.bot_pool:
                    aux_bot = self.bot_pool.get_bot_by_id(sender_bot_id)
                    if aux_bot:
                        aux_bot.update_membership(task.chat_id, False)
                        should_cleanup = False
                        self.logger.warning(
                            "Aux bot %s got Forbidden in chat %s for queued task %s; "
                            "marking it as non-member for this chat and retrying.",
                            sender_bot_id, task.chat_id, task.task_id,
                        )
                        self._requeue_send_task(task)
                        continue
                self.logger.exception(f"Error executing queued task {task.task_id}: {e}")
                TelegramBotManager._drop_queued_database_update(self, task.task_id)

            except telegram.error.TelegramError as e:
                retry_after = TelegramBotManager._rate_limit_retry_after_seconds(e)
                if retry_after is not None:
                    should_cleanup = False
                    TelegramBotManager._requeue_after_telegram_rate_limit(
                        self, task, sender_bot_id, retry_after
                    )
                    continue
                self._release_reserved_slot(sender_bot_id, task.chat_id)
                self.logger.exception(f"Error executing queued task {task.task_id}: {e}")
                TelegramBotManager._drop_queued_database_update(self, task.task_id)

            except Exception as e:
                self._release_reserved_slot(sender_bot_id, task.chat_id)
                self.logger.exception(f"Error executing queued task {task.task_id}: {e}")
                TelegramBotManager._drop_queued_database_update(self, task.task_id)

            finally:
                if should_cleanup:
                    for path in task.cleanup_files:
                        try:
                            os.unlink(path)
                            self.logger.debug("Cleaned up queued task temp file: %s", path)
                        except OSError as e:
                            self.logger.warning("Failed to clean up temp file %s: %s", path, e)

    def _run_database_update_callback(self, on_complete: Optional[Callable[[], None]]):
        if on_complete is None:
            return
        try:
            on_complete()
        except Exception as e:
            self.logger.warning("Database update completion callback failed: %s", e)

    def _queued_rendezvous_maps(self) -> tuple[dict[str, tuple], dict[str, tuple]]:
        return self._pending_queued_logs, self._completed_queued_results

    @staticmethod
    def _queued_log_callback(entry: Optional[tuple]) -> Optional[Callable[[], None]]:
        if entry is not None and len(entry) >= 4:
            return entry[3]
        return None

    def _drop_queued_database_update(self, task_id: str):
        """Release bookkeeping for a queued task that will not produce a Telegram message."""
        if not hasattr(self, '_pending_logs_lock'):
            return
        pending_update = None
        with self._pending_logs_lock:
            pending_logs, completed_results = TelegramBotManager._queued_rendezvous_maps(self)
            pending_update = pending_logs.pop(task_id, None)
            if pending_update is None:
                completed_results[task_id] = (
                    _QUEUED_DATABASE_UPDATE_DROPPED, None, time.time()
                )
            else:
                completed_results.pop(task_id, None)
        TelegramBotManager._run_database_update_callback(
            self, TelegramBotManager._queued_log_callback(pending_update)
        )

    def _finalize_queued_database_update(
        self,
        etm_msg,
        old_msg_id,
        real_tg_msg: TelegramMessage,
        *,
        sender_bot_id: Optional[str] = None,
        on_complete: Optional[Callable[[], None]] = None,
    ):
        """Write the send result to the database.

        On failure the write is pushed to a retry queue instead of being
        silently dropped — a lost mapping would cause subsequent edits /
        deletes to create duplicate messages.
        """
        try:
            etm_msg.type_telegram = get_msg_type(real_tg_msg)
            etm_msg.put_telegram_file(real_tg_msg)
            self.channel.db.add_or_update_message_log(
                etm_msg,
                real_tg_msg,
                old_msg_id,
                sender_bot_id=sender_bot_id,
            )
            TelegramBotManager._run_database_update_callback(self, on_complete)
        except Exception as e:
            self.logger.warning(
                "DB write failed for tg_msg %s, queuing for retry: %s",
                getattr(real_tg_msg, 'message_id', '?'),
                e,
            )
            TelegramBotManager._enqueue_db_retry(
                self, etm_msg, old_msg_id, real_tg_msg, sender_bot_id, on_complete=on_complete
            )

    def _enqueue_db_retry(self, etm_msg, old_msg_id, real_tg_msg, sender_bot_id,
                          attempt: int = 0,
                          on_complete: Optional[Callable[[], None]] = None):
        """Push a failed DB write into the retry queue with exponential back-off."""
        next_retry_at = time.time() + min(2 ** attempt, 30)  # cap at 30 s
        with self._db_retry_lock:
            self._db_retry_queue.append(
                (etm_msg, old_msg_id, real_tg_msg, sender_bot_id,
                 attempt + 1, next_retry_at, on_complete)
            )

    def _process_db_retry_queue(self):
        """Process pending DB-write retries.  Called from the worker loop."""
        if not self._db_retry_queue:
            return

        current_time = time.time()
        still_pending: list[tuple] = []

        with self._db_retry_lock:
            items = self._db_retry_queue[:]
            self._db_retry_queue.clear()

        for item in items:
            if len(item) >= 7:
                etm_msg, old_msg_id, real_tg_msg, sender_bot_id, attempt, next_retry_at, on_complete = item
            else:
                etm_msg, old_msg_id, real_tg_msg, sender_bot_id, attempt, next_retry_at = item
                on_complete = None
            if current_time < next_retry_at:
                still_pending.append(
                    (etm_msg, old_msg_id, real_tg_msg, sender_bot_id,
                     attempt, next_retry_at, on_complete)
                )
                continue
            try:
                self.channel.db.add_or_update_message_log(
                    etm_msg,
                    real_tg_msg,
                    old_msg_id,
                    sender_bot_id=sender_bot_id,
                )
                self.logger.info(
                    "DB retry succeeded for tg_msg %s (attempt %d)",
                    getattr(real_tg_msg, 'message_id', '?'),
                    attempt,
                )
                TelegramBotManager._run_database_update_callback(self, on_complete)
            except Exception as e:
                if attempt >= self._db_max_retries:
                    self.logger.error(
                        "DB write permanently failed after %d attempts for tg_msg %s: %s",
                        attempt,
                        getattr(real_tg_msg, 'message_id', '?'),
                        e,
                    )
                else:
                    self.logger.warning(
                        "DB retry %d failed for tg_msg %s, will retry: %s",
                        attempt,
                        getattr(real_tg_msg, 'message_id', '?'),
                        e,
                    )
                    TelegramBotManager._enqueue_db_retry(
                        self, etm_msg, old_msg_id, real_tg_msg, sender_bot_id, attempt, on_complete=on_complete
                    )

        if still_pending:
            with self._db_retry_lock:
                self._db_retry_queue.extend(still_pending)

    def _cleanup_stale_rendezvous(self):
        """Remove rendezvous entries that were never paired within the TTL."""
        cutoff = time.time() - self._rendezvous_ttl
        with self._pending_logs_lock:
            pending_logs, completed_results = TelegramBotManager._queued_rendezvous_maps(self)
            stale_logs = [
                tid for tid, entry in pending_logs.items()
                if len(entry) >= 3 and entry[2] < cutoff
            ]
            for tid in stale_logs:
                TelegramBotManager._run_database_update_callback(
                    self,
                    TelegramBotManager._queued_log_callback(pending_logs.get(tid)),
                )
                del pending_logs[tid]
                self.logger.warning("Rendezvous TTL: dropped stale pending log for task %s", tid)

            stale_results = [
                tid for tid, entry in completed_results.items()
                if len(entry) >= 3 and entry[2] < cutoff
            ]
            for tid in stale_results:
                del completed_results[tid]
                self.logger.warning("Rendezvous TTL: dropped stale completed result for task %s", tid)

    def submit_async_db_write(
        self,
        etm_msg,
        real_tg_msg: TelegramMessage,
        old_msg_id=None,
        *,
        sender_bot_id: Optional[str] = None,
        on_complete: Optional[Callable[[], None]] = None,
    ):
        """Fire-and-forget database write, usable from both blocking and eventual paths.

        Failures are caught and routed to the retry queue — callers should
        NEVER re-send to Telegram on a DB-write error.
        """
        TelegramBotManager._finalize_queued_database_update(
            self,
            etm_msg, old_msg_id, real_tg_msg,
            sender_bot_id=sender_bot_id,
            on_complete=on_complete,
        )

    def register_queued_database_update(self, task_id: str, etm_msg, old_msg_id=None,
                                         on_complete: Optional[Callable[[], None]] = None):
        """
        Register a pending database update for a queued task.

        Args:
            task_id: Task identifier from queued execution
            etm_msg: ETMMsg object to log
            old_msg_id: Optional old message ID for updates
        """
        completed_result: Optional[tuple]
        with self._pending_logs_lock:
            pending_logs, completed_results = TelegramBotManager._queued_rendezvous_maps(self)
            completed_result = completed_results.pop(task_id, None)
            if completed_result is None:
                if on_complete is not None:
                    pending_logs[task_id] = (etm_msg, old_msg_id, time.time(), on_complete)
                else:
                    pending_logs[task_id] = (etm_msg, old_msg_id, time.time())
                self.logger.debug("Registered queued database update for task %s", task_id)
                return

        if completed_result[0] is _QUEUED_DATABASE_UPDATE_DROPPED:
            self.logger.debug("Queued task %s was dropped before database registration", task_id)
            TelegramBotManager._run_database_update_callback(self, on_complete)
            return

        real_tg_msg, sender_bot_id = completed_result[0], completed_result[1]
        self.logger.debug("Finalizing already-completed queued task %s", task_id)
        TelegramBotManager._finalize_queued_database_update(
            self,
            etm_msg,
            old_msg_id,
            real_tg_msg,
            sender_bot_id=sender_bot_id,
            on_complete=on_complete,
        )

    def _handle_queued_database_update(
        self,
        task_id: str,
        real_tg_msg,
        *,
        sender_bot_id: Optional[str] = None,
    ):
        """
        Handle database update when queued task completes.

        Args:
            task_id: Task identifier
            real_tg_msg: The real Telegram message that was sent
        """
        pending_update = None
        with self._pending_logs_lock:
            pending_logs, completed_results = TelegramBotManager._queued_rendezvous_maps(self)
            pending_update = pending_logs.pop(task_id, None)
            if pending_update is None:
                completed_results[task_id] = (real_tg_msg, sender_bot_id, time.time())
                self.logger.debug("Stored queued result for task %s until database registration is ready", task_id)
                return

        etm_msg, old_msg_id = pending_update[0], pending_update[1]
        on_complete = TelegramBotManager._queued_log_callback(pending_update)
        TelegramBotManager._finalize_queued_database_update(
            self,
            etm_msg,
            old_msg_id,
            real_tg_msg,
            sender_bot_id=sender_bot_id,
            on_complete=on_complete,
        )
        self.logger.debug("Updated database with real message for queued task %s", task_id)

    def stop_queued_worker(self):
        """Stop the queued send worker thread."""
        self.logger.debug("Stopping queued send worker...")

        if hasattr(self, '_send_worker_stop'):
            self._send_worker_stop.set()

        if hasattr(self, '_send_worker_thread') and self._send_worker_thread.is_alive():
            self.logger.debug("Waiting for queued send worker to stop...")
            self._send_worker_thread.join(timeout=5)

            if self._send_worker_thread.is_alive():
                self.logger.warning("Queued send worker did not stop gracefully within timeout")
            else:
                self.logger.debug("Queued send worker stopped successfully")

        # Drain any remaining DB retries before shutdown
        if hasattr(self, '_db_retry_queue') and self._db_retry_queue:
            self.logger.info("Processing %d remaining DB retries at shutdown...", len(self._db_retry_queue))
            self._process_db_retry_queue()

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def send_message(self, *args, prefix: str = '', suffix: str = '', **kwargs):
        """
        Send text message.

        Takes exactly same parameters as telegram.bot.send_message,
        plus the following.

        Args:
            prefix (str, optional): Prefix of the message. Default: ""
            suffix (str, optional): Suffix of the message. Default: ""

        Returns:
            telegram.Message
        """
        prefix = (prefix and (prefix + "\n")) or prefix
        suffix = (suffix and ("\n" + suffix)) or suffix
        if str(kwargs.get('parse_mode', '')).lower() == "html":
            prefix = html.escape(prefix)
            suffix = html.escape(suffix)
        text: str
        if args[1:]:
            text = args[1]
        else:
            text = kwargs.pop('text')
        args = args[:1]
        if len(prefix + text + suffix) >= telegram.constants.MessageLimit.MAX_TEXT_LENGTH:
            full_message = io.BytesIO((prefix + text + suffix).encode('utf-8'))
            truncated = prefix + text[:100] + "\n...\n" + text[-100:] + suffix
            msg = self._bot_send_message_fallback(args[0], text=truncated, **kwargs)
            filename = "%s_%s" % (args[0], msg.message_id)
            if not kwargs.get('parse_mode'):
                filename += ".txt"
            elif kwargs.get('parse_mode', '').lower() == 'markdown':
                filename += ".md"
            elif kwargs.get('parse_mode', '').lower() == 'html':
                filename += ".html"
                full_message_html = (
                    "<html><head><meta charset='utf-8'></head>"
                    "<body><pre style='white-space:pre-wrap'>" + (prefix + text + suffix) + "</pre></body></html>"
                )
                # Replace the attachment payload with HTML-wrapped content.
                # (Previous logic did seek(0) then truncate(), which empties the buffer.)
                full_message = io.BytesIO(full_message_html.encode('utf-8'))
            else:
                filename += ".txt"
            self._active_bot.send_document(args[0], full_message, filename=filename,
                                          reply_to_message_id=msg.message_id,
                                          caption=self._("Message is truncated due to its length. "
                                                         "Full message is sent as attachment."))
            return msg
        else:
            kwargs['text'] = prefix + text + suffix
            return self._bot_send_message_fallback(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def edit_message_text(self, prefix='', suffix='', **kwargs):
        """
        Edit text message.
        Takes exactly same parameters as telegram.bot.edit_message_text,
        plus the following.

        Args:
            prefix (str, optional): Prefix of the message. Default: ""
            suffix (str, optional): Suffix of the message. Default: ""

        Returns:
            telegram.Message
        """
        prefix = (prefix and (prefix + "\n")) or prefix
        suffix = (suffix and ("\n" + suffix)) or suffix
        if str(kwargs.get('parse_mode', '')).lower() == "html":
            prefix = html.escape(prefix)
            suffix = html.escape(suffix)
        text = kwargs.pop('text', '')
        if len(prefix + text + suffix) >= telegram.constants.MessageLimit.MAX_TEXT_LENGTH:
            full_message = io.BytesIO((prefix + text + suffix).encode())
            truncated = prefix + text[:100] + "\n...\n" + text[-100:] + suffix
            msg = self._bot_edit_message_text_fallback(text=truncated, **kwargs)
            filename = "%s_%s" % (kwargs['chat_id'], msg.message_id)
            if kwargs.get('parse_mode', '').lower() == 'markdown':
                filename += ".md"
            elif kwargs.get('parse_mode', '').lower() == 'html':
                filename += ".html"
            else:
                filename += ".txt"
            self._active_bot.send_document(kwargs['chat_id'], full_message,
                                          filename=filename,
                                          reply_to_message_id=msg.message_id,
                                          caption=self._("Message is truncated due to its length. "
                                                         "Full message is sent as attachment."))
            return msg
        else:
            kwargs['text'] = prefix + text + suffix
            return self._bot_edit_message_text_fallback(**kwargs)

    def _bot_send_message_fallback(self, *args, **kwargs):
        """
        Remove ``parse_mode`` if the server fails to parse.

        Returns:
            telegram.Message: The message sent
        """
        try:
            return self._active_bot.send_message(*args, **kwargs)
        except telegram.error.BadRequest as e:
            if e.message.lower().startswith("can't parse entities") and 'parse_mode' in kwargs:
                kwargs.pop("parse_mode")
                return self._active_bot.send_message(*args, **kwargs)
            else:
                raise e

    def _bot_edit_message_text_fallback(self, *args, **kwargs):
        """
        Remove ``parse_mode`` if the server fails to parse.

        Returns:
            telegram.Message: The message sent
        """
        try:
            return self._active_bot.edit_message_text(*args, **kwargs)
        except telegram.error.BadRequest as e:
            if e.message == "Message can't be edited":
                kwargs['reply_to_message_id'] = kwargs.pop('message_id')
                return self._active_bot.send_message(*args, **kwargs)
            elif e.message == "message to edit not found":
                kwargs.pop('message_id')
                return self._active_bot.send_message(*args, **kwargs)
            elif e.message.lower().startswith("can't parse entities") and 'parse_mode' in kwargs:
                kwargs.pop("parse_mode")
                return self._active_bot.edit_message_text(*args, **kwargs)
            else:
                raise e

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.caption_affix_decorator
    @Decorators.retry_on_chat_migration
    def send_audio(self, *args, **kwargs):
        """
        Send an audio file.

        Takes exactly same parameters as telegram.bot.send_audio,
        plus the following.

        Fallback to document when failed to send.

        Args:
            prefix (str, optional): Prefix of the caption. Default: ""
            suffix (str, optional): Suffix of the caption. Default: ""

        Returns:
            telegram.Message
        """
        try:
            return self._active_bot.send_audio(*args, **kwargs)
        except telegram.error.BadRequest:
            return self._active_bot.send_document(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.caption_affix_decorator
    @Decorators.retry_on_chat_migration
    def send_voice(self, *args, **kwargs):
        """
        Send an voice message.

        Takes exactly same parameters as telegram.bot.send_voice,
        plus the following.

        Fallback to document when failed to send.

        Args:
            prefix (str, optional): Prefix of the caption. Default: ""
            suffix (str, optional): Suffix of the caption. Default: ""

        Returns:
            telegram.Message
        """
        try:
            return self._active_bot.send_voice(*args, **kwargs)
        except telegram.error.BadRequest:
            return self._active_bot.send_document(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.caption_affix_decorator
    @Decorators.retry_on_chat_migration
    def send_video(self, *args, **kwargs):
        """
        Send an voice message.

        Takes exactly same parameters as telegram.bot.send_voice,
        plus the following.

        Fallback to document when failed to send.

        Args:
            prefix (str, optional): Prefix of the caption. Default: ""
            suffix (str, optional): Suffix of the caption. Default: ""

        Returns:
            telegram.Message
        """
        try:
            return self._active_bot.send_video(*args, **kwargs)
        except telegram.error.BadRequest:
            return self._active_bot.send_document(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.caption_affix_decorator
    @Decorators.retry_on_chat_migration
    def send_document(self, *args, **kwargs):
        """
        Send a document.

        Takes exactly same parameters as telegram.bot.send_document,
        plus the following.

        Args:
            prefix (str, optional): Prefix of the caption. Default: ""
            suffix (str, optional): Suffix of the caption. Default: ""

        Returns:
            telegram.Message
        """
        return self._active_bot.send_document(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.caption_affix_decorator
    @Decorators.retry_on_chat_migration
    def send_animation(self, *args, **kwargs):
        """
        Send a document.

        Takes exactly same parameters as telegram.bot.send_document,
        plus the following.

        Args:
            prefix (str, optional): Prefix of the caption. Default: ""
            suffix (str, optional): Suffix of the caption. Default: ""

        Returns:
            telegram.Message
        """
        return self._active_bot.send_animation(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.caption_affix_decorator
    @Decorators.retry_on_chat_migration
    def send_photo(self, *args, **kwargs):
        """
        Send a document.

        Takes exactly same parameters as telegram.bot.send_document,
        plus the following.

        Args:
            prefix (str, optional): Prefix of the caption. Default: ""
            suffix (str, optional): Suffix of the caption. Default: ""

        Returns:
            telegram.Message
        """
        try:
            return self._active_bot.send_photo(*args, **kwargs)
        except telegram.error.BadRequest:
            return self._active_bot.send_document(*args, **kwargs)

    @Decorators.skip_on_rate_limit
    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def send_chat_action(self, *args, **kwargs):
        message_thread_id = kwargs.pop('message_thread_id', None)
        if message_thread_id != None:
            kwargs['api_kwargs'] = { "message_thread_id":  message_thread_id}
        return self._bot.send_chat_action(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def edit_message_reply_markup(self, *args, **kwargs):
        return self._active_bot.edit_message_reply_markup(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def send_location(self, *args, **kwargs):
        return self._active_bot.send_location(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def send_venue(self, *args, **kwargs):
        return self._active_bot.send_venue(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def send_sticker(self, *args, **kwargs):
        return self._active_bot.send_sticker(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def forward_message(self, *args, **kwargs):
        return self._active_bot.forward_message(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def copy_message(self, *args, **kwargs):
        return self._active_bot.copy_message(*args, **kwargs)

    @Decorators.retry_on_timeout
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def get_me(self, *args, **kwargs):
        return self._bot.get_me(*args, **kwargs)

    def session_expired(self, update: Update, context: CallbackContext):
        assert isinstance(update, Update)
        assert update.effective_message
        assert update.effective_chat
        if update.callback_query:
            self.answer_callback_query(update.callback_query.id)
        self.edit_message_text(text=self._("Session expired. Please try again. (SE01)"),
                               chat_id=update.effective_chat.id,
                               message_id=update.effective_message.message_id)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.caption_affix_decorator
    @Decorators.retry_on_chat_migration
    def edit_message_caption(self, *args, **kwargs):
        return self._active_bot.edit_message_caption(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def edit_message_media(self, *args, **kwargs):
        return self._active_bot.edit_message_media(*args, **kwargs)

    def reply_error(self, update, errmsg):
        """
        A wrap that quote-reply a message with error details.

        Returns:
            telegram.Message: Message sent
        """
        return self.send_message(update.effective_chat.id, errmsg,
                                 reply_to_message_id=update.effective_message.message_id)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def get_file(self, file_id: str) -> File:
        return cast(File, self._active_bot.get_file(file_id))

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def delete_message(self, chat_id, message_id, _sender_bot_id=None):
        if _sender_bot_id and self.bot_pool:
            aux_bot = self.bot_pool.get_bot_by_id(_sender_bot_id)
            if aux_bot and not aux_bot.disabled:
                try:
                    return aux_bot.bot.delete_message(chat_id, message_id)
                except telegram.error.Forbidden:
                    aux_bot.update_membership(chat_id, False)
                    self.logger.warning(
                        "Auxiliary bot %s got Forbidden in chat %s during delete_message; "
                        "marking it as non-member for this chat.",
                        _sender_bot_id, chat_id,
                    )
                except telegram.error.BadRequest:
                    pass  # Fall through to main bot
        return self._active_bot.delete_message(chat_id, message_id)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def answer_callback_query(self, *args, prefix="", suffix="", text=None,
                              message_id=None, **kwargs):
        if text is None:
            return self._bot.answer_callback_query(
                *args, **kwargs
            )
        prefix = (prefix and (prefix + "\n")) or prefix
        suffix = (suffix and ("\n" + suffix)) or suffix

        chat_id = kwargs.get('chat_id')

        if len(prefix + text + suffix) >= MAX_CALLBACK_QUERY_ANSWER_LENGTH:
            full_message = prefix + text + suffix
            full_message_buffer = io.StringIO(full_message)
            keep_size = MAX_CALLBACK_QUERY_ANSWER_LENGTH // 3
            truncated = full_message[:keep_size] + "…" + full_message[keep_size:]
            result = self._bot.answer_callback_query(*args, text=truncated, **kwargs)
            filename = f"{chat_id}_{message_id}.txt"
            if chat_id is not None:
                self._bot.send_document(
                    chat_id,
                    full_message_buffer,
                    filename,
                    reply_to_message_id=message_id,
                    caption=self._("Response is truncated due to its length. "
                                   "Full message is sent as attachment."),
                )
            return result
        self.logger.debug(f"answer_callback_query({args}, {kwargs})")
        return self._bot.answer_callback_query(
            *args, text=prefix + text + suffix, **kwargs
        )

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def get_chat_info(self, *args, **kwargs):
        return self._bot.get_chat(*args, **kwargs)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def create_forum_topic(self, *args, **kwargs) -> ForumTopic:
        return cast(ForumTopic, self._bot.create_forum_topic(*args, **kwargs))

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def edit_forum_topic(self, *args, **kwargs):
        return self._bot.edit_forum_topic(*args, **kwargs)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def reopen_forum_topic(self, *args, **kwargs) -> bool:
        return cast(bool, self._bot.reopen_forum_topic(*args, **kwargs))

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def set_chat_title(self, *args, **kwargs):
        return self._bot.set_chat_title(*args, **kwargs)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def set_chat_photo(self, *args, **kwargs):
        return self._bot.set_chat_photo(*args, **kwargs)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def pin_chat_message(self, *args, **kwargs):
        return self._bot.pin_chat_message(*args, **kwargs)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def set_chat_description(self, *args, **kwargs):
        return self._bot.set_chat_description(*args, **kwargs)

    def polling(self, drop_pending_updates: bool = False, timeout: int | timedelta = 10):
        """
        Poll message from Telegram Bot API.
        This method is blocking and is expected to run inside EFB's master poll thread.

        Args:
            drop_pending_updates: Whether to clean any pending updates on
                Telegram servers before actually starting to poll.
                Default is False.
            timeout: Long-poll timeout for ``getUpdates``.
        """
        if self.webhook:
            start_webhook = self.channel.config['webhook']['start_webhook']
            self.application.run_webhook(
                **start_webhook,
                drop_pending_updates=drop_pending_updates,
                close_loop=True,
                stop_signals=None,
            )
        else:
            try:
                asyncio.run(
                    TelegramBotManager._run_application_lifecycle(
                        self,
                        drop_pending_updates=drop_pending_updates,
                        timeout=timeout,
                    )
                )
            except BaseException:
                self.logger.exception("Polling thread crashed")
                raise
            finally:
                self._manual_polling_stop_event = None
                shutdown_done = getattr(self, "_shutdown_complete_event", None)
                if shutdown_done is not None and not shutdown_done.is_set():
                    shutdown_done.set()

    def graceful_stop(self):
        """Gracefully stop the bot"""
        self.logger.info("Starting graceful shutdown...")

        # Log pending tasks count before stopping
        pending_count = 0
        if hasattr(self, '_send_queues'):
            with self._send_queues_lock:
                pending_count = sum(len(q) for q in self._send_queues.values())
            pending_count += len(self._send_in_flight)

        if pending_count > 0:
            self.logger.info("Found %d pending queued send tasks", pending_count)

        # Stop the queued send worker first
        self.stop_queued_worker()

        # Shut down auxiliary bot pool
        if self.bot_pool:
            self.bot_pool.shutdown()

        # Then stop the PTB application loop
        self.logger.debug("Stopping Telegram application...")
        if hasattr(self, 'application'):
            manual_evt = getattr(self, "_manual_polling_stop_event", None)
            if manual_evt is not None:
                def _signal_manual_stop() -> None:
                    manual_evt.set()

                stop_requested = False
                if hasattr(self, '_runtime'):
                    stop_requested = self._runtime.call_soon(_signal_manual_stop)

                if not stop_requested:
                    manual_evt_loop = getattr(manual_evt, "_loop", None)
                    if manual_evt_loop is not None and manual_evt_loop.is_running():
                        manual_evt_loop.call_soon_threadsafe(_signal_manual_stop)
                        stop_requested = True

                if not stop_requested:
                    self.logger.warning(
                        "Could not schedule polling stop on runtime loop; "
                        "falling back to direct stop signalling."
                    )
                    try:
                        self.application.stop_running()
                    except RuntimeError as exc:
                        self.logger.debug("Telegram application loop not ready for stop_running(): %s", exc)
                    try:
                        manual_evt.set()
                    except Exception:
                        self.logger.debug("Failed to set manual polling stop event directly.", exc_info=True)
            else:
                application_stopped = False
                if hasattr(self, '_runtime') and self._runtime._ready.is_set():
                    try:
                        self._runtime.call(self._shutdown_ptb_application(), timeout=30)
                        application_stopped = True
                    except Exception as exc:
                        self.logger.warning("PTB shutdown coroutine did not complete cleanly: %s", exc)

                if not application_stopped:
                    stop_requested = False
                    if hasattr(self, '_runtime'):
                        stop_requested = self._runtime.call_soon(self.application.stop_running)
                    if not stop_requested:
                        try:
                            self.application.stop_running()
                        except RuntimeError as exc:
                            self.logger.debug("Telegram application loop not ready for stop_running(): %s", exc)

            if hasattr(self, "_shutdown_complete_event"):
                if not self._shutdown_complete_event.wait(timeout=30):
                    self.logger.warning(
                        "Telegram post_shutdown hook did not fire within 30s; "
                        "the next polling instance may see a Conflict from Telegram."
                    )
        if hasattr(self, '_runtime'):
            if getattr(self._runtime, '_owns_loop_thread', False):
                self._runtime.shutdown()
            else:
                self._runtime.clear_loop()
        self.logger.info("Graceful shutdown completed")

    def __del__(self):
        """Ensure cleanup on object destruction"""
        try:
            if hasattr(self, '_send_worker_stop') and hasattr(self, '_send_worker_thread'):
                if not self._send_worker_stop.is_set():
                    self._send_worker_stop.set()
                if self._send_worker_thread.is_alive():
                    self._send_worker_thread.join(timeout=1)
        except Exception:
            # Don't raise exceptions in __del__
            pass

    def _detect_empty_file(self, file, chat, caption, prefix, suffix, message_thread_id=None):
        empty = True
        if isinstance(file, str):
            stat_path = url2pathname(urlparse(file).path) if file.startswith('file://') else file
            empty = os.stat(stat_path).st_size == 0
        elif hasattr(file, "seekable"):
            try:
                if hasattr(file, 'closed') and file.closed:
                    empty = True
                elif file.seekable():
                    file.seek(0, 2)
                    empty = file.tell() == 0
                    file.seek(0, 0)
                else:
                    empty = True
            except (ValueError, OSError):
                empty = True
        elif isinstance(file, InputFile):
            empty = not bool(len(file.input_file_content))
        if empty:
            kwargs = {'prefix': self._("Empty attachment detected.") + prefix, 'text': caption, 'suffix': suffix}
            if message_thread_id is not None:
                kwargs['message_thread_id'] = message_thread_id
            return self.send_message(chat, **kwargs)
