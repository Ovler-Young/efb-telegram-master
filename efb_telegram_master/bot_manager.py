# coding=utf-8
import asyncio
import collections
import html
import io
import logging
import os
import threading
import time
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


class DelayedTask(NamedTuple):
    """Represents a delayed message task."""
    execute_time: float  # When to execute (timestamp)
    chat_id: int        # Target chat ID
    function: Callable  # Function to execute
    args: tuple        # Function arguments
    kwargs: dict       # Function keyword arguments
    task_id: str       # Unique task identifier
    arrival_seq: int = 0  # Monotonic arrival order; preserved across requeues
    cleanup_files: list = []  # Files to delete after task completes

if TYPE_CHECKING:
    from . import TelegramChannel

MAX_CALLBACK_QUERY_ANSWER_LENGTH = 200
P = ParamSpec("P")
T = TypeVar("T")
BotMethod: TypeAlias = Callable[..., object]


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
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout)


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
    is_delayed: bool = True
    delay_time: float = 0.0
    _delayed_execution_pending: bool = True
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


class TelegramBotManager(LocaleMixin):
    """
    This is a wrapper of Telegram's message sending and editing methods.
    Used to deal with text/caption length overflow, parse_mode, document fallback, etc.

    Attributes:
        me (telegram.User): Telegram User
        admins (List[int]): List of admin user IDs.
        updater (telegram.ext.Updater): Updater of the bot
        dispatcher (telegram.ext.Dispatcher): Dispatcher of the updater
    """

    webhook = False
    logger = logging.getLogger(__name__)

    # Type declarations for instance attributes assigned in __init__
    application: Application
    _bot: SyncBotProtocol
    _async_bot: telegram.Bot
    me: Optional[User]
    admins: List[int]
    dispatcher: Application
    bot_pool: Optional['BotPool']
    _delayed_worker_stop: threading.Event
    _chat_queues_lock: threading.Lock
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
                # Bypass: caller already reserved a slot and set _using_bot
                if kwargs.pop('_bypass_rate_limit', False):
                    return fn(self, *args, **kwargs)

                sender_bot_id = kwargs.pop('_sender_bot_id', None)
                send_mode = kwargs.pop('_send_mode', 'blocking')
                force_main_bot = kwargs.pop('_force_main_bot', False)
                force_sender_known = False
                forced_sender_bot_id = None

                chat_id = None
                if args:
                    chat_id = args[0]
                elif 'chat_id' in kwargs:
                    chat_id = kwargs['chat_id']

                # if _delayed_worker_stop is set, we should not schedule new tasks
                if self._delayed_worker_stop.is_set():
                    self.logger.warning(f"Delayed worker is stopped. Not scheduling new tasks for chat {chat_id}.")
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

                if sender_bot_id and self.bot_pool:
                    aux_bot = self.bot_pool.get_bot_by_id(sender_bot_id)
                    if aux_bot and not aux_bot.disabled:
                        try:
                            with self._using_bot(aux_bot.bot):
                                result = fn(self, *args, **kwargs)
                            return self._make_send_receipt(result, sender_bot_id=str(sender_bot_id))
                        except telegram.error.Forbidden:
                            if chat_id is not None:
                                aux_bot.update_membership(int(chat_id), False)
                            self.logger.warning(
                                "Auxiliary bot %s got Forbidden in chat %s during forced-route API call; "
                                "marking it as non-member for this chat.",
                                sender_bot_id, chat_id,
                            )
                    return self._make_send_receipt(fn(self, *args, **kwargs))

                if chat_id:
                    has_callback = _has_callback_keyboard(kwargs.get('reply_markup'))
                    cleanup_files = getattr(self._cleanup_tls, 'pending_cleanup', [])[:]
                    self._cleanup_tls.pending_cleanup = []

                    if send_mode == 'eventual':
                        if force_sender_known:
                            kwargs = dict(kwargs)
                            kwargs['_force_sender_known'] = True
                            kwargs['_force_sender_bot_id'] = forced_sender_bot_id
                        return self._schedule_eventual_send(
                            chat_id,
                            fn,
                            (self,) + args,
                            kwargs,
                            cleanup_files=cleanup_files,
                        )

                    message_thread_id = kwargs.get('message_thread_id')
                    if force_main_bot:
                        bot, chosen_sender_bot_id, delay_time = self._bot, None, 0.0
                    elif force_sender_known:
                        bot, chosen_sender_bot_id, delay_time = self._select_forced_sender(
                            chat_id, forced_sender_bot_id,
                        )
                    else:
                        bot, chosen_sender_bot_id, delay_time = self._select_sender(
                            chat_id,
                            has_callback=has_callback,
                            message_thread_id=message_thread_id,
                        )
                    if chosen_sender_bot_id is None:
                        delay_time, _, _ = self._calculate_rate_limit_delay(chat_id)
                    if delay_time > 0:
                        time.sleep(delay_time)
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
                                delay_time, _, _ = self._calculate_rate_limit_delay(chat_id)
                                if delay_time > 0:
                                    time.sleep(delay_time)
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
                        if hasattr(self, '_delayed_worker_stop'):
                            # Sleep in small chunks to allow for interruption during shutdown
                            remaining_seconds = retry_after
                            while remaining_seconds > 0 and not self._delayed_worker_stop.is_set():
                                sleep_chunk = min(1.0, remaining_seconds)
                                time.sleep(sleep_chunk)
                                remaining_seconds -= sleep_chunk
                        else:
                            time.sleep(retry_after)
                    except telegram.error.TelegramError as e:
                        if not cls.enable_retry:
                            raise
                        if "Too Many Requests" in str(e) or "429" in str(e) or "Flood" in str(e):
                            timestamp_info = get_timestamp_info()
                            if attempt >= max_retries:
                                cls.logger.error(f"Max retries exceeded for rate limit error: {e} (chat_id: {chat_id}){timestamp_info}")
                                raise

                            delay = 60
                            cls.logger.warning(f"Rate limit detected, waiting {delay}s before retry {attempt + 1}/{max_retries} (chat_id: {chat_id}){timestamp_info}")

                            # Use interruptible sleep for rate limit waits
                            if hasattr(self, '_delayed_worker_stop'):
                                # Sleep in small chunks to allow for interruption during shutdown
                                remaining_seconds = float(delay)
                                while remaining_seconds > 0 and not self._delayed_worker_stop.is_set():
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
                with self._chat_queues_lock:
                    if any(self._chat_queues.values()) or self._chat_in_flight:
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

    def __init__(self, channel: 'TelegramChannel'):
        self.channel: 'TelegramChannel' = channel
        config = self.channel.config

        req_kwargs = {'read_timeout': 15.0}
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
        # Per-chat FIFO queues + thread pool for concurrent dispatch.
        # Same chat_id: sends are serial (ordering guarantee).
        # Different chat_ids: sends run in parallel threads.
        from collections import deque as _deque
        from concurrent.futures import ThreadPoolExecutor, Future

        self._chat_queues: dict[int, _deque[DelayedTask]] = {}
        self._chat_queues_lock = threading.Lock()
        self._tasks_scheduled = 0  # monotonic counter for diagnostics
        self._delayed_worker_stop = threading.Event()

        # Per-chat concurrency tracking
        self._chat_in_flight: dict[int, tuple] = {}  # chat_id -> (Future, task, sender_bot_id)
        self._chat_frozen_until: dict[int, float] = {}  # chat_id -> when ALL bots are rate-limited
        self._bot_chat_frozen_until: dict[tuple, float] = {}  # (bot_id|None, chat_id) -> RetryAfter deadline

        # Per-chat send interval (200ms safety gap)
        self._last_send_by_chat: dict[int, float] = {}
        self.CHAT_SEND_INTERVAL = 0.2  # seconds

        # Thread pool for non-blocking sends
        self._send_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="ETM-send",
        )

        # Rendezvous dicts for delayed DB updates — with timestamps for TTL cleanup
        # task_id -> (etm_msg, old_msg_id, registered_at)
        self._pending_delayed_logs: dict[str, tuple] = {}
        # task_id -> (real_tg_msg, sender_bot_id, completed_at)
        self._completed_delayed_results: dict[str, tuple] = {}
        self._pending_logs_lock = threading.Lock()
        self._rendezvous_ttl = 600.0          # 10 minutes
        self._rendezvous_cleanup_interval = 300.0  # 5 minutes
        self._last_rendezvous_cleanup = time.time()

        # DB write retry queue
        self._db_retry_queue: list[tuple] = []
        self._db_retry_lock = threading.Lock()
        self._db_max_retries = 5

        self._delayed_worker_thread = threading.Thread(
            target=self._delayed_message_worker,
            name="ETM delayed messages worker",
            daemon=True
        )
        self._delayed_worker_thread.start()
        self.logger.debug("Delayed message system initialized...")

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

    def _schedule_eventual_send(
        self,
        chat_id: int,
        function: Callable,
        args: tuple,
        kwargs: dict,
        *,
        cleanup_files: Optional[list] = None,
        delay_time: float = 0.0,
    ) -> SendReceipt:
        task_id = self._schedule_delayed_task(
            chat_id=chat_id,
            delay_time=delay_time,
            function=function,
            args=args,
            kwargs=kwargs,
            cleanup_files=cleanup_files,
        )
        placeholder = self._create_delayed_message_placeholder(chat_id, delay_time, task_id)
        return self._make_send_receipt(placeholder, queued=True, task_id=task_id)

    def _select_sender(self, chat_id: int, *, has_callback: bool = False, message_thread_id: Optional[int] = None):
        """Choose the earliest sender using the current main/aux heuristics."""
        main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)
        if self.bot_pool and not has_callback:
            slot = self.bot_pool.acquire_send_slot(
                chat_id,
                max_delay=main_delay if main_delay > 0 else 0.0,
                affinity_key=(chat_id, message_thread_id),
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
                return aux_bot.bot, str(sender_bot_id), 0.0
            self.logger.warning(
                "Reply target sender bot %s is unavailable for chat %s; falling back to main bot.",
                sender_bot_id, chat_id,
            )
        main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)
        return self._bot, None, main_delay

    def _select_unfrozen_sender(self, chat_id: int, *, has_callback: bool = False,
                                message_thread_id: Optional[int] = None, now: float = 0.0):
        """Like _select_sender, but skips bots frozen by Telegram RetryAfter.

        Returns (bot, bot_id, delay) or (None, None, soonest_unfreeze_delay)
        when every candidate is frozen.
        """
        now = now or time.time()
        main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)

        # ── Try aux bots first (skip frozen ones) ──
        if self.bot_pool and not has_callback:
            # Allow aux bots to participate even when main bot isn't delayed,
            # but only notify admin when the main bot is actually rate-limited.
            max_delay = max(main_delay, 0.01)
            slot = self.bot_pool.acquire_send_slot(
                chat_id,
                max_delay=max_delay,
                skip_bot=lambda aux_bot: self._bot_chat_frozen_until.get((str(aux_bot.bot_id), chat_id), 0.0) > now,
                affinity_key=(chat_id, message_thread_id),
                notify_admin=(main_delay > 0),
            )
            if slot is not None:
                aux_bot_obj, aux_delay = slot
                return aux_bot_obj.bot, str(aux_bot_obj.bot_id), aux_delay

        # ── Try main bot ──
        main_frozen = self._bot_chat_frozen_until.get((None, chat_id), 0.0)
        if main_frozen <= now:
            if main_delay <= 0:
                self._calculate_rate_limit_delay(chat_id)  # reserve
                return self._bot, None, 0.0
            # Main bot available but rate-limited — return its delay
            return self._bot, None, main_delay

        # ── All bots frozen ──
        # Find earliest unfreeze time so the caller knows when to retry
        soonest = float('inf')
        for key, until in self._bot_chat_frozen_until.items():
            if key[1] == chat_id and until > now:
                soonest = min(soonest, until - now)
        return None, None, soonest if soonest != float('inf') else 0.5

    def _select_forced_unfrozen_sender(self, chat_id: int, sender_bot_id: Optional[str], *, now: float = 0.0):
        """Select the reply target's sender bot, respecting RetryAfter freezes."""
        now = now or time.time()
        if sender_bot_id and self.bot_pool:
            frozen_until = self._bot_chat_frozen_until.get((str(sender_bot_id), chat_id), 0.0)
            if frozen_until > now:
                return None, None, frozen_until - now
            aux_bot = self.bot_pool.get_bot_by_id(sender_bot_id)
            if aux_bot and not aux_bot.disabled:
                return aux_bot.bot, str(sender_bot_id), 0.0
            self.logger.warning(
                "Reply target sender bot %s is unavailable for delayed chat %s; falling back to main bot.",
                sender_bot_id, chat_id,
            )

        main_frozen = self._bot_chat_frozen_until.get((None, chat_id), 0.0)
        if main_frozen > now:
            return None, None, main_frozen - now
        main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)
        if main_delay > 0:
            return self._bot, None, main_delay
        self._calculate_rate_limit_delay(chat_id)
        return self._bot, None, 0.0

    def _requeue_delayed_task(self, task: DelayedTask, delay_time: float):
        """Put a task back at the front of its chat queue and freeze that chat."""
        with self._chat_queues_lock:
            q = self._chat_queues.setdefault(task.chat_id, collections.deque())
            q.appendleft(task)
        if delay_time > 0:
            self._chat_frozen_until[task.chat_id] = time.time() + delay_time

    def _init_bot_pool(self, aux_configs: list, config: dict, channel: 'TelegramChannel'):
        """Initialize the auxiliary bot pool from config."""
        req_kwargs = {'read_timeout': 15.0}
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
            tuple: (delay_time, chat_count, global_count)
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

    def _create_delayed_message_placeholder(self, chat_id: int, delay_time: float, task_id: str):
        """
        Create a placeholder message object for delayed execution.

        Args:
            chat_id: Telegram chat ID
            delay_time: Delay time in seconds
            task_id: Unique task identifier

        Returns:
            A mock message object indicating delayed execution
        """

        placeholder = QueuedSendPlaceholder(
            chat_id=chat_id,
            message_id=int(time.time() * 1000),
            date=int(time.time()),
            text=f"[Message scheduled for delivery in {delay_time:.2f}s due to rate limiting]",
            task_id=task_id,
            delay_time=delay_time,
        )
        self.logger.debug(f"Created delayed message placeholder for chat {chat_id} with {delay_time:.2f}s delay")
        return placeholder

    def _schedule_delayed_task(self, chat_id: int, delay_time: float, function: Callable,
                              args: tuple, kwargs: dict, cleanup_files: Optional[list] = None) -> str:
        """Append a task to the per-chat FIFO queue."""
        task_id = f"{chat_id}_{str(time.time_ns())}_{id(function)}"

        task = DelayedTask(
            execute_time=time.time() + delay_time,
            chat_id=chat_id,
            function=function,
            args=args,
            kwargs=kwargs,
            task_id=task_id,
            cleanup_files=cleanup_files or []
        )

        with self._chat_queues_lock:
            q = self._chat_queues.setdefault(chat_id, collections.deque())
            q.append(task)
            self._tasks_scheduled += 1

        if delay_time > 0:
            self._chat_frozen_until[chat_id] = max(
                self._chat_frozen_until.get(chat_id, 0.0),
                time.time() + delay_time,
            )

        self.logger.debug(f"Scheduled delayed task {task_id} for chat {chat_id} in {delay_time:.2f}s")
        return task_id

    # ── Async-dispatch delayed message worker ──────────────────────

    def _delayed_message_worker(self):
        """Worker thread: dispatch sends to a thread pool.

        Same chat_id → serial (one in-flight at a time, preserves order).
        Different chat_ids → parallel (thread pool).
        The worker thread itself never blocks on HTTP; it only orchestrates.
        """
        self.logger.debug("Delayed message worker started")

        while not self._delayed_worker_stop.is_set():
            try:
                now = time.time()

                # ── 1. Harvest completed sends ──
                self._harvest_completed_sends()

                # ── 2. Dispatch ready tasks ──
                with self._chat_queues_lock:
                    dispatchable_chats = [
                        cid for cid, q in self._chat_queues.items()
                        if q and cid not in self._chat_in_flight
                    ]

                for chat_id in dispatchable_chats:
                    if self._delayed_worker_stop.is_set():
                        break

                    # Frozen (RetryAfter / rate limit)?
                    if self._chat_frozen_until.get(chat_id, 0.0) > now:
                        continue

                    # Per-chat 200ms interval
                    last_sent = self._last_send_by_chat.get(chat_id, 0.0)
                    if now - last_sent < self.CHAT_SEND_INTERVAL:
                        continue

                    # Peek at front task
                    with self._chat_queues_lock:
                        q = self._chat_queues.get(chat_id)
                        if not q:
                            continue
                        task = q[0]
                        if task.execute_time > now:
                            continue
                        task = q.popleft()

                    # Select sender, skipping (bot, chat) pairs frozen by RetryAfter
                    if task.kwargs.get('_force_sender_known'):
                        sender_bot, sender_bot_id, delay_time = self._select_forced_unfrozen_sender(
                            task.chat_id,
                            task.kwargs.get('_force_sender_bot_id'),
                            now=now,
                        )
                    else:
                        sender_bot, sender_bot_id, delay_time = self._select_unfrozen_sender(
                            task.chat_id,
                            has_callback=_has_callback_keyboard(task.kwargs.get('reply_markup')),
                            message_thread_id=task.kwargs.get('message_thread_id'),
                            now=now,
                        )
                    if sender_bot is None:
                        # Every bot is frozen or rate-limited for this chat — put task back
                        self._requeue_delayed_task(task, delay_time if delay_time > 0 else 0.5)
                        continue
                    if delay_time > 0:
                        self._requeue_delayed_task(task, delay_time)
                        continue

                    # Submit to thread pool — non-blocking
                    self._dispatch_send(task, sender_bot, sender_bot_id)

                # ── 3. Housekeeping ──
                self._process_db_retry_queue()

                # Purge expired freeze entries
                if self._bot_chat_frozen_until:
                    expired = [k for k, v in self._bot_chat_frozen_until.items() if v <= now]
                    for k in expired:
                        del self._bot_chat_frozen_until[k]
                if self._chat_frozen_until:
                    expired = [k for k, v in self._chat_frozen_until.items() if v <= now]
                    for k in expired:
                        del self._chat_frozen_until[k]

                if now - self._last_rendezvous_cleanup > self._rendezvous_cleanup_interval:
                    self._cleanup_stale_rendezvous()
                    self._last_rendezvous_cleanup = now

                self._delayed_worker_stop.wait(timeout=0.05)

            except Exception as e:
                self.logger.exception(f"Error in delayed message worker: {e}")
                self._delayed_worker_stop.wait(timeout=1)

        # Shutdown: wait for in-flight sends to finish
        for chat_id, (future, task, _bot_id) in list(self._chat_in_flight.items()):
            try:
                future.result(timeout=10)
            except Exception:
                pass
        self._send_executor.shutdown(wait=False)
        self.logger.debug("Delayed message worker stopped")

    def _dispatch_send(self, task: DelayedTask, sender_bot, sender_bot_id: Optional[str]):
        """Submit a send operation to the thread pool."""

        def _do_send():
            send_kwargs = dict(task.kwargs)
            send_kwargs.pop('_force_sender_known', None)
            send_kwargs.pop('_force_sender_bot_id', None)
            with self._using_bot(sender_bot):
                return task.function(*task.args, **send_kwargs)

        future = self._send_executor.submit(_do_send)
        self._chat_in_flight[task.chat_id] = (future, task, sender_bot_id)

    def _harvest_completed_sends(self):
        """Check all in-flight futures; handle success / errors."""
        completed = [(cid, ft) for cid, ft in self._chat_in_flight.items() if ft[0].done()]

        for chat_id, (future, task, sender_bot_id) in completed:
            del self._chat_in_flight[chat_id]
            should_cleanup = True

            try:
                result = future.result()  # already done, won't block

                if sender_bot_id is not None:
                    self._record_aux_use(task.chat_id)

                self._last_send_by_chat[task.chat_id] = time.time()
                self.logger.debug(f"Delayed task {task.task_id} completed successfully")

                if result and hasattr(result, 'message_id'):
                    self._handle_delayed_database_update(
                        task.task_id, result, sender_bot_id=sender_bot_id,
                    )

            except telegram.error.RetryAfter as e:
                should_cleanup = False
                retry_after_value = e.retry_after
                retry_after = (
                    retry_after_value.total_seconds()
                    if isinstance(retry_after_value, timedelta)
                    else float(retry_after_value)
                )
                self.logger.warning(
                    "RetryAfter for bot %s in chat %d (task %s): %.2fs — other bots can still send",
                    sender_bot_id or "main", chat_id, task.task_id, retry_after,
                )
                # Freeze only THIS bot for THIS chat; other bots remain available
                self._bot_chat_frozen_until[(sender_bot_id, chat_id)] = time.time() + retry_after
                # Put task back at front — next dispatch will try a different bot
                with self._chat_queues_lock:
                    q = self._chat_queues.setdefault(chat_id, collections.deque())
                    q.appendleft(task)

            except telegram.error.BadRequest as e:
                self.logger.warning(
                    "Non-retryable BadRequest for delayed task %s, dropping: %s "
                    "(chat_id=%s, reply_to_message_id=%s, message_thread_id=%s, method=%s)",
                    task.task_id, e, task.chat_id,
                    task.kwargs.get("reply_to_message_id"),
                    task.kwargs.get("message_thread_id"),
                    getattr(task.function, "__name__", repr(task.function)),
                )

            except (telegram.error.TimedOut, telegram.error.NetworkError) as e:
                should_cleanup = False
                self.logger.warning(
                    "Transient error for delayed task %s, retrying: %s",
                    task.task_id, e,
                )
                self._requeue_delayed_task(task, 1.0)

            except telegram.error.Forbidden as e:
                if sender_bot_id and self.bot_pool:
                    aux_bot = self.bot_pool.get_bot_by_id(sender_bot_id)
                    if aux_bot:
                        aux_bot.update_membership(chat_id, False)
                        should_cleanup = False
                        self.logger.warning(
                            "Aux bot %s got Forbidden in chat %s for delayed task %s; "
                            "marking it as non-member for this chat and retrying.",
                            sender_bot_id, chat_id, task.task_id,
                        )
                        self._requeue_delayed_task(task, 0.0)
                        continue
                self.logger.exception(f"Error executing delayed task {task.task_id}: {e}")

            except Exception as e:
                self.logger.exception(f"Error executing delayed task {task.task_id}: {e}")

            finally:
                if should_cleanup:
                    for path in task.cleanup_files:
                        try:
                            os.unlink(path)
                            self.logger.debug("Cleaned up delayed task temp file: %s", path)
                        except OSError as e:
                            self.logger.warning("Failed to clean up temp file %s: %s", path, e)

    def _finalize_delayed_database_update(
        self,
        etm_msg,
        old_msg_id,
        real_tg_msg: TelegramMessage,
        *,
        sender_bot_id: Optional[str] = None,
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
        except Exception as e:
            self.logger.warning(
                "DB write failed for tg_msg %s, queuing for retry: %s",
                getattr(real_tg_msg, 'message_id', '?'),
                e,
            )
            TelegramBotManager._enqueue_db_retry(
                self, etm_msg, old_msg_id, real_tg_msg, sender_bot_id
            )

    def _enqueue_db_retry(self, etm_msg, old_msg_id, real_tg_msg, sender_bot_id, attempt: int = 0):
        """Push a failed DB write into the retry queue with exponential back-off."""
        next_retry_at = time.time() + min(2 ** attempt, 30)  # cap at 30 s
        with self._db_retry_lock:
            self._db_retry_queue.append(
                (etm_msg, old_msg_id, real_tg_msg, sender_bot_id, attempt + 1, next_retry_at)
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

        for etm_msg, old_msg_id, real_tg_msg, sender_bot_id, attempt, next_retry_at in items:
            if current_time < next_retry_at:
                still_pending.append((etm_msg, old_msg_id, real_tg_msg, sender_bot_id, attempt, next_retry_at))
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
                        self, etm_msg, old_msg_id, real_tg_msg, sender_bot_id, attempt
                    )

        if still_pending:
            with self._db_retry_lock:
                self._db_retry_queue.extend(still_pending)

    def _cleanup_stale_rendezvous(self):
        """Remove rendezvous entries that were never paired within the TTL."""
        cutoff = time.time() - self._rendezvous_ttl
        with self._pending_logs_lock:
            stale_logs = [
                tid for tid, entry in self._pending_delayed_logs.items()
                if len(entry) >= 3 and entry[2] < cutoff
            ]
            for tid in stale_logs:
                del self._pending_delayed_logs[tid]
                self.logger.warning("Rendezvous TTL: dropped stale pending log for task %s", tid)

            stale_results = [
                tid for tid, entry in self._completed_delayed_results.items()
                if len(entry) >= 3 and entry[2] < cutoff
            ]
            for tid in stale_results:
                del self._completed_delayed_results[tid]
                self.logger.warning("Rendezvous TTL: dropped stale completed result for task %s", tid)

    def submit_async_db_write(
        self,
        etm_msg,
        real_tg_msg: TelegramMessage,
        old_msg_id=None,
        *,
        sender_bot_id: Optional[str] = None,
    ):
        """Fire-and-forget database write, usable from both blocking and eventual paths.

        Failures are caught and routed to the retry queue — callers should
        NEVER re-send to Telegram on a DB-write error.
        """
        self._finalize_delayed_database_update(
            etm_msg, old_msg_id, real_tg_msg, sender_bot_id=sender_bot_id,
        )

    def register_delayed_database_update(self, task_id: str, etm_msg, old_msg_id=None):
        """
        Register a pending database update for a delayed task.

        Args:
            task_id: Task identifier from delayed execution
            etm_msg: ETMMsg object to log
            old_msg_id: Optional old message ID for updates
        """
        completed_result: Optional[tuple]
        with self._pending_logs_lock:
            completed_result = self._completed_delayed_results.pop(task_id, None)
            if completed_result is None:
                self._pending_delayed_logs[task_id] = (etm_msg, old_msg_id, time.time())
                self.logger.debug(f"Registered delayed database update for task {task_id}")
                return

        real_tg_msg, sender_bot_id = completed_result[0], completed_result[1]
        self.logger.debug(f"Finalizing already-completed delayed task {task_id}")
        self._finalize_delayed_database_update(
            etm_msg,
            old_msg_id,
            real_tg_msg,
            sender_bot_id=sender_bot_id,
        )

    def _handle_delayed_database_update(
        self,
        task_id: str,
        real_tg_msg,
        *,
        sender_bot_id: Optional[str] = None,
    ):
        """
        Handle database update when delayed task completes.

        Args:
            task_id: Task identifier
            real_tg_msg: The real Telegram message that was sent
        """
        pending_update = None
        with self._pending_logs_lock:
            pending_update = self._pending_delayed_logs.pop(task_id, None)
            if pending_update is None:
                self._completed_delayed_results[task_id] = (real_tg_msg, sender_bot_id, time.time())
                self.logger.debug(f"Stored delayed result for task {task_id} until database registration is ready")
                return

        etm_msg, old_msg_id = pending_update[0], pending_update[1]
        self._finalize_delayed_database_update(
            etm_msg,
            old_msg_id,
            real_tg_msg,
            sender_bot_id=sender_bot_id,
        )
        self.logger.debug(f"Updated database with real message for delayed task {task_id}")

    def stop_delayed_worker(self):
        """Stop the delayed message worker thread."""
        self.logger.debug("Stopping delayed message worker...")

        if hasattr(self, '_delayed_worker_stop'):
            self._delayed_worker_stop.set()

        if hasattr(self, '_delayed_worker_thread') and self._delayed_worker_thread.is_alive():
            self.logger.debug("Waiting for delayed message worker to stop...")
            self._delayed_worker_thread.join(timeout=5)

            if self._delayed_worker_thread.is_alive():
                self.logger.warning("Delayed message worker did not stop gracefully within timeout")
            else:
                self.logger.debug("Delayed message worker stopped successfully")

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

    @Decorators.retry_on_timeout
    @Decorators.caption_affix_decorator
    @Decorators.retry_on_chat_migration
    def edit_message_caption(self, *args, **kwargs):
        return self._active_bot.edit_message_caption(*args, **kwargs)

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
        if hasattr(self, '_chat_queues'):
            with self._chat_queues_lock:
                pending_count = sum(len(q) for q in self._chat_queues.values())
            pending_count += len(self._chat_in_flight)

        if pending_count > 0:
            self.logger.info(f"Found {pending_count} pending delayed tasks")

        # Stop the delayed message worker first
        self.stop_delayed_worker()

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
            if hasattr(self, '_delayed_worker_stop') and hasattr(self, '_delayed_worker_thread'):
                if not self._delayed_worker_stop.is_set():
                    self._delayed_worker_stop.set()
                if self._delayed_worker_thread.is_alive():
                    self._delayed_worker_thread.join(timeout=1)
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
