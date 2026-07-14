# coding=utf-8
from __future__ import annotations

import asyncio
import collections
import collections.abc
import html
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from typing import TYPE_CHECKING, BinaryIO, Callable, Collection, Coroutine, List, Literal, Mapping, NamedTuple, Optional, ParamSpec, Protocol, TypeAlias, Tuple, TypeVar, cast
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import url2pathname
from unittest.mock import Mock, patch

import telegram.constants
import telegram.error
from telegram import File, ForumTopic, InlineKeyboardMarkup, InputFile, Update, User
from telegram import Message as TelegramMessage
from telegram.ext import Application, CallbackContext, MessageHandler, TypeHandler
from telegram.ext import _applicationbuilder as ptb_applicationbuilder
from telegram.request import HTTPXRequest

from .auxiliary_bot import AuxiliaryBot
from .bot_pool import BotPool
from .locale_mixin import LocaleMixin
from .msg_type import get_msg_type
from .outbound import (
    OutboundQueue,
    OutboundQueueScheduler,
    QueueEnqueueError,
    QueueRequest,
    SchedulerStoppedError,
    SenderSelection,
    SenderSelectionResult,
)
from .ptb_compat import Filters
from .utils import TelegramChatID, TelegramMessageID, message_id_to_str


SendTarget: TypeAlias = Tuple[str, int]
BotChatKey: TypeAlias = Tuple[Optional[str], int]


class QueuedDbLogContext(NamedTuple):
    """Database log context carried by a queued send task."""
    etm_msg: 'ETMMsg'
    old_msg_id: Optional['OldMsgID'] = None
    on_complete: Optional[Callable[[], None]] = None


if TYPE_CHECKING:
    from . import TelegramChannel
    from .message import ETMMsg
    from .utils import OldMsgID

MAX_CALLBACK_QUERY_ANSWER_LENGTH = 200
P = ParamSpec("P")
T = TypeVar("T")
BotMethod: TypeAlias = Callable[..., object]
_INTERNAL_KWARGS = frozenset({
    '_sender_bot_id',
    '_slave_id',
    '_send_mode',
    '_force_main_bot',
    '_required_sender_bot_id',
    '_queued_db_log_context',
})


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
    durable_db_logged: bool = False

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
        application (telegram.ext.Application): PTB application of the bot.
        dispatcher (telegram.ext.Application): Dispatcher-compatible application instance.
    """

    webhook = False
    logger = logging.getLogger(__name__)
    DEFAULT_SEND_WORKER_COUNT = 8
    DEFAULT_HTTPX_POOL_MULTIPLIER = 2.0
    HTTPX_POOL_MULTIPLIER_ENV = "ETM_HTTPX_POOL_MULTIPLIER"
    BLOCKING_SEND_TIMEOUT = 300.0
    BLOCKING_SEND_TARGET_SLAVE_ID = "__blocking__"
    TELEGRAM_RETRY_AFTER_GRACE_SECONDS = 5.0
    TELEGRAM_RETRY_AFTER_REPEATED_FLOOR_SECONDS = 60.0
    TELEGRAM_RETRY_AFTER_BACKOFF_CAP_SECONDS = 900.0
    MEMBERSHIP_RECHECK_SECONDS = 0.25
    SHUTDOWN_DRAIN_TIMEOUT = 5.0
    SHUTDOWN_JOIN_GRACE = 1.0

    # Type declarations for instance attributes assigned in __init__
    application: Application
    _bot: SyncBotProtocol
    _async_bot: telegram.Bot
    me: Optional[User]
    admins: List[int]
    dispatcher: Application
    bot_pool: Optional['BotPool']
    _send_worker_stop: threading.Event
    _stopping: threading.Event
    _cleanup_tls: threading.local
    _aux_recent_use: dict[int, float]

    class Decorators:
        logger = logging.getLogger(__name__)

        enable_retry = False

        @classmethod
        def exception_filter(cls, exception: Exception):
            cls.logger.exception("Exception: %s while sending request to Telegram server.", exception)
            return isinstance(exception, telegram.error.TimedOut)

        @classmethod
        def rate_limit_decorator(cls, fn: Callable):
            """Apply rate limiting and sender routing for outbound API calls."""
            @wraps(fn)
            def rate_limit_wrapper(self: 'TelegramBotManager', *args, **kwargs):
                is_edit_method = fn.__name__.startswith('edit_message_')

                sender_bot_id = kwargs.pop('_sender_bot_id', None)
                slave_id = kwargs.pop('_slave_id', None)
                send_mode = kwargs.pop('_send_mode', 'blocking')
                force_main_bot = kwargs.pop('_force_main_bot', False)
                required_sender_bot_id = str(sender_bot_id) if sender_bot_id and is_edit_method else None

                chat_id = None
                if args:
                    chat_id = args[0]
                elif 'chat_id' in kwargs:
                    chat_id = kwargs['chat_id']
                has_callback = _has_callback_keyboard(kwargs.get('reply_markup'))

                send_worker_stop = getattr(self, '_send_worker_stop', None)
                if send_worker_stop is not None and send_worker_stop.is_set():
                    self.logger.warning("Durable outbound worker is stopped; rejecting work for chat %s.", chat_id)
                    return None

                if chat_id:
                    chat_id_int = int(chat_id)
                    cleanup_tls = getattr(self, '_cleanup_tls', None)
                    cleanup_files = getattr(cleanup_tls, 'pending_cleanup', [])[:]
                    if cleanup_tls is not None:
                        cleanup_tls.pending_cleanup = []

                    if send_mode == 'eventual':
                        if not slave_id:
                            self.logger.warning(
                                "Eventual send requested for chat %s without _slave_id; falling back to blocking.",
                                chat_id,
                            )
                        elif not is_edit_method and not has_callback:
                            return self._enqueue_eventual_send(
                                str(slave_id),
                                int(chat_id),
                                fn,
                                (self,) + args,
                                kwargs,
                                cleanup_files=cleanup_files,
                            )

                    blocking_kwargs = dict(kwargs)
                    if required_sender_bot_id is not None:
                        blocking_kwargs['_required_sender_bot_id'] = required_sender_bot_id
                    if force_main_bot or (has_callback and not is_edit_method) or (
                        is_edit_method and required_sender_bot_id is None
                    ):
                        blocking_kwargs['_required_sender_bot_id'] = "__main__"

                    return self._enqueue_blocking_send_and_wait(
                        str(slave_id) if slave_id else None,
                        chat_id_int,
                        fn,
                        (self,) + args,
                        blocking_kwargs,
                        cleanup_files=cleanup_files,
                    )

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
        def skip_on_rate_limit(cls, fn: Callable):
            """Skip execution silently if messages are queued or pool is in
            high-volume mode for the target chat.
            TN-0005 keeps nonessential chat actions outside message quotas and
            suppresses them while they could compete with durable work."""
            AUX_USE_RECENCY = 5.0  # seconds

            @wraps(fn)
            def skip_wrapper(self: 'TelegramBotManager', *args, **kwargs):
                if OutboundTask.select().where(OutboundTask.state.in_(TaskState.ACTIVE)).exists():
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
        self._stopping = threading.Event()

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

        # Each bot owns independent in-memory global and bot-chat limits.
        from .rate_limiter import SlidingWindowRateLimiter
        self.GLOBAL_LIMIT = 28    # acquisitions per second
        self.GLOBAL_WINDOW = 1.0
        self.CHAT_LIMIT = 18      # acquisitions per minute per chat
        self.CHAT_WINDOW = 60.0
        self._rate_limiter = SlidingWindowRateLimiter()

        self._cleanup_tls = threading.local()  # Thread-local for pending cleanup files
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

        # The queue is initialized before accepting executor work.  A failed
        # SQLite setup leaves the file available for inspection and starts no worker.
        from concurrent.futures import ThreadPoolExecutor

        self._send_worker_stop = threading.Event()
        self._bot_chat_disabled_until: dict[BotChatKey, float] = {}
        self._bot_chat_retry_failures: dict[BotChatKey, int] = {}
        self._last_metrics_snapshot = 0.0
        from .etm_metrics import Metrics, start_metrics_server
        metrics_top_n, metrics_endpoint = self._parse_metrics_config(config.get('metrics'), self.logger)
        self._metrics = Metrics(namespace="etm", top_n=metrics_top_n)
        self._metrics.register_manager_state(self)
        self._metrics_httpd = None
        if metrics_endpoint is not None:
            metrics_host, metrics_port = metrics_endpoint
            self._metrics_httpd = start_metrics_server(
                metrics_host,
                metrics_port,
                registry=self._metrics.registry,
            )

        self._send_worker_count = self.DEFAULT_SEND_WORKER_COUNT
        self._outbound_queue = OutboundQueue(channel.db._base_path)
        self._send_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=self._send_worker_count, thread_name_prefix="ETM-send",
        )
        self._outbound_scheduler = OutboundQueueScheduler(
            self._outbound_queue,
            self,
            executor=self._send_executor,
            worker_count=self._send_worker_count,
        )

        self._send_worker_thread = threading.Thread(
            target=self._queued_send_worker,
            name="ETM queued send worker",
            daemon=True
        )
        self._send_worker_thread.start()
        self.logger.debug("Durable outbound system initialized...")

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
        durable_db_logged: bool = False,
    ) -> SendReceipt:
        return SendReceipt(
            message=message,
            sender_bot_id=sender_bot_id,
            queued=queued,
            task_id=task_id,
            durable_db_logged=durable_db_logged,
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
        db_log_context = kwargs.pop('_queued_db_log_context', None)

        task_id = self._enqueue_send_task(
            target=(slave_id, int(chat_id)),
            function=function,
            args=args,
            kwargs=kwargs,
            cleanup_files=cleanup_files,
            db_log_context=db_log_context,
        )
        placeholder = self._create_queued_message_placeholder(chat_id, task_id)
        return self._make_send_receipt(placeholder, queued=True, task_id=task_id)

    def _enqueue_blocking_send_and_wait(
        self,
        slave_id: Optional[str],
        chat_id: int,
        function: Callable,
        args: tuple,
        kwargs: dict,
        *,
        cleanup_files: Optional[list] = None,
    ) -> SendReceipt:
        kwargs = dict(kwargs)
        db_log_context = kwargs.pop('_queued_db_log_context', None)

        waiter: Future = Future()
        # Blocking operations without slave affinity share a per-chat control
        # source so their local submissions retain a deterministic order.
        target = (slave_id or self.BLOCKING_SEND_TARGET_SLAVE_ID, int(chat_id))
        task_id = self._enqueue_send_task(
            target=target,
            function=function,
            args=args,
            kwargs=kwargs,
            cleanup_files=cleanup_files,
            db_log_context=db_log_context,
            priority=True,
            waiter=waiter,
        )
        try:
            return waiter.result(timeout=self.BLOCKING_SEND_TIMEOUT)
        except FutureTimeoutError as exc:
            if waiter.done():
                return waiter.result(timeout=0)
            error = RuntimeError(
                f"Blocking send to chat {chat_id} timed out after {self.BLOCKING_SEND_TIMEOUT:g}s"
            )
            task_number = int(task_id)
            with self._outbound_registry_lock:
                workflow_id = self._outbound_workflow_by_task.get(task_number)
                if workflow_id is not None and self._outbound_waiters.get(workflow_id) is waiter:
                    self._outbound_waiters.pop(workflow_id, None)
                    self._outbound_waiter_receipts.pop(workflow_id, None)
            metrics = getattr(self, '_metrics', None)
            if metrics:
                metrics.waiter_timed_out(function.__name__)
            raise error from exc

    def select_sender(
        self,
        task: OutboundTask,
        now: datetime,
    ) -> SenderSelectionResult:
        """Choose a sender without consuming any rate-limit capacity."""
        now_timestamp = time.monotonic()
        required_sender_bot_id = task.required_sender_bot_id
        chat_id = getattr(task, "telegram_chat_id", None)
        if chat_id is None:
            chat_id = task.target_chat_id

        if required_sender_bot_id == "__main__":
            cooldown = self._bot_chat_disabled_until.get((None, chat_id), 0.0)
            delay = max(self._rate_limiter.peek_delay(chat_id), cooldown - now_timestamp)
            if delay > 0:
                return SenderSelectionResult(
                    retry_at=now + timedelta(seconds=delay),
                    reason="required_main_unavailable",
                )
            return SenderSelectionResult(selection=SenderSelection(
                sender=self._bot,
                sender_bot_id=None,
                reservation=None,
            ))

        if required_sender_bot_id is not None:
            aux_bot = self.bot_pool.get_bot_by_id(required_sender_bot_id) if self.bot_pool else None
            if aux_bot is None or aux_bot.disabled:
                return SenderSelectionResult(
                    reason="required_sender_unavailable",
                    terminal_error_class="required_sender_unavailable",
                )
            membership = aux_bot.check_membership_tri(chat_id)
            if membership is None:
                return SenderSelectionResult(
                    retry_at=now + timedelta(seconds=self.MEMBERSHIP_RECHECK_SECONDS),
                    reason="required_sender_membership_pending",
                )
            if membership is not True:
                return SenderSelectionResult(
                    reason="required_sender_unavailable",
                    terminal_error_class="required_sender_unavailable",
                )
            cooldown = self._bot_chat_disabled_until.get(
                (str(required_sender_bot_id), chat_id), 0.0,
            )
            delay = max(aux_bot.peek_delay(chat_id), cooldown - now_timestamp)
            if delay > 0:
                return SenderSelectionResult(
                    retry_at=now + timedelta(seconds=delay),
                    reason="required_sender_rate_limited",
                )
            return SenderSelectionResult(selection=SenderSelection(
                sender=aux_bot.bot,
                sender_bot_id=str(aux_bot.bot_id),
                reservation=None,
            ))

        main_cooldown = self._bot_chat_disabled_until.get((None, chat_id), 0.0)
        main_delay = max(
            self._rate_limiter.peek_delay(chat_id),
            main_cooldown - now_timestamp,
        )
        candidates: list[tuple[object, Optional[str], float]] = [(self._bot, None, main_delay)]
        unknown_membership = False
        if self.bot_pool:
            for aux_bot, membership in self.bot_pool.candidate_bots(chat_id):
                if membership is None:
                    unknown_membership = True
                    continue
                if membership is not True:
                    continue
                cooldown = self._bot_chat_disabled_until.get((str(aux_bot.bot_id), chat_id), 0.0)
                candidates.append((
                    aux_bot.bot,
                    str(aux_bot.bot_id),
                    max(aux_bot.peek_delay(chat_id), cooldown - now_timestamp),
                ))

        if unknown_membership:
            return SenderSelectionResult(
                retry_at=now + timedelta(seconds=self.MEMBERSHIP_RECHECK_SECONDS),
                reason="auxiliary_membership_pending",
            )

        earliest_delay = min(delay for _sender, _bot_id, delay in candidates)
        if earliest_delay > 0:
            return SenderSelectionResult(
                retry_at=now + timedelta(seconds=earliest_delay),
                reason="all_senders_rate_limited",
            )

        selectable = [candidate for candidate in candidates if candidate[2] == 0]
        preferred_bot = self.bot_pool.preferred_sender(task.slave_id) if self.bot_pool else None
        if preferred_bot is not None:
            preferred_id = str(preferred_bot.bot_id)
            for sender, sender_bot_id, _delay in selectable:
                if sender_bot_id == preferred_id:
                    return SenderSelectionResult(selection=SenderSelection(
                        sender=sender,
                        sender_bot_id=sender_bot_id,
                        reservation=None,
                    ))

        for sender, sender_bot_id, _delay in selectable:
            if sender_bot_id is None:
                return SenderSelectionResult(selection=SenderSelection(
                    sender=sender,
                    sender_bot_id=None,
                    reservation=None,
                ))

        sender, sender_bot_id, _delay = min(
            selectable,
            key=lambda candidate: str(candidate[1]),
        )
        return SenderSelectionResult(selection=SenderSelection(
            sender=sender,
            sender_bot_id=sender_bot_id,
            reservation=None,
        ))

    def acquire_sender_limits(self, selection: SenderSelection, telegram_chat_id: int) -> bool:
        """Consume the chosen sender's global limit before its bot-chat limit."""
        if selection.sender_bot_id is None:
            return self._rate_limiter.try_acquire(telegram_chat_id)
        aux_bot = self.bot_pool.get_bot_by_id(selection.sender_bot_id) if self.bot_pool else None
        return aux_bot is not None and aux_bot.try_acquire_limits(telegram_chat_id)

    def next_sender_deadline(self, selection: SenderSelection, telegram_chat_id: int) -> float:
        """Return the chosen sender's monotonic limiter delay after a failed acquisition."""
        if selection.sender_bot_id is None:
            return self._rate_limiter.peek_delay(telegram_chat_id)
        aux_bot = self.bot_pool.get_bot_by_id(selection.sender_bot_id) if self.bot_pool else None
        return float('inf') if aux_bot is None else aux_bot.peek_delay(telegram_chat_id)

    def record_successful_sender(self, task: OutboundTask, selection: SenderSelection) -> None:
        """Persist no state; update optional in-memory affinity after an auxiliary success."""
        if selection.sender_bot_id is not None and self.bot_pool:
            self.bot_pool.record_successful_auxiliary_send(task.slave_id, selection.sender_bot_id)

    def remove_confirmed_non_member_affinity(self, task: OutboundTask, sender_bot_id: Optional[str]) -> None:
        """Apply membership-probe failure only to the triggering affinity entry."""
        if sender_bot_id is not None and self.bot_pool:
            self.bot_pool.remove_failed_membership_affinity(task.slave_id, sender_bot_id)

    def execute_task(self, task: OutboundTask, selection: SenderSelection) -> object:
        operation, args, kwargs = self._outbound_codec.decode_command(task.payload)
        if task.depends_on_task_id is not None:
            predecessor = OutboundTask.get_by_id(task.depends_on_task_id)
            if not predecessor.result_payload:
                raise ValueError(f"Outbound predecessor {predecessor.id} has no result payload.")
            predecessor_payload = json.loads(predecessor.result_payload)
            args = cast(tuple, self._resolve_outbound_result_refs(args, predecessor_payload))
            kwargs = cast(dict, self._resolve_outbound_result_refs(kwargs, predecessor_payload))
        if not operation.startswith('api_'):
            raise RuntimeError(f"Unsupported durable outbound operation: {operation}")
        api_method_name = operation[len('api_'):]
        method = getattr(selection.sender, api_method_name, None)
        if not callable(method):
            raise RuntimeError(f"Selected bot does not support outbound operation: {api_method_name}")
        started = time.monotonic()
        result = method(*args, **kwargs)
        metrics = getattr(self, '_metrics', None)
        if metrics:
            metrics.observe_send_latency(
                self._metrics_sender(selection.sender_bot_id),
                time.monotonic() - started,
            )
        return result

    def task_dispatched(
        self,
        task: OutboundTask,
        selection: SenderSelection,
        now: datetime,
    ) -> None:
        metrics = getattr(self, '_metrics', None)
        if not metrics:
            return
        metrics.task_dispatched(self._metrics_sender(selection.sender_bot_id))
        if task.accepted_at is not None:
            metrics.observe_queue_wait(max(0.0, (now - task.accepted_at).total_seconds()))

    def serialize_result(
        self,
        task: OutboundTask,
        result: object,
        selection: SenderSelection,
    ) -> Mapping[str, object]:
        self._outbound_live_results[task.id] = (result, selection.sender_bot_id)
        self._bot_chat_retry_failures.pop(
            (selection.sender_bot_id, task.target_chat_id),
            None,
        )
        if selection.sender_bot_id is not None:
            self._record_aux_use(task.target_chat_id)
            self.record_successful_sender(task, selection)
        metrics = getattr(self, '_metrics', None)
        if metrics:
            total_seconds = None
            if task.accepted_at is not None:
                total_seconds = max(0.0, (utc_now() - task.accepted_at).total_seconds())
            metrics.send_completed(self._metrics_sender(selection.sender_bot_id), "ok", total_seconds)
        chat = getattr(result, 'chat', None)
        chat_id = getattr(result, 'chat_id', None) or getattr(chat, 'id', None) or task.target_chat_id
        message_id = getattr(result, 'message_id', None)
        if message_id is None:
            _operation, _args, kwargs = self._outbound_codec.decode_command(task.payload)
            message_id = kwargs.get('message_id')
        payload: dict[str, object] = {
            "ok": True,
            "chat_id": int(chat_id),
            "sender_bot_id": selection.sender_bot_id,
        }
        if message_id is not None:
            payload["message_id"] = int(message_id)
        try:
            payload["media_type"] = get_msg_type(cast(TelegramMessage, result)).value
        except Exception:
            pass

        attachment = None
        for attribute in ('animation', 'document', 'video', 'voice', 'audio', 'sticker', 'video_note'):
            attachment = getattr(result, attribute, None)
            if attachment is not None:
                break
        if attachment is None:
            photos = getattr(result, 'photo', None)
            if photos:
                attachment = photos[-1]
        if attachment is not None:
            file_id = getattr(attachment, 'file_id', None)
            file_unique_id = getattr(attachment, 'file_unique_id', None)
            mime = getattr(attachment, 'mime_type', None)
            if file_id is not None:
                payload["file_id"] = file_id
            if file_unique_id is not None:
                payload["file_unique_id"] = file_unique_id
            if mime is not None:
                payload["mime"] = mime
        return payload

    def classify_error(
        self,
        task: OutboundTask,
        error: Exception,
        selection: SenderSelection,
        now: datetime,
    ) -> FailureDisposition:
        if isinstance(error, telegram.error.ChatMigrated):
            new_chat_id = int(error.new_chat_id)
            with OutboundTask._meta.database.atomic():
                self.channel.chat_binding.chat_migration_by_id(task.target_chat_id, new_chat_id)
                self._outbound_repository.migrate_chat_target(task.target_chat_id, new_chat_id)
            return FailureDisposition(FailureDisposition.RETRY, "chat_migrated", retry_at=now)

        retry_after = self._rate_limit_retry_after_seconds(error)
        if retry_after is not None:
            bot_chat_key = (selection.sender_bot_id, task.target_chat_id)
            failures = self._bot_chat_retry_failures.get(bot_chat_key, 0) + 1
            self._bot_chat_retry_failures[bot_chat_key] = failures
            delay = self._telegram_retry_delay_seconds(retry_after, failures)
            self._bot_chat_disabled_until[bot_chat_key] = now.timestamp() + delay
            if self.bot_pool and task.slave_id:
                self.bot_pool.forget_affinity(task.slave_id)
            metrics = getattr(self, '_metrics', None)
            if metrics:
                metrics.rate_limited(self._metrics_sender(selection.sender_bot_id))
                metrics.task_requeued("rate_limit")
            return FailureDisposition(FailureDisposition.RETRY, "retry_after", retry_at=None)

        if isinstance(error, (telegram.error.TimedOut, telegram.error.NetworkError)):
            if task.attempt_count < 3:
                return FailureDisposition(FailureDisposition.RETRY, "network", retry_at=now)
            self._outbound_live_errors[task.id] = error
            return FailureDisposition(FailureDisposition.DEAD, "network_attempts_exhausted")

        if isinstance(error, telegram.error.Forbidden) and selection.sender_bot_id and self.bot_pool:
            aux_bot = self.bot_pool.get_bot_by_id(selection.sender_bot_id)
            if aux_bot is not None:
                aux_bot.update_membership(task.target_chat_id, False)
                return FailureDisposition(FailureDisposition.RETRY, "forbidden_aux", retry_at=now)

        error_class = "telegram_error"
        if isinstance(error, telegram.error.BadRequest):
            message = (getattr(error, 'message', None) or str(error)).lower()
            if "can't parse entities" in message:
                error_class = "parse_entities"
            elif "can't be edited" in message:
                error_class = "edit_not_allowed"
            elif "message to edit not found" in message:
                error_class = "edit_not_found"
            elif task.operation in {'api_send_audio', 'api_send_voice', 'api_send_video', 'api_send_photo'}:
                error_class = "media_bad_request"
            else:
                error_class = "bad_request"
            if self._outbound_repository.has_error_handler(task.id, error_class):
                return FailureDisposition(FailureDisposition.EXPECTED, error_class)
        elif isinstance(error, telegram.error.Forbidden):
            error_class = "forbidden"
        self._outbound_live_errors[task.id] = error
        return FailureDisposition(FailureDisposition.DEAD, error_class)

    def reconcile_sent_task(
        self,
        task: OutboundTask,
        result_payload: Mapping[str, object],
        selection: SenderSelection,
    ) -> None:
        if not task.log_payload:
            return
        if "message_id" not in result_payload:
            raise ValueError(f"Outbound task {task.id} produced no Telegram message ID for logging.")
        self.channel.db.reconcile_outbound_message_log(
            task.id,
            json.loads(task.log_payload),
            result_payload,
            sender_bot_id=selection.sender_bot_id,
        )

    def workflow_finished(self, workflow_outcome: WorkflowOutcome) -> None:
        metrics = getattr(self, '_metrics', None)
        if metrics:
            metrics.workflow_terminal(workflow_outcome.state)
        with self._outbound_registry_lock:
            waiter = self._outbound_waiters.pop(workflow_outcome.workflow_id, None)
            return_receipt = self._outbound_waiter_receipts.pop(workflow_outcome.workflow_id, False)
            callback = self._outbound_db_callbacks.pop(workflow_outcome.workflow_id, None)
            task_ids = [
                task_id for task_id, workflow_id in self._outbound_workflow_by_task.items()
                if workflow_id == workflow_outcome.workflow_id
            ]
            for task_id in task_ids:
                self._outbound_workflow_by_task.pop(task_id, None)

        if not task_ids:
            try:
                task_ids = [
                    task.id
                    for task in OutboundTask.select(OutboundTask.id).where(
                        OutboundTask.workflow_id == workflow_outcome.workflow_id
                    )
                ]
            except Exception:
                self.logger.exception(
                    "Failed to load durable task IDs for completed workflow %s.",
                    workflow_outcome.workflow_id,
                )

        result_entry = None
        if workflow_outcome.result_task_id is not None:
            result_entry = self._outbound_live_results.get(workflow_outcome.result_task_id)
        live_error = next(
            (self._outbound_live_errors[task_id] for task_id in task_ids if task_id in self._outbound_live_errors),
            None,
        )
        for task_id in task_ids:
            if task_id != workflow_outcome.result_task_id:
                self._outbound_live_results.pop(task_id, None)

        try:
            if waiter is not None and not waiter.done():
                if workflow_outcome.state == "completed" and result_entry is not None:
                    result, sender_bot_id = result_entry
                    if return_receipt:
                        waiter.set_result(self._make_send_receipt(
                            result,
                            sender_bot_id=sender_bot_id,
                            task_id=str(workflow_outcome.result_task_id),
                            durable_db_logged=True,
                        ))
                    else:
                        waiter.set_result(result)
                elif live_error is not None:
                    waiter.set_exception(live_error)
                else:
                    waiter.set_exception(RuntimeError(
                        f"Outbound workflow {workflow_outcome.workflow_id} ended as "
                        f"{workflow_outcome.state}: {workflow_outcome.error_class or 'no result'}"
                    ))
        finally:
            if workflow_outcome.result_task_id is not None:
                self._outbound_live_results.pop(workflow_outcome.result_task_id, None)
            for task_id in task_ids:
                self._outbound_live_errors.pop(task_id, None)
            self._run_database_update_callback(callback)
            chat_binding = getattr(self.channel, 'chat_binding', None)
            if chat_binding is not None:
                chat_binding.resume_pending_history_migrations()

    def _reconcile_sent_pending_tasks(self, task_ids: Optional[Collection[int]] = None) -> None:
        for task in self._outbound_repository.sent_pending_tasks(
            tuple(task_ids) if task_ids is not None else None
        ):
            if not task.result_payload:
                continue
            result_payload = json.loads(task.result_payload)
            try:
                if task.log_payload:
                    self.channel.db.reconcile_outbound_message_log(
                        task.id,
                        json.loads(task.log_payload),
                        result_payload,
                        sender_bot_id=cast(Optional[str], result_payload.get("sender_bot_id")),
                    )
                outcome = self._outbound_repository.complete_success(task.id, utc_now())
            except Exception:
                self.logger.exception("Failed to reconcile sent outbound task %s", task.id)
                continue
            if outcome is not None:
                self.workflow_finished(outcome)

    @staticmethod
    def _metrics_sender(sender_bot_id: Optional[str]) -> str:
        return "aux" if sender_bot_id else "main"

    @staticmethod
    def _parse_metrics_config(metrics_cfg: object, logger) -> tuple[int, Optional[tuple[str, int]]]:
        top_n = 20
        if metrics_cfg is None:
            return top_n, None
        if not isinstance(metrics_cfg, collections.abc.Mapping):
            logger.warning("Invalid metrics config, Prometheus endpoint disabled: %s", metrics_cfg)
            return top_n, None

        try:
            parsed_top_n = int(metrics_cfg.get('top_n', top_n))
            if parsed_top_n < 0:
                raise ValueError
            top_n = parsed_top_n
        except (TypeError, ValueError):
            logger.warning("Invalid metrics top_n, using default %d: %s", top_n, metrics_cfg.get('top_n'))

        host = metrics_cfg.get('host', '127.0.0.1')
        if not isinstance(host, str) or not host:
            logger.warning("Invalid metrics host, Prometheus endpoint disabled: %s", host)
            return top_n, None

        try:
            port = int(metrics_cfg.get('port', 9101))
            if not 0 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            logger.warning("Invalid metrics port, Prometheus endpoint disabled: %s", metrics_cfg.get('port'))
            return top_n, None

        return top_n, (host, port)

    def _snapshot_send_metrics(self, *, worker_alive: bool):
        metrics = getattr(self, '_metrics', None)
        if metrics:
            metrics.snapshot_manager_state(self, worker_alive=worker_alive)

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
            )
            aux_bot.bind_runtime(self._runtime)
            if aux_bot.initialize():
                aux_bots.append(aux_bot)
            else:
                self.logger.error("Skipping auxiliary bot with invalid token")

        if aux_bots:
            self.bot_pool = BotPool(aux_bots, self)
            self.logger.info("Initialized bot pool with %d auxiliary bot(s)", len(aux_bots))

    def _record_aux_use(self, chat_id: int):
        """Record that an aux bot was used for a chat (for typing suppression)."""
        self._aux_recent_use[chat_id] = time.time()

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

    @staticmethod
    def _call_argument(args: tuple, kwargs: Mapping[str, object], name: str, index: int, default=None):
        if len(args) > index:
            return args[index]
        return kwargs.get(name, default)

    @staticmethod
    def _replace_call_argument(
        args: tuple,
        kwargs: Mapping[str, object],
        name: str,
        index: int,
        value: object,
    ) -> tuple[tuple, dict]:
        mutable_args = list(args)
        mutable_kwargs = dict(kwargs)
        if len(mutable_args) > index:
            mutable_args[index] = value
            mutable_kwargs.pop(name, None)
        else:
            mutable_kwargs[name] = value
        return tuple(mutable_args), mutable_kwargs

    @staticmethod
    def _rename_call_argument(
        args: tuple,
        kwargs: Mapping[str, object],
        old_name: str,
        new_name: str,
        index: int,
    ) -> tuple[tuple, dict]:
        mutable_kwargs = dict(kwargs)
        if len(args) <= index and old_name in mutable_kwargs:
            mutable_kwargs[new_name] = mutable_kwargs.pop(old_name)
        return args, mutable_kwargs

    @staticmethod
    def _outbound_file_is_empty(value: object) -> bool:
        if isinstance(value, str):
            parsed = urlparse(value)
            if parsed.scheme in {'http', 'https'}:
                return False
            path = url2pathname(parsed.path) if parsed.scheme == 'file' else value
            return os.stat(path).st_size == 0
        if isinstance(value, InputFile):
            content = value.input_file_content
            if isinstance(content, bytes):
                return not content
            value = content
        if callable(getattr(value, 'seek', None)) and callable(getattr(value, 'tell', None)):
            stream = cast(BinaryIO, value)
            current = stream.tell()
            try:
                stream.seek(0, 2)
                return stream.tell() == 0
            finally:
                stream.seek(current)
        return False

    @staticmethod
    def _resolve_outbound_result_refs(value: object, predecessor_payload: Mapping[str, object]) -> object:
        if isinstance(value, dict):
            result_key = value.get("__etm_outbound_result__")
            if isinstance(result_key, str):
                if result_key not in predecessor_payload:
                    raise ValueError(f"Predecessor result has no {result_key!r} field.")
                return predecessor_payload[result_key]
            return {
                key: TelegramBotManager._resolve_outbound_result_refs(item, predecessor_payload)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [TelegramBotManager._resolve_outbound_result_refs(item, predecessor_payload) for item in value]
        if isinstance(value, tuple):
            return tuple(TelegramBotManager._resolve_outbound_result_refs(item, predecessor_payload) for item in value)
        return value

    def _build_outbound_workflow_specs(
        self,
        *,
        source_key: str,
        slave_id: Optional[str],
        priority: bool,
        target_chat_id: int,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
        required_sender_bot_id: Optional[str],
        log_payload: Optional[Mapping[str, object]],
        owned_local_paths: tuple[str, ...] = (),
    ) -> tuple[list[OutboundTaskSpec], int]:
        specs: list[OutboundTaskSpec] = []
        message_thread_id = cast(Optional[int], kwargs.get('message_thread_id'))

        def add(
            api_operation: str,
            step_args: tuple,
            step_kwargs: Mapping[str, object],
            *,
            dependency: Optional[int] = None,
            run_condition: str = RunCondition.ALWAYS,
            step_log_payload: Optional[Mapping[str, object]] = None,
            required_sender: Optional[str] = required_sender_bot_id,
        ) -> int:
            step_index = len(specs)
            specs.append(OutboundTaskSpec(
                source_key=source_key,
                slave_id=slave_id,
                priority=priority,
                target_chat_id=target_chat_id,
                message_thread_id=message_thread_id,
                operation=f"api_{api_operation}",
                args=step_args,
                kwargs=step_kwargs,
                depends_on_step_index=dependency,
                run_condition=run_condition,
                required_sender_bot_id=required_sender,
                log_payload=step_log_payload,
                owned_local_paths=owned_local_paths,
            ))
            return step_index

        def add_parse_variant(
            api_operation: str,
            primary_index: int,
            step_args: tuple,
            step_kwargs: Mapping[str, object],
            *,
            step_log_payload: Optional[Mapping[str, object]],
            required_sender: Optional[str] = required_sender_bot_id,
        ) -> Optional[int]:
            if 'parse_mode' not in step_kwargs or step_kwargs.get('parse_mode') is None:
                return None
            fallback_kwargs = dict(step_kwargs)
            fallback_kwargs.pop('parse_mode', None)
            return add(
                api_operation,
                step_args,
                fallback_kwargs,
                dependency=primary_index,
                run_condition=f"{RunCondition.PREDECESSOR_ERROR_PREFIX}parse_entities",
                step_log_payload=step_log_payload,
                required_sender=required_sender,
            )

        def add_full_text_attachment(predecessor_index: int, content: str, filename: str) -> None:
            attachment_kwargs: dict[str, object] = {
                "chat_id": target_chat_id,
                "document": io.BytesIO(content.encode("utf-8")),
                "filename": filename,
                "reply_to_message_id": {"__etm_outbound_result__": "message_id"},
                "caption": self._("Message is truncated due to its length. Full message is sent as attachment."),
            }
            if message_thread_id is not None:
                attachment_kwargs["message_thread_id"] = message_thread_id
            add(
                "send_document",
                (),
                attachment_kwargs,
                dependency=predecessor_index,
                run_condition=RunCondition.PREDECESSOR_SUCCESS,
                required_sender=None,
            )

        if operation == 'send_message':
            step_kwargs = dict(kwargs)
            prefix = str(step_kwargs.pop('prefix', '') or '')
            suffix = str(step_kwargs.pop('suffix', '') or '')
            text = str(self._call_argument(args, step_kwargs, 'text', 1, '') or '')
            prefix = f"{prefix}\n" if prefix else ''
            suffix = f"\n{suffix}" if suffix else ''
            if str(step_kwargs.get('parse_mode', '')).lower() == 'html':
                prefix = html.escape(prefix)
                suffix = html.escape(suffix)
            full_text = prefix + text + suffix
            send_text = full_text
            is_long = len(full_text) >= telegram.constants.MessageLimit.MAX_TEXT_LENGTH
            if is_long:
                send_text = prefix + text[:100] + "\n...\n" + text[-100:] + suffix
            step_args, step_kwargs = self._replace_call_argument(args, step_kwargs, 'text', 1, send_text)
            primary = add('send_message', step_args, step_kwargs, step_log_payload=log_payload)
            parse_fallback = add_parse_variant(
                'send_message', primary, step_args, step_kwargs, step_log_payload=log_payload,
            )
            if is_long:
                extension = '.html' if str(step_kwargs.get('parse_mode', '')).lower() == 'html' else '.txt'
                add_full_text_attachment(primary, full_text, f"{target_chat_id}_message{extension}")
                if parse_fallback is not None:
                    add_full_text_attachment(parse_fallback, full_text, f"{target_chat_id}_message.txt")
            return specs, primary

        if operation == 'edit_message_text':
            step_kwargs = dict(kwargs)
            prefix = str(step_kwargs.pop('prefix', '') or '')
            suffix = str(step_kwargs.pop('suffix', '') or '')
            text = str(step_kwargs.get('text', '') or '')
            prefix = f"{prefix}\n" if prefix else ''
            suffix = f"\n{suffix}" if suffix else ''
            if str(step_kwargs.get('parse_mode', '')).lower() == 'html':
                prefix = html.escape(prefix)
                suffix = html.escape(suffix)
            full_text = prefix + text + suffix
            is_long = len(full_text) >= telegram.constants.MessageLimit.MAX_TEXT_LENGTH
            step_kwargs['text'] = (
                prefix + text[:100] + "\n...\n" + text[-100:] + suffix
                if is_long else full_text
            )
            primary = add('edit_message_text', args, step_kwargs, step_log_payload=log_payload)
            variants = [primary]
            parse_fallback = add_parse_variant(
                'edit_message_text', primary, args, step_kwargs,
                step_log_payload=log_payload,
            )
            if parse_fallback is not None:
                variants.append(parse_fallback)

            for edit_index, edit_kwargs in [(primary, step_kwargs)] + (
                [(parse_fallback, {key: value for key, value in step_kwargs.items() if key != 'parse_mode'})]
                if parse_fallback is not None else []
            ):
                for error_class, reply_to_original in (
                    ('edit_not_allowed', True),
                    ('edit_not_found', False),
                ):
                    fallback_kwargs = dict(edit_kwargs)
                    original_message_id = fallback_kwargs.pop('message_id', None)
                    if reply_to_original and original_message_id is not None:
                        fallback_kwargs['reply_to_message_id'] = original_message_id
                    fallback = add(
                        'send_message',
                        (),
                        fallback_kwargs,
                        dependency=cast(int, edit_index),
                        run_condition=f"{RunCondition.PREDECESSOR_ERROR_PREFIX}{error_class}",
                        step_log_payload=log_payload,
                        required_sender=None,
                    )
                    variants.append(fallback)
                    parse_send_fallback = add_parse_variant(
                        'send_message', fallback, (), fallback_kwargs,
                        step_log_payload=log_payload, required_sender=None,
                    )
                    if parse_send_fallback is not None:
                        variants.append(parse_send_fallback)
            if is_long:
                for variant in variants:
                    add_full_text_attachment(variant, full_text, f"{target_chat_id}_message.txt")
            return specs, primary

        caption_operations = {
            'send_audio': 'audio',
            'send_voice': 'voice',
            'send_video': 'video',
            'send_document': 'document',
            'send_animation': 'animation',
            'send_photo': 'photo',
        }
        if operation in caption_operations or operation == 'edit_message_caption':
            step_kwargs = dict(kwargs)
            fallback_to_document = bool(step_kwargs.pop('_fallback_to_document', True))
            prefix = str(step_kwargs.pop('prefix', '') or '')
            suffix = str(step_kwargs.pop('suffix', '') or '')
            caption = str(step_kwargs.pop('caption', '') or '')
            if operation in caption_operations:
                file_value = self._call_argument(
                    args,
                    step_kwargs,
                    caption_operations[operation],
                    1,
                )
                if file_value is not None and self._outbound_file_is_empty(file_value):
                    return self._build_outbound_workflow_specs(
                        source_key=source_key,
                        slave_id=slave_id,
                        priority=priority,
                        target_chat_id=target_chat_id,
                        operation='send_message',
                        args=(target_chat_id,),
                        kwargs={
                            "prefix": self._("Empty attachment detected.") + prefix,
                            "text": caption,
                            "suffix": suffix,
                            **(
                                {"message_thread_id": message_thread_id}
                                if message_thread_id is not None else {}
                            ),
                        },
                        required_sender_bot_id=None,
                        log_payload=log_payload,
                        owned_local_paths=owned_local_paths,
                    )
            prefix = f"{prefix}\n" if prefix else ''
            suffix = f"\n{suffix}" if suffix else ''
            if str(step_kwargs.get('parse_mode', '')).lower() == 'html':
                prefix = html.escape(prefix)
                suffix = html.escape(suffix)
            full_caption = prefix + caption + suffix
            is_long = len(full_caption) >= telegram.constants.MessageLimit.CAPTION_LENGTH
            step_kwargs['caption'] = (
                prefix + caption[:100] + "\n...\n" + caption[-100:] + suffix
                if is_long else full_caption
            )
            primary = add(operation, args, step_kwargs, step_log_payload=log_payload)
            variants = [primary]
            parse_fallback = add_parse_variant(
                operation, primary, args, step_kwargs, step_log_payload=log_payload,
            )
            if parse_fallback is not None:
                variants.append(parse_fallback)

            document_fallback_operations = {'send_audio', 'send_voice', 'send_video'}
            if operation in document_fallback_operations or (
                operation == 'send_photo' and fallback_to_document
            ):
                for media_index, media_kwargs in [(primary, step_kwargs)] + (
                    [(parse_fallback, {key: value for key, value in step_kwargs.items() if key != 'parse_mode'})]
                    if parse_fallback is not None else []
                ):
                    document_args, document_kwargs = self._rename_call_argument(
                        args,
                        media_kwargs,
                        caption_operations[operation],
                        'document',
                        1,
                    )
                    fallback = add(
                        'send_document',
                        document_args,
                        document_kwargs,
                        dependency=cast(int, media_index),
                        run_condition=f"{RunCondition.PREDECESSOR_ERROR_PREFIX}media_bad_request",
                        step_log_payload=log_payload,
                        required_sender=None,
                    )
                    variants.append(fallback)
                    parse_document = add_parse_variant(
                        'send_document', fallback, document_args, document_kwargs,
                        step_log_payload=log_payload, required_sender=None,
                    )
                    if parse_document is not None:
                        variants.append(parse_document)
            if is_long:
                for variant in variants:
                    add_full_text_attachment(variant, full_caption, f"{target_chat_id}_caption.txt")
            return specs, primary

        primary = add(operation, args, kwargs, step_log_payload=log_payload)
        return specs, primary

    def _enqueue_send_task(self, target: SendTarget, function: Callable,
                           args: tuple, kwargs: dict,
                           cleanup_files: Optional[list] = None,
                           db_log_context: Optional[QueuedDbLogContext] = None,
                           priority: bool = False,
                           waiter: Optional[Future] = None) -> str:
        """Durably accept one bounded Telegram operation into a source lane."""
        slave_id, chat_id = target
        durable_kwargs = dict(kwargs)
        required_sender_bot_id = durable_kwargs.pop('_required_sender_bot_id', None)
        for key in _INTERNAL_KWARGS:
            durable_kwargs.pop(key, None)
        durable_args = args[1:] if args and args[0] is self else args
        log_payload = None
        if db_log_context is not None:
            log_payload = self.channel.db.build_outbound_log_payload(
                db_log_context.etm_msg,
                db_log_context.old_msg_id,
            )
        specs, result_task_index = self._build_outbound_workflow_specs(
            source_key=slave_id,
            slave_id=None if slave_id == self.BLOCKING_SEND_TARGET_SLAVE_ID else slave_id,
            priority=priority,
            target_chat_id=int(chat_id),
            operation=function.__name__,
            args=durable_args,
            kwargs=durable_kwargs,
            required_sender_bot_id=required_sender_bot_id,
            log_payload=log_payload,
            owned_local_paths=tuple(str(path) for path in cleanup_files or ()),
        )
        created = self._outbound_repository.create_workflow(
            specs,
            result_task_index=result_task_index,
        )
        task = created.tasks[0]
        with self._outbound_registry_lock:
            for workflow_task in created.tasks:
                self._outbound_workflow_by_task[workflow_task.id] = created.workflow.id
            if waiter is not None:
                self._outbound_waiters[created.workflow.id] = waiter
                self._outbound_waiter_receipts[created.workflow.id] = True
            if db_log_context is not None:
                self._outbound_db_callbacks[created.workflow.id] = db_log_context.on_complete

        for path in cleanup_files or ():
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as error:
                self.logger.warning("Failed to remove source temp file %s after durable spooling: %s", path, error)

        metrics = getattr(self, '_metrics', None)
        if metrics:
            metrics.task_enqueued(priority=priority)

        self._outbound_scheduler.wake_event.set()
        self.logger.debug("Durably accepted outbound task %s for source %s", task.id, slave_id)
        return str(task.id)

    def enqueue_history_operation(
        self,
        *,
        source_key: str,
        target_chat_id: int,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
        history_entry_ids: Collection[int],
    ) -> int:
        specs, result_task_index = self._build_outbound_workflow_specs(
            source_key=f"history:{source_key}",
            slave_id=None,
            priority=False,
            target_chat_id=target_chat_id,
            operation=operation,
            args=args,
            kwargs=kwargs,
            required_sender_bot_id=None,
            log_payload=None,
        )
        with OutboundTask._meta.database.atomic():
            created = self._outbound_repository.create_workflow(
                specs,
                result_task_index=result_task_index,
            )
            self.channel.db.link_history_migration_entries(
                history_entry_ids,
                cast(int, created.workflow.id),
            )
        with self._outbound_registry_lock:
            for task in created.tasks:
                self._outbound_workflow_by_task[task.id] = created.workflow.id
        self._outbound_scheduler.wake_event.set()
        return cast(int, created.workflow.id)

    def _enqueue_blocking_api_operation(
        self,
        *,
        target_chat_id: int,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
        required_sender_bot_id: Optional[str],
    ) -> object:
        created = self._outbound_repository.create_workflow([
            OutboundTaskSpec(
                source_key=f"__control__:{target_chat_id}",
                slave_id=None,
                priority=True,
                target_chat_id=target_chat_id,
                message_thread_id=cast(Optional[int], kwargs.get('message_thread_id')),
                operation=f"api_{operation}",
                args=args,
                kwargs=kwargs,
                required_sender_bot_id=required_sender_bot_id,
            )
        ])
        waiter: Future = Future()
        with self._outbound_registry_lock:
            for task in created.tasks:
                self._outbound_workflow_by_task[task.id] = created.workflow.id
            self._outbound_waiters[created.workflow.id] = waiter
            self._outbound_waiter_receipts[created.workflow.id] = False
        self._outbound_scheduler.wake_event.set()
        try:
            return waiter.result(timeout=self.BLOCKING_SEND_TIMEOUT)
        except FutureTimeoutError as error:
            with self._outbound_registry_lock:
                if self._outbound_waiters.get(created.workflow.id) is waiter:
                    self._outbound_waiters.pop(created.workflow.id, None)
                    self._outbound_waiter_receipts.pop(created.workflow.id, None)
            metrics = getattr(self, '_metrics', None)
            if metrics:
                metrics.waiter_timed_out(operation)
            raise RuntimeError(
                f"Outbound control workflow {created.workflow.id} timed out; durable work remains queued."
            ) from error

    def _enqueue_main_chat_mutation(
        self,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
    ) -> object:
        chat_id_value = args[0] if args else kwargs['chat_id']
        if not isinstance(chat_id_value, (int, str)):
            raise TypeError("Telegram chat_id must be an integer or decimal string.")
        chat_id = int(chat_id_value)
        return self._enqueue_blocking_api_operation(
            target_chat_id=chat_id,
            operation=operation,
            args=args,
            kwargs=kwargs,
            required_sender_bot_id="__main__",
        )

    # ── Async-dispatch queued send worker ──────────────────────

    def _queued_send_worker(self):
        """Drive durable lane heads without blocking on Telegram HTTP calls."""
        self.logger.debug("Durable outbound worker started")
        metrics = getattr(self, '_metrics', None)

        while not self._send_worker_stop.is_set():
            try:
                now = utc_now()
                if metrics:
                    metrics.loop_tick()
                recovery = self._outbound_repository.recover(
                    now,
                    local_in_flight_task_ids=self._outbound_scheduler.in_flight_task_ids(),
                )
                if metrics and recovery.requeued_ambiguous_ids:
                    metrics.recovered("ambiguous_requeued", len(recovery.requeued_ambiguous_ids))
                self._reconcile_sent_pending_tasks(recovery.sent_pending_log_ids)
                self._outbound_scheduler.harvest_completed(now)
                heartbeat_count = self._outbound_scheduler.heartbeat(now)
                if metrics:
                    metrics.lease_heartbeat(heartbeat_count)
                self._outbound_scheduler.dispatch_ready(now)

                now_timestamp = now.timestamp()
                if self._bot_chat_disabled_until:
                    expired = [k for k, v in self._bot_chat_disabled_until.items() if v <= now_timestamp]
                    for k in expired:
                        del self._bot_chat_disabled_until[k]
                if metrics and now_timestamp - self._last_metrics_snapshot >= 1.0:
                    self._snapshot_send_metrics(worker_alive=True)
                    self._last_metrics_snapshot = now_timestamp

                self._outbound_scheduler.wake_event.wait(timeout=0.25)
                self._outbound_scheduler.wake_event.clear()

            except Exception as e:
                if metrics:
                    metrics.loop_error()
                self.logger.exception("Error in durable outbound worker: %s", e)
                self._send_worker_stop.wait(timeout=1)

        shutdown_deadline = time.monotonic() + self.SHUTDOWN_DRAIN_TIMEOUT
        while self._outbound_scheduler.in_flight_snapshot() and time.monotonic() < shutdown_deadline:
            self._outbound_scheduler.harvest_completed(utc_now())
            self._send_worker_stop.wait(timeout=0.05)
        self._send_executor.shutdown(wait=False)
        self._snapshot_send_metrics(worker_alive=False)
        self.logger.debug("Durable outbound worker stopped")

    # Queue-runtime implementation.  These definitions intentionally follow
    # the former workflow helpers above so public wrappers retain their call
    # points while the runtime has only dequeue-or-loss semantics.
    def _queue_operation(self, operation: str) -> Callable[..., object]:
        method = getattr(self._bot, operation, None)
        if not callable(method):
            raise QueueEnqueueError(f"Telegram bot has no queued operation {operation!r}.")
        return cast(Callable[..., object], method)

    def _enqueue_requests(self, requests: list[QueueRequest]) -> tuple[str, Future]:
        with self._outbound_scheduler._lock:
            if self._outbound_scheduler.stopping:
                error = self._outbound_scheduler.failure or SchedulerStoppedError(
                    "Outbound scheduler stopped."
                )
                raise error
            row_id, waiter = self._outbound_queue.enqueue_many(requests, self._queue_operation)
            self._outbound_scheduler.wake_event.set()
            return str(row_id), waiter

    def _enqueue_send_task(
        self,
        target: SendTarget,
        function: Callable,
        args: tuple,
        kwargs: dict,
        cleanup_files: Optional[list] = None,
        db_log_context: Optional[QueuedDbLogContext] = None,
        priority: bool = False,
        waiter: Optional[Future] = None,
    ) -> str:
        del target, db_log_context
        operation = function.__name__
        telegram_args = args[1:] if args and args[0] is self else args
        queued_kwargs = dict(kwargs)
        queued_kwargs["_send_mode"] = "blocking" if priority else "eventual"
        row_id, queue_waiter = self._enqueue_requests([
            QueueRequest(operation=operation, args=telegram_args, kwargs=queued_kwargs)
        ])
        if waiter is not None:
            def transfer(source: Future) -> None:
                if waiter.done():
                    return
                try:
                    waiter.set_result(source.result())
                except BaseException as error:
                    waiter.set_exception(error)
            queue_waiter.add_done_callback(transfer)
        for path in cleanup_files or ():
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        return row_id

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
        queued_kwargs = dict(kwargs)
        queued_kwargs["_slave_id"] = slave_id
        row_id = self._enqueue_send_task(
            (slave_id, chat_id), function, args, queued_kwargs, cleanup_files=cleanup_files
        )
        return self._make_send_receipt(
            self._create_queued_message_placeholder(chat_id, row_id), queued=True, task_id=row_id
        )

    def _enqueue_blocking_send_and_wait(
        self,
        slave_id: Optional[str],
        chat_id: int,
        function: Callable,
        args: tuple,
        kwargs: dict,
        *,
        cleanup_files: Optional[list] = None,
    ) -> SendReceipt:
        queued_kwargs = dict(kwargs)
        if slave_id:
            queued_kwargs["_slave_id"] = slave_id
        queued_kwargs["_send_mode"] = "blocking"
        row_id, queue_waiter = self._enqueue_requests([
            QueueRequest(function.__name__, args[1:] if args and args[0] is self else args, queued_kwargs)
        ])
        for path in cleanup_files or ():
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        try:
            result = queue_waiter.result(timeout=self.BLOCKING_SEND_TIMEOUT)
        except FutureTimeoutError as error:
            raise RuntimeError(
                f"Blocking send to chat {chat_id} timed out after {self.BLOCKING_SEND_TIMEOUT:g}s"
            ) from error
        return self._make_send_receipt(result, task_id=row_id)

    def enqueue_history_operation(
        self,
        *,
        source_key: str,
        target_chat_id: int,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
        history_entry_ids: Collection[int],
    ) -> Future:
        del source_key, target_chat_id, history_entry_ids
        request_kwargs = dict(kwargs)
        request_kwargs["_send_mode"] = "eventual"
        _row_id, waiter = self._enqueue_requests([QueueRequest(operation, args, request_kwargs)])
        return waiter

    def _enqueue_blocking_api_operation(
        self,
        *,
        target_chat_id: int,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
        required_sender_bot_id: Optional[str],
    ) -> object:
        del target_chat_id
        request_kwargs = dict(kwargs)
        request_kwargs["_send_mode"] = "blocking"
        if required_sender_bot_id is not None:
            request_kwargs["_required_sender_bot_id"] = required_sender_bot_id
        _row_id, waiter = self._enqueue_requests([QueueRequest(operation, args, request_kwargs)])
        return waiter.result(timeout=self.BLOCKING_SEND_TIMEOUT)

    def _enqueue_main_chat_mutation(
        self, operation: str, args: tuple, kwargs: Mapping[str, object]
    ) -> object:
        return self._enqueue_blocking_api_operation(
            target_chat_id=int(args[0] if args else kwargs["chat_id"]),
            operation=operation,
            args=args,
            kwargs=kwargs,
            required_sender_bot_id="__main__",
        )

    def select_sender(self, row, now: float) -> SenderSelectionResult:
        chat_id = row.telegram_chat_id
        required = row.required_sender_bot_id
        if required == "__main__":
            return SenderSelectionResult(selection=SenderSelection(self._bot, None))
        if required is not None:
            auxiliary = self.bot_pool.get_bot_by_id(required) if self.bot_pool else None
            if auxiliary is None or auxiliary.disabled:
                return SenderSelectionResult(terminal_error_class="required_sender_unavailable")
            membership = auxiliary.check_membership_tri(chat_id)
            if membership is None:
                return SenderSelectionResult(retry_at=now + self.MEMBERSHIP_RECHECK_SECONDS)
            if membership is not True:
                return SenderSelectionResult(terminal_error_class="required_sender_unavailable")
            return SenderSelectionResult(selection=SenderSelection(auxiliary.bot, str(auxiliary.bot_id)))

        candidates: list[SenderSelection] = [SenderSelection(self._bot, None)]
        unknown_auxiliary = False
        if self.bot_pool:
            for auxiliary, membership in self.bot_pool.candidate_bots(chat_id):
                if membership is None:
                    unknown_auxiliary = True
                elif membership:
                    candidates.append(SenderSelection(auxiliary.bot, str(auxiliary.bot_id)))
        if unknown_auxiliary:
            return SenderSelectionResult(retry_at=now + self.MEMBERSHIP_RECHECK_SECONDS)
        preferred = self.bot_pool.preferred_sender(row.slave_id) if self.bot_pool and row.slave_id else None
        if preferred is not None:
            for candidate in candidates:
                if candidate.sender_bot_id == str(preferred.bot_id):
                    return SenderSelectionResult(selection=candidate)
        return SenderSelectionResult(selection=min(candidates, key=lambda item: item.sender_bot_id or ""))

    def acquire_sender_limits(self, selection: SenderSelection, telegram_chat_id: int) -> bool:
        if selection.sender_bot_id is None:
            return self._rate_limiter.try_acquire(telegram_chat_id)
        auxiliary = self.bot_pool.get_bot_by_id(selection.sender_bot_id) if self.bot_pool else None
        return auxiliary is not None and auxiliary.try_acquire_limits(telegram_chat_id)

    def execute_queued_call(self, row, args: tuple, kwargs: dict, selection: SenderSelection) -> object:
        method = getattr(selection.sender, row.operation)
        return method(*args, **kwargs)

    def record_queued_success(self, row, result: object, selection: SenderSelection) -> None:
        if selection.sender_bot_id is not None and self.bot_pool and row.slave_id:
            self.bot_pool.record_successful_auxiliary_send(row.slave_id, selection.sender_bot_id)

    def record_queued_failure(self, row, error: BaseException, selection: SenderSelection) -> None:
        retry_after = self._rate_limit_retry_after_seconds(cast(Exception, error))
        if retry_after is not None:
            self._bot_chat_disabled_until[(selection.sender_bot_id, row.telegram_chat_id)] = (
                time.monotonic() + retry_after
            )

    def _queued_send_worker(self):
        self.logger.debug("Outbound queue worker started")
        while not self._send_worker_stop.is_set() and not self._outbound_scheduler.stopping:
            self._outbound_scheduler.harvest_completed()
            self._outbound_scheduler.dispatch_once()
            deadline = self._outbound_scheduler.next_deadline
            timeout = 0.25 if deadline is None else max(0.0, min(0.25, deadline - time.monotonic()))
            self._outbound_scheduler.wake_event.wait(timeout=timeout)
            self._outbound_scheduler.wake_event.clear()
        self._outbound_scheduler.stop_and_drain(self.SHUTDOWN_DRAIN_TIMEOUT)
        self._send_executor.shutdown(wait=False)
        self._outbound_queue.close()
        self.logger.debug("Outbound queue worker stopped")

    @staticmethod
    def _rate_limit_retry_after_seconds(error: Exception) -> Optional[float]:
        if isinstance(error, telegram.error.RetryAfter):
            retry_after_value = error.retry_after
            if isinstance(retry_after_value, timedelta):
                return retry_after_value.total_seconds()
            return float(retry_after_value)
        response = getattr(getattr(error, "__cause__", None), "response", None)
        if getattr(response, "status_code", None) == 429:
            return 60.0
        for error_text in (getattr(error, "message", None), str(error)):
            if not error_text:
                continue
            retry_after_match = re.search(
                r"(?:retry after|retry_after|retry in)\s+(\d+(?:\.\d+)?)",
                error_text,
                re.IGNORECASE,
            )
            if retry_after_match:
                return float(retry_after_match.group(1))
            if re.search(r"Too Many Requests|\b429\b|Flood", error_text, re.IGNORECASE):
                return 60.0
        return None

    @classmethod
    def _telegram_retry_delay_seconds(cls, retry_after: float, consecutive_failures: int) -> float:
        retry_after = max(float(retry_after), 0.0)
        delay = retry_after + cls.TELEGRAM_RETRY_AFTER_GRACE_SECONDS
        if consecutive_failures >= 2:
            floor = cls.TELEGRAM_RETRY_AFTER_REPEATED_FLOOR_SECONDS * (2 ** (consecutive_failures - 2))
            delay = max(delay, min(floor, cls.TELEGRAM_RETRY_AFTER_BACKOFF_CAP_SECONDS))
        return delay

    def _run_database_update_callback(self, on_complete: Optional[Callable[[], None]]):
        if on_complete is None:
            return
        try:
            on_complete()
        except Exception as e:
            self.logger.warning("Database update completion callback failed: %s", e)

    def _write_database_update(
        self,
        etm_msg,
        old_msg_id,
        real_tg_msg: TelegramMessage,
        *,
        sender_bot_id: Optional[str] = None,
        on_complete: Optional[Callable[[], None]] = None,
    ):
        """Write a sent Telegram message to the database once."""
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
                "DB write failed for tg_msg %s, dropping mapping: %s",
                getattr(real_tg_msg, 'message_id', '?'),
                e,
            )
        finally:
            self._run_database_update_callback(on_complete)

    def write_db_mapping(
        self,
        etm_msg,
        real_tg_msg: TelegramMessage,
        old_msg_id=None,
        *,
        sender_bot_id: Optional[str] = None,
        on_complete: Optional[Callable[[], None]] = None,
    ):
        """Write database mapping after a blocking send has succeeded."""
        self._write_database_update(
            etm_msg,
            old_msg_id,
            real_tg_msg,
            sender_bot_id=sender_bot_id,
            on_complete=on_complete,
        )

    def stop_queued_worker(self):
        """Set the queue stop boundary, then wait for its bounded drain."""
        self.logger.debug("Stopping outbound queue worker...")

        if hasattr(self, '_outbound_scheduler'):
            self._outbound_scheduler.stop_and_drain(self.SHUTDOWN_DRAIN_TIMEOUT)
        if hasattr(self, '_send_worker_stop'):
            self._send_worker_stop.set()
        if hasattr(self, '_outbound_scheduler'):
            self._outbound_scheduler.wake_event.set()

        if hasattr(self, '_send_worker_thread') and self._send_worker_thread.is_alive():
            self.logger.debug("Waiting for durable outbound worker to stop...")
            self._send_worker_thread.join(
                timeout=self.SHUTDOWN_DRAIN_TIMEOUT + self.SHUTDOWN_JOIN_GRACE
            )

            if self._send_worker_thread.is_alive():
                self.logger.warning("Durable outbound worker did not stop within timeout")
            else:
                self.logger.debug("Durable outbound worker stopped")

    @Decorators.rate_limit_decorator
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
        return self._bot.send_message(*args, **kwargs)

    @Decorators.rate_limit_decorator
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
        return self._bot.edit_message_text(**kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
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
        return self._bot.send_audio(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
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
        return self._bot.send_voice(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
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
        return self._bot.send_video(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
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
        return self._bot.send_document(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
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
        return self._bot.send_animation(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
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
        return self._bot.send_photo(*args, **kwargs)

    @Decorators.skip_on_rate_limit
    @Decorators.retry_on_chat_migration
    def send_chat_action(self, *args, **kwargs):
        message_thread_id = kwargs.pop('message_thread_id', None)
        if message_thread_id != None:
            kwargs['api_kwargs'] = { "message_thread_id":  message_thread_id}
        return self._bot.send_chat_action(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def edit_message_reply_markup(self, *args, **kwargs):
        return self._bot.edit_message_reply_markup(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def send_location(self, *args, **kwargs):
        return self._bot.send_location(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def send_venue(self, *args, **kwargs):
        return self._bot.send_venue(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def send_sticker(self, *args, **kwargs):
        return self._bot.send_sticker(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def forward_message(self, *args, **kwargs):
        return self._bot.forward_message(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def copy_message(self, *args, **kwargs):
        return self._bot.copy_message(*args, **kwargs)

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
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def edit_message_caption(self, *args, **kwargs):
        return self._bot.edit_message_caption(*args, **kwargs)

    @Decorators.rate_limit_decorator
    @Decorators.handle_rate_limit_error
    @Decorators.retry_on_chat_migration
    def edit_message_media(self, *args, **kwargs):
        return self._bot.edit_message_media(*args, **kwargs)

    def reply_error(self, update, errmsg):
        """
        A wrap that quote-reply a message with error details.

        Returns:
            telegram.Message: Message sent
        """
        return self.send_message(update.effective_chat.id, errmsg,
                                 reply_to_message_id=update.effective_message.message_id)

    @Decorators.retry_on_chat_migration
    def get_file(self, file_id: str) -> File:
        return cast(File, self._bot.get_file(file_id))

    def delete_message(self, chat_id, message_id, _sender_bot_id=None):
        required_sender = str(_sender_bot_id) if _sender_bot_id else "__main__"
        return self._enqueue_blocking_api_operation(
            target_chat_id=int(chat_id),
            operation="delete_message",
            args=(chat_id, message_id),
            kwargs={},
            required_sender_bot_id=required_sender,
        )

    @Decorators.retry_on_chat_migration
    def answer_callback_query(self, *args, prefix="", suffix="", text=None,
                              message_id=None, **kwargs):
        chat_id = kwargs.pop('chat_id', None)
        if text is None:
            return self._bot.answer_callback_query(
                *args, **kwargs
            )
        prefix = (prefix and (prefix + "\n")) or prefix
        suffix = (suffix and ("\n" + suffix)) or suffix

        if len(prefix + text + suffix) >= MAX_CALLBACK_QUERY_ANSWER_LENGTH:
            full_message = prefix + text + suffix
            keep_size = MAX_CALLBACK_QUERY_ANSWER_LENGTH // 3
            truncated = full_message[:keep_size] + "..." + full_message[-keep_size:]
            filename = f"{chat_id}_{message_id}.txt"
            if chat_id is not None:
                source_key = f"__callback__:{chat_id}"
                specs = [
                    OutboundTaskSpec(
                        source_key=source_key,
                        slave_id=None,
                        priority=True,
                        target_chat_id=int(chat_id),
                        message_thread_id=None,
                        operation="api_answer_callback_query",
                        args=args,
                        kwargs={**kwargs, "text": truncated},
                        required_sender_bot_id="__main__",
                    ),
                    OutboundTaskSpec(
                        source_key=source_key,
                        slave_id=None,
                        priority=True,
                        target_chat_id=int(chat_id),
                        message_thread_id=None,
                        operation="api_send_document",
                        args=(),
                        kwargs={
                            "chat_id": int(chat_id),
                            "document": io.StringIO(full_message),
                            "filename": filename,
                            "reply_to_message_id": message_id,
                            "caption": self._(
                                "Response is truncated due to its length. Full message is sent as attachment."
                            ),
                        },
                        depends_on_step_index=0,
                        run_condition=RunCondition.PREDECESSOR_SUCCESS,
                        required_sender_bot_id="__main__",
                    ),
                ]
                created = self._outbound_repository.create_workflow(specs, result_task_index=0)
                waiter: Future = Future()
                with self._outbound_registry_lock:
                    for task in created.tasks:
                        self._outbound_workflow_by_task[task.id] = created.workflow.id
                    self._outbound_waiters[created.workflow.id] = waiter
                    self._outbound_waiter_receipts[created.workflow.id] = False
                self._outbound_scheduler.wake_event.set()
                try:
                    return waiter.result(timeout=self.BLOCKING_SEND_TIMEOUT)
                except FutureTimeoutError as error:
                    with self._outbound_registry_lock:
                        if self._outbound_waiters.get(created.workflow.id) is waiter:
                            self._outbound_waiters.pop(created.workflow.id, None)
                            self._outbound_waiter_receipts.pop(created.workflow.id, None)
                    metrics = getattr(self, '_metrics', None)
                    if metrics:
                        metrics.waiter_timed_out("answer_callback_query")
                    raise RuntimeError(
                        f"Callback response workflow {created.workflow.id} timed out; durable work remains queued."
                    ) from error
            return self._bot.answer_callback_query(*args, text=truncated, **kwargs)
        self.logger.debug(f"answer_callback_query({args}, {kwargs})")
        return self._bot.answer_callback_query(
            *args, text=prefix + text + suffix, **kwargs
        )

    @Decorators.retry_on_chat_migration
    def get_chat_info(self, *args, **kwargs):
        return self._bot.get_chat(*args, **kwargs)

    def create_forum_topic(self, *args, **kwargs) -> ForumTopic:
        chat_id = int(args[0] if args else kwargs['chat_id'])
        return cast(ForumTopic, self._enqueue_blocking_api_operation(
            target_chat_id=chat_id,
            operation="create_forum_topic",
            args=args,
            kwargs=kwargs,
            required_sender_bot_id="__main__",
        ))

    def edit_forum_topic(self, *args, **kwargs):
        chat_id = int(args[0] if args else kwargs['chat_id'])
        return self._enqueue_blocking_api_operation(
            target_chat_id=chat_id,
            operation="edit_forum_topic",
            args=args,
            kwargs=kwargs,
            required_sender_bot_id="__main__",
        )

    def reopen_forum_topic(self, *args, **kwargs) -> bool:
        chat_id = int(args[0] if args else kwargs['chat_id'])
        return cast(bool, self._enqueue_blocking_api_operation(
            target_chat_id=chat_id,
            operation="reopen_forum_topic",
            args=args,
            kwargs=kwargs,
            required_sender_bot_id="__main__",
        ))

    def set_chat_title(self, *args, **kwargs):
        return self._enqueue_main_chat_mutation("set_chat_title", args, kwargs)

    def set_chat_photo(self, *args, **kwargs):
        return self._enqueue_main_chat_mutation("set_chat_photo", args, kwargs)

    def pin_chat_message(self, *args, **kwargs):
        return self._enqueue_main_chat_mutation("pin_chat_message", args, kwargs)

    def set_chat_description(self, *args, **kwargs):
        return self._enqueue_main_chat_mutation("set_chat_description", args, kwargs)

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
        if not hasattr(self, '_stopping') or not hasattr(self._stopping, 'set'):
            self._stopping = threading.Event()
        self._stopping.set()
        self.logger.info("Starting graceful shutdown...")

        # Log pending tasks count before stopping
        pending_count = 0
        if hasattr(self, '_outbound_scheduler'):
            pending_count = OutboundTask.select().where(
                OutboundTask.state.in_(TaskState.ACTIVE)
            ).count()

        if pending_count > 0:
            self.logger.info("Found %d pending queued send tasks", pending_count)

        # Stop the queued send worker first
        self.stop_queued_worker()

        metrics_httpd = getattr(self, '_metrics_httpd', None)
        if metrics_httpd:
            metrics_httpd.shutdown()
            metrics_httpd.server_close()

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
