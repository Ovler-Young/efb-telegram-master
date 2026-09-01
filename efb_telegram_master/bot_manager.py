# coding=utf-8
from __future__ import annotations

import asyncio
import collections
import collections.abc
from enum import Enum
import html
import io
import logging
import numbers
import os
import pickle
import re
import threading
import time
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
    QUEUED_OPERATIONS,
    QueueEnqueueError,
    QueuePersistenceError,
    QueueRequest,
    SchedulerStoppedError,
    SenderSelection,
    SenderSelectionResult,
)
from .ptb_compat import Filters
from .rate_limiter import SlidingWindowRateLimiter
from .utils import TelegramChatID, TelegramMessageID, message_id_to_str


BotChatKey: TypeAlias = Tuple[Optional[str], int]


class QueuedCompletionKind(str, Enum):
    RETRY_EVENTUAL = "retry_eventual"
    TERMINAL_FAILURE = "terminal_failure"
    SUCCESS = "success"


@dataclass(frozen=True)
class QueuedCompletionDecision:
    """The scheduler-facing terminal state for one completed queued call."""

    kind: QueuedCompletionKind
    retry_at: Optional[float] = None
    retry_reason: Optional[str] = None


class QueuedChatMigrationRetry(Exception):
    """Retry a retained call after handling Telegram chat migration."""


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
    'prefix',
    'suffix',
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

        @wraps(attr)
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
    TELEGRAM_RATE_LIMIT_FALLBACK_SECONDS = 60.0
    MEMBERSHIP_RECHECK_SECONDS = 0.25
    SHUTDOWN_DRAIN_TIMEOUT = 5.0
    SHUTDOWN_JOIN_GRACE = 1.0
    TRANSPORT_RETRY_SECONDS = 1.0
    _bot_chat_state_lock_initialization_lock = threading.Lock()

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
        _POSITIONAL_CHAT_ID_INDICES = {
            'edit_message_text': 1,
            'edit_message_media': 1,
        }

        @classmethod
        def exception_filter(cls, exception: Exception):
            cls.logger.exception("Exception: %s while sending request to Telegram server.", exception)
            return isinstance(exception, telegram.error.TimedOut)

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
                        chat_id_index = cls._POSITIONAL_CHAT_ID_INDICES.get(fn.__name__, 0)
                        chat_id = args[chat_id_index]
                        self.channel.chat_binding.chat_migration_by_id(chat_id, e.new_chat_id)
                        args = (
                            *args[:chat_id_index],
                            e.new_chat_id,
                            *args[chat_id_index + 1:],
                        )
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
        self._rate_limiter = SlidingWindowRateLimiter()

        self._cleanup_tls = threading.local()  # Thread-local for pending cleanup files
        self._shutdown_complete_event = threading.Event()
        self._graceful_stop_lock = threading.Lock()
        self._graceful_stop_complete = False
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
        self._bot_chat_state_lock = threading.Lock()
        self._bot_chat_disabled_until: dict[BotChatKey, float] = {}
        self._membership_failure_affinities: dict[BotChatKey, set[str]] = {}
        self._queued_db_log_contexts: dict[int, QueuedDbLogContext] = {}
        self._queued_completion_callbacks: dict[int, Optional[Callable[[], None]]] = {}
        self._queued_db_log_context_lock = threading.Lock()
        self._last_metrics_snapshot = 0.0
        from .etm_metrics import Metrics
        metrics_top_n, metrics_endpoint = self._parse_metrics_config(config.get('metrics'), self.logger)
        self._metrics = Metrics(namespace="etm")
        channel.db.set_metrics(self._metrics)
        if self.bot_pool:
            for auxiliary in self.bot_pool.bots:
                auxiliary.bind_metrics(self._metrics)
        self._metrics_httpd = None

        self._send_worker_count = self.DEFAULT_SEND_WORKER_COUNT
        self._outbound_queue = OutboundQueue(channel.db._base_path, metrics=self._metrics)
        self._send_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=self._send_worker_count, thread_name_prefix="ETM-send",
        )
        self._outbound_finalization_lock = threading.Lock()
        self._outbound_resources_finalized = False
        self._outbound_scheduler = OutboundQueueScheduler(
            self._outbound_queue,
            self,
            executor=self._send_executor,
            worker_count=self._send_worker_count,
        )
        self._register_runtime_metric_collectors(metrics_top_n)

        if metrics_endpoint is not None:
            metrics_host, metrics_port = metrics_endpoint
            self._metrics_httpd = self._start_metrics_endpoint(metrics_host, metrics_port)

        self.logger.debug("Durable outbound system initialized...")

        self.logger.debug("Adding base dispatchers...")
        self._add_base_dispatchers()
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
        if (
            not self._send_worker_stop.is_set()
            and not self._outbound_scheduler.stopping
            and getattr(self, "_send_worker_thread", None) is None
        ):
            self._send_worker_thread = threading.Thread(
                target=self._queued_send_worker,
                name="ETM queued send worker",
                daemon=True,
            )
            self._send_worker_thread.start()
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

    @staticmethod
    def _normalize_telegram_chat_id(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, numbers.Integral):
            raise QueueEnqueueError("chat_id must be a non-Boolean integral value.")
        return int(value)

    @staticmethod
    def _strip_private_queue_metadata(kwargs: Mapping[str, object]) -> dict[str, object]:
        return {key: value for key, value in kwargs.items() if key not in _INTERNAL_KWARGS}

    @staticmethod
    def _queued_chat_id_argument(
        operation: str, args: tuple, kwargs: Mapping[str, object]
    ) -> object:
        chat_id_index = TelegramBotManager.Decorators._POSITIONAL_CHAT_ID_INDICES.get(operation, 0)
        return args[chat_id_index] if len(args) > chat_id_index else kwargs.get("chat_id")

    @staticmethod
    def _rewrite_queued_chat_id(
        operation: str, args: tuple, kwargs: dict, new_chat_id: int
    ) -> tuple[tuple, dict]:
        if "chat_id" in kwargs:
            migrated_kwargs = dict(kwargs)
            migrated_kwargs["chat_id"] = new_chat_id
            return args, migrated_kwargs
        chat_id_index = TelegramBotManager.Decorators._POSITIONAL_CHAT_ID_INDICES.get(
            operation, 0
        )
        migrated_args = list(args)
        migrated_args[chat_id_index] = new_chat_id
        return tuple(migrated_args), kwargs

    def _queued_operation_callable(self, operation: str) -> Callable[..., object]:
        method = self._queue_operation(operation)

        def queued_operation(*args: object, **kwargs: object) -> object:
            telegram_args = args[1:] if args and args[0] is self else args
            return method(*telegram_args, **kwargs)

        queued_operation.__name__ = operation
        return queued_operation

    @staticmethod
    def _affix_queued_content(content: object, prefix: object, suffix: object, parse_mode: object) -> str:
        text = str(content)
        prefix_text = f"{prefix}\n" if prefix else ""
        suffix_text = f"\n{suffix}" if suffix else ""
        if str(parse_mode).lower() == "html":
            prefix_text = html.escape(prefix_text)
            suffix_text = html.escape(suffix_text)
        return prefix_text + text + suffix_text

    def _route_affixed_queued_operation(
        self,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
        *,
        eventual_capable: bool,
        content_key: str,
        content_index: int,
        prefix: object = "",
        suffix: object = "",
    ) -> SendReceipt:
        queued_kwargs = dict(kwargs)
        prefix = queued_kwargs.pop("prefix", prefix)
        suffix = queued_kwargs.pop("suffix", suffix)
        queued_args = list(args)
        if len(queued_args) > content_index:
            content = queued_args[content_index]
            queued_args[content_index] = self._affix_queued_content(
                content, prefix, suffix, queued_kwargs.get("parse_mode", "")
            )
        else:
            content = queued_kwargs.get(content_key, "")
            queued_kwargs[content_key] = self._affix_queued_content(
                content, prefix, suffix, queued_kwargs.get("parse_mode", "")
            )
        return self._route_queued_operation(
            operation, tuple(queued_args), queued_kwargs, eventual_capable=eventual_capable
        )

    def _route_queued_operation(
        self,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
        *,
        eventual_capable: bool,
    ) -> SendReceipt:
        if operation not in QUEUED_OPERATIONS:
            raise QueueEnqueueError(f"Unsupported queued operation: {operation}")
        queued_kwargs = dict(kwargs)
        sender_bot_id = queued_kwargs.pop("_sender_bot_id", None)
        slave_id = queued_kwargs.pop("_slave_id", None)
        send_mode = queued_kwargs.pop("_send_mode", "blocking")
        force_main_bot = queued_kwargs.pop("_force_main_bot", False)
        queued_kwargs.pop("_required_sender_bot_id", None)
        db_log_context = queued_kwargs.pop("_queued_db_log_context", None)
        if db_log_context is not None and not isinstance(db_log_context, QueuedDbLogContext):
            raise QueueEnqueueError("_queued_db_log_context must be a QueuedDbLogContext when supplied.")
        if send_mode not in {"blocking", "eventual"}:
            raise QueueEnqueueError("_send_mode must be 'blocking' or 'eventual'.")

        chat_id = self._queued_chat_id_argument(operation, args, queued_kwargs)
        normalized_chat_id = self._normalize_telegram_chat_id(chat_id)
        has_callback = _has_callback_keyboard(queued_kwargs.get("reply_markup"))
        cleanup_tls = getattr(self, "_cleanup_tls", None)
        cleanup_files = getattr(cleanup_tls, "pending_cleanup", [])[:]
        if cleanup_tls is not None:
            cleanup_tls.pending_cleanup = []

        function = self._queued_operation_callable(operation)
        function_args = (self,) + args
        if eventual_capable and send_mode == "eventual" and slave_id and not has_callback:
            return self._enqueue_eventual_send(
                str(slave_id),
                normalized_chat_id,
                function,
                function_args,
                queued_kwargs,
                cleanup_files=cleanup_files,
                db_log_context=db_log_context,
            )

        blocking_kwargs = dict(queued_kwargs)
        required_sender_bot_id = str(sender_bot_id) if sender_bot_id and not eventual_capable else None
        if required_sender_bot_id is not None:
            blocking_kwargs["_required_sender_bot_id"] = required_sender_bot_id
        if force_main_bot or (eventual_capable and has_callback) or (
            not eventual_capable and required_sender_bot_id is None
        ):
            blocking_kwargs["_required_sender_bot_id"] = "__main__"
        return self._enqueue_blocking_send_and_wait(
            str(slave_id) if slave_id else None,
            normalized_chat_id,
            function,
            function_args,
            blocking_kwargs,
            cleanup_files=cleanup_files,
        )

    def _call_direct_operation(
        self, operation: str, args: tuple, kwargs: Mapping[str, object]
    ) -> object:
        return getattr(self._bot, operation)(*args, **self._strip_private_queue_metadata(kwargs))

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

    def _start_metrics_endpoint(self, host: str, port: int):
        from .etm_metrics import start_metrics_server

        try:
            return start_metrics_server(host, port, registry=self._metrics.registry)
        except OSError as error:
            self.logger.warning(
                "Unable to start Prometheus endpoint on %s:%d: %s", host, port, error
            )
            return None

    def _register_runtime_metric_collectors(self, top_n: int) -> None:
        """Bind bounded scrape callbacks after all outbound runtime state exists."""
        from .etm_metrics import DestinationQueueSnapshot, WorkerSnapshot

        def destination_snapshot() -> list[DestinationQueueSnapshot]:
            return [
                DestinationQueueSnapshot(destination, depth, oldest_age)
                for destination, depth, oldest_age in self._outbound_queue.destination_snapshot(top_n)
            ]

        def worker_snapshot() -> WorkerSnapshot:
            worker = getattr(self, "_send_worker_thread", None)
            return WorkerSnapshot(
                healthy=bool(worker is not None and worker.is_alive()),
                in_flight=self._outbound_scheduler.in_flight_count(),
            )

        self._metrics.register_destination_queue_collector(destination_snapshot, top_n)
        self._metrics.register_worker_collector(worker_snapshot)
        self._metrics.register_cooldown_collector(self._cooldown_snapshot)
        self._metrics.register_auxiliary_count_collector(self._auxiliary_count_snapshot)
        self._metrics.register_membership_cache_collector(self._membership_cache_snapshot)
        self._metrics.register_rate_limit_occupancy_collector(self._rate_limit_occupancy_snapshot)

    def _get_bot_chat_state_lock(self):
        lock = getattr(self, "_bot_chat_state_lock", None)
        if lock is not None:
            return lock
        with self._bot_chat_state_lock_initialization_lock:
            lock = getattr(self, "_bot_chat_state_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._bot_chat_state_lock = lock
            return lock

    def _cooldown_snapshot(self) -> dict[str, float]:
        now = time.monotonic()
        cooldowns = {"main": 0.0, "auxiliary": 0.0}
        with self._get_bot_chat_state_lock():
            cooldown_entries = tuple(self._bot_chat_disabled_until.items())
        for (sender_bot_id, _chat_id), deadline in cooldown_entries:
            sender_kind = "main" if sender_bot_id is None else "auxiliary"
            cooldowns[sender_kind] = max(cooldowns[sender_kind], max(0.0, deadline - now))
        return cooldowns

    def _auxiliary_count_snapshot(self) -> dict[str, int]:
        if not self.bot_pool:
            return {"enabled": 0, "disabled": 0}
        return self.bot_pool.auxiliary_count_snapshot()

    def _membership_cache_snapshot(self) -> dict[str, int]:
        if not self.bot_pool:
            return {"member": 0, "not_member": 0, "unknown_probe_pending": 0}
        return self.bot_pool.membership_cache_snapshot()

    def _rate_limit_occupancy_snapshot(self) -> dict[str, float]:
        occupancy = self._rate_limiter.occupancy_snapshot()
        if self.bot_pool:
            for scope, value in self.bot_pool.rate_limit_occupancy_snapshot().items():
                occupancy[scope] = max(occupancy[scope], value)
        return occupancy

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

    # Queue rows remain durable only until the scheduler commits their deletion.
    def _queue_operation(self, operation: str) -> Callable[..., object]:
        method = getattr(self._bot, operation, None)
        if not callable(method):
            raise QueueEnqueueError(f"Telegram bot has no queued operation {operation!r}.")
        return cast(Callable[..., object], method)

    def _enqueue_requests(
        self,
        requests: list[QueueRequest],
        *,
        db_log_context: Optional[QueuedDbLogContext] = None,
    ) -> tuple[str, Future]:
        with self._outbound_scheduler._lock:
            if self._outbound_scheduler.stopping:
                error = self._outbound_scheduler.failure or SchedulerStoppedError(
                    "Outbound scheduler stopped."
                )
                raise error
            durable_requests = requests
            if db_log_context is not None:
                blocking_context = any(
                    request.kwargs.get("_send_mode", "eventual") == "blocking"
                    for request in requests
                )
                if not blocking_context:
                    encoded_context = self._encode_queued_log_context(db_log_context)
                    durable_requests = [
                        QueueRequest(
                            request.operation, request.args, request.kwargs, encoded_context
                        )
                        for request in requests
                    ]
            else:
                blocking_context = False
            row_id, waiter = self._outbound_queue.enqueue_many(
                durable_requests, self._queue_operation
            )
            if db_log_context is not None:
                with self._queued_db_log_context_lock:
                    if blocking_context:
                        self._queued_db_log_contexts[row_id] = db_log_context
                    else:
                        self._queued_completion_callbacks[row_id] = db_log_context.on_complete
            self._outbound_scheduler.wake_event.set()
            return str(row_id), waiter

    def _enqueue_send_task(
        self,
        function: Callable,
        args: tuple,
        kwargs: dict,
        cleanup_files: Optional[list] = None,
        *,
        db_log_context: Optional[QueuedDbLogContext] = None,
    ) -> str:
        operation = function.__name__
        telegram_args = args[1:] if args and args[0] is self else args
        queued_kwargs = dict(kwargs)
        row_id, _ = self._enqueue_requests([
            QueueRequest(operation=operation, args=telegram_args, kwargs=queued_kwargs)
        ], db_log_context=db_log_context)
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
        db_log_context: Optional[QueuedDbLogContext] = None,
    ) -> SendReceipt:
        queued_kwargs = dict(kwargs)
        queued_kwargs["_slave_id"] = slave_id
        queued_kwargs["_send_mode"] = "eventual"
        row_id = self._enqueue_send_task(
            function,
            args,
            queued_kwargs,
            cleanup_files=cleanup_files,
            db_log_context=db_log_context,
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
        telegram_kwargs = self._strip_private_queue_metadata(kwargs)
        target_chat_id = OutboundQueue._destination(
            self._queue_operation(operation), args, telegram_kwargs
        )
        return self._enqueue_blocking_api_operation(
            target_chat_id=target_chat_id,
            operation=operation,
            args=args,
            kwargs=telegram_kwargs,
            required_sender_bot_id="__main__",
        )

    def select_sender(self, row, now: float) -> SenderSelectionResult:
        chat_id = row.telegram_chat_id
        required = row.required_sender_bot_id
        if required == "__main__":
            return self._select_available_sender(SenderSelection(self._bot, None), chat_id, now)
        if required is not None:
            auxiliary = self.bot_pool.get_bot_by_id(required) if self.bot_pool else None
            if auxiliary is None or auxiliary.disabled:
                return SenderSelectionResult(terminal_error_class="required_sender_unavailable")
            membership = auxiliary.check_membership_tri(chat_id)
            if membership is None:
                return SenderSelectionResult(retry_at=now + self.MEMBERSHIP_RECHECK_SECONDS)
            if membership is not True:
                return SenderSelectionResult(terminal_error_class="required_sender_unavailable")
            return self._select_available_sender(
                SenderSelection(auxiliary.bot, str(auxiliary.bot_id)), chat_id, now
            )

        candidates: list[tuple[int, str, SenderSelection, float]] = []
        main_selection = SenderSelection(self._bot, None)
        main_result = self._select_available_sender(main_selection, chat_id, now)
        if main_result.selection is not None:
            candidates.append((1, "", main_selection, now))
        elif main_result.retry_at is not None:
            candidates.append((1, "", main_selection, main_result.retry_at))

        membership_retry_at: Optional[float] = None
        if self.bot_pool:
            for auxiliary, membership in self.bot_pool.candidate_bots(chat_id):
                if membership is None:
                    retry_at = now + self.MEMBERSHIP_RECHECK_SECONDS
                    membership_retry_at = (
                        retry_at
                        if membership_retry_at is None
                        else min(membership_retry_at, retry_at)
                    )
                elif membership:
                    selection = SenderSelection(auxiliary.bot, str(auxiliary.bot_id))
                    candidate_result = self._select_available_sender(selection, chat_id, now)
                    if candidate_result.selection is not None:
                        deadline = now
                    elif candidate_result.retry_at is not None:
                        deadline = candidate_result.retry_at
                    else:
                        continue
                    preferred = self.bot_pool.preferred_sender(row.slave_id) if row.slave_id else None
                    affinity_rank = 0 if preferred is auxiliary else 2
                    candidates.append((affinity_rank, str(auxiliary.bot_id), selection, deadline))
        selectable = [candidate for candidate in candidates if candidate[3] <= now]
        if selectable:
            _rank, _bot_id, selection, _deadline = min(selectable, key=lambda candidate: candidate[:2])
            return SenderSelectionResult(selection=selection)
        if membership_retry_at is not None:
            return SenderSelectionResult(retry_at=membership_retry_at)
        retry_deadlines = [candidate[3] for candidate in candidates]
        if retry_deadlines:
            return SenderSelectionResult(retry_at=min(retry_deadlines))
        return SenderSelectionResult(retry_at=now + self.MEMBERSHIP_RECHECK_SECONDS)

    def _select_available_sender(
        self, selection: SenderSelection, chat_id: int, now: float
    ) -> SenderSelectionResult:
        with self._get_bot_chat_state_lock():
            cooldown_until = self._bot_chat_disabled_until.get((selection.sender_bot_id, chat_id), 0.0)
        limiter_delay = self._sender_limiter_delay(selection, chat_id)
        retry_at = max(cooldown_until, now + limiter_delay)
        if retry_at > now:
            return SenderSelectionResult(retry_at=retry_at)
        return SenderSelectionResult(selection=selection)

    def _sender_limiter_delay(self, selection: SenderSelection, chat_id: int) -> float:
        if selection.sender_bot_id is None:
            peek_delay = getattr(self._rate_limiter, "peek_delay", None)
            return 0.0 if peek_delay is None else float(peek_delay(chat_id))
        auxiliary = self.bot_pool.get_bot_by_id(selection.sender_bot_id) if self.bot_pool else None
        return 0.0 if auxiliary is None else float(auxiliary.peek_delay(chat_id))

    def acquire_sender_limits(self, selection: SenderSelection, telegram_chat_id: int) -> bool:
        if selection.sender_bot_id is None:
            return self._rate_limiter.try_acquire(telegram_chat_id)
        auxiliary = self.bot_pool.get_bot_by_id(selection.sender_bot_id) if self.bot_pool else None
        return auxiliary is not None and auxiliary.try_acquire_limits(telegram_chat_id)

    @staticmethod
    def _rewind_queued_files(args: tuple, kwargs: Mapping[str, object]) -> None:
        for value in (*args, *kwargs.values()):
            seek = getattr(value, "seek", None)
            if callable(seek):
                seek(0)

    @staticmethod
    def _queued_content_argument(
        args: tuple,
        kwargs: Mapping[str, object],
        content_key: str,
        content_index: int,
    ) -> tuple[Optional[str], bool]:
        if len(args) > content_index:
            content = args[content_index]
            return (content, True) if isinstance(content, str) else (None, True)
        content = kwargs.get(content_key)
        return (content, False) if isinstance(content, str) else (None, False)

    def execute_queued_call(self, row, args: tuple, kwargs: dict, selection: SenderSelection) -> object:
        sender = cast(SyncBotProtocol, selection.sender)
        method = getattr(sender, row.operation)
        telegram_kwargs = self._strip_private_queue_metadata(kwargs)
        telegram_args = args
        migration_retried = False

        def call_method() -> object:
            nonlocal migration_retried, telegram_args, telegram_kwargs
            try:
                return method(*telegram_args, **telegram_kwargs)
            except telegram.error.ChatMigrated as error:
                if migration_retried:
                    raise
                migration_retried = True
                old_chat_id = self._queued_chat_id_argument(
                    row.operation, telegram_args, telegram_kwargs
                )
                try:
                    self.channel.chat_binding.chat_migration_by_id(old_chat_id, error.new_chat_id)
                except Exception as migration_error:
                    if getattr(row, "priority", 1) == 0:
                        raise QueuedChatMigrationRetry(str(migration_error)) from migration_error
                    raise
                telegram_args, telegram_kwargs = self._rewrite_queued_chat_id(
                    row.operation, telegram_args, telegram_kwargs, error.new_chat_id
                )
                self._rewind_queued_files(telegram_args, telegram_kwargs)
                if getattr(row, "priority", 1) == 0:
                    self._outbound_queue.retarget(
                        row.id, error.new_chat_id, telegram_args, telegram_kwargs
                    )
                    raise QueuedChatMigrationRetry(
                        f"Telegram chat migrated to {error.new_chat_id}."
                    ) from error
                try:
                    return method(*telegram_args, **telegram_kwargs)
                except (telegram.error.RetryAfter, telegram.error.NetworkError) as retry_error:
                    setattr(retry_error, "_etm_telegram_chat_id", error.new_chat_id)
                    raise

        content_spec = {
            "send_message": ("text", 1, int(telegram.constants.MessageLimit.MAX_TEXT_LENGTH)),
            "edit_message_text": ("text", 0, int(telegram.constants.MessageLimit.MAX_TEXT_LENGTH)),
            "send_audio": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
            "send_voice": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
            "send_video": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
            "send_document": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
            "send_animation": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
            "send_photo": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
            "edit_message_caption": ("caption", 3, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
        }.get(row.operation)
        attachment: Optional[io.BytesIO] = None
        content_key: Optional[str] = None
        original_parse_mode = str(telegram_kwargs.get("parse_mode", "")).lower()
        if content_spec is not None:
            content_key, content_index, content_limit = content_spec
            full_content, is_positional = self._queued_content_argument(
                telegram_args, telegram_kwargs, content_key, content_index
            )
            if full_content is not None and len(full_content) >= content_limit:
                attachment_content = full_content
                if original_parse_mode == "html":
                    attachment_content = (
                        "<html><head><meta charset='utf-8'></head>"
                        "<body><pre style='white-space:pre-wrap'>"
                        + full_content
                        + "</pre></body></html>"
                    )
                attachment = io.BytesIO(attachment_content.encode("utf-8"))
                truncated = full_content[:100] + "\n...\n" + full_content[-100:]
                if is_positional:
                    mutable_args = list(telegram_args)
                    mutable_args[content_index] = truncated
                    telegram_args = tuple(mutable_args)
                else:
                    telegram_kwargs[content_key] = truncated
        try:
            result = call_method()
        except telegram.error.BadRequest as error:
            if not error.message.lower().startswith("can't parse entities") or "parse_mode" not in telegram_kwargs:
                raise
            telegram_kwargs.pop("parse_mode")
            self._rewind_queued_files(telegram_args, telegram_kwargs)
            result = call_method()
        if attachment is None or content_key is None:
            return result
        chat_id = self._queued_chat_id_argument(row.operation, telegram_args, telegram_kwargs)
        message_id = getattr(result, "message_id", None)
        if chat_id is None or message_id is None:
            return result
        extension = (
            ".md" if original_parse_mode == "markdown"
            else ".html" if original_parse_mode == "html" else ".txt"
        )
        label = "Message" if content_key == "text" else "Caption"
        sender.send_document(
            chat_id,
            attachment,
            filename=f"{chat_id}_{message_id}{extension}",
            reply_to_message_id=message_id,
            caption=f"{label} is truncated due to its length. Full message is sent as attachment.",
        )
        return result

    @staticmethod
    def _encode_queued_log_context(context: QueuedDbLogContext) -> bytes:
        try:
            return b"\x01" + pickle.dumps((context.etm_msg, context.old_msg_id), protocol=5)
        except Exception as error:
            raise QueueEnqueueError("Unable to serialize queued database log context.") from error

    @staticmethod
    def _decode_queued_log_context(payload: object) -> tuple['ETMMsg', Optional['OldMsgID']]:
        if not isinstance(payload, bytes) or not payload or payload[0] != 1:
            raise QueuePersistenceError("Queued database log context has an unknown version.")
        try:
            value = pickle.loads(payload[1:])
        except Exception as error:
            raise QueuePersistenceError("Queued database log context cannot be decoded.") from error
        if not isinstance(value, tuple) or len(value) != 2:
            raise QueuePersistenceError("Queued database log context has an invalid shape.")
        return cast('ETMMsg', value[0]), cast(Optional['OldMsgID'], value[1])

    @staticmethod
    def encode_queued_completion_receipt(result: object, selection: SenderSelection) -> bytes:
        try:
            return b"\x01" + pickle.dumps((result, selection.sender_bot_id), protocol=5)
        except Exception as error:
            raise QueuePersistenceError("Unable to serialize queued Telegram completion receipt.") from error

    @staticmethod
    def _decode_queued_completion_receipt(payload: object) -> tuple[TelegramMessage, Optional[str]]:
        if not isinstance(payload, bytes) or not payload or payload[0] != 1:
            raise QueuePersistenceError("Queued Telegram completion receipt has an unknown version.")
        try:
            value = pickle.loads(payload[1:])
        except Exception as error:
            raise QueuePersistenceError("Queued Telegram completion receipt cannot be decoded.") from error
        if not isinstance(value, tuple) or len(value) != 2 or not isinstance(value[1], (str, type(None))):
            raise QueuePersistenceError("Queued Telegram completion receipt has an invalid shape.")
        return cast(TelegramMessage, value[0]), value[1]

    def _pop_queued_db_log_context(self, row_id: object) -> Optional[QueuedDbLogContext]:
        if not isinstance(row_id, int):
            return None
        contexts = getattr(self, "_queued_db_log_contexts", None)
        context_lock = getattr(self, "_queued_db_log_context_lock", None)
        if contexts is None or context_lock is None:
            return None
        with context_lock:
            return contexts.pop(row_id, None)

    def _pop_queued_completion_callback(self, row_id: object) -> Optional[Callable[[], None]]:
        if not isinstance(row_id, int):
            return None
        callbacks = getattr(self, "_queued_completion_callbacks", None)
        context_lock = getattr(self, "_queued_db_log_context_lock", None)
        if callbacks is None or context_lock is None:
            return None
        with context_lock:
            return callbacks.pop(row_id, None)

    def _finish_queued_database_update(
        self,
        row_id: object,
        real_tg_msg: Optional[TelegramMessage] = None,
        *,
        sender_bot_id: Optional[str] = None,
    ) -> None:
        db_log_context = self._pop_queued_db_log_context(row_id)
        if db_log_context is None:
            return
        if real_tg_msg is None:
            self._run_database_update_callback(db_log_context.on_complete)
            return
        self._write_database_update(
            db_log_context.etm_msg,
            db_log_context.old_msg_id,
            real_tg_msg,
            sender_bot_id=sender_bot_id,
            on_complete=db_log_context.on_complete,
        )

    def reconcile_queued_delivery(self, row) -> bool:
        """Write a persisted Telegram completion to MsgLog."""
        if row.log_context is None or row.completion_receipt is None:
            return False
        try:
            etm_msg, old_msg_id = self._decode_queued_log_context(row.log_context)
            real_tg_msg, sender_bot_id = self._decode_queued_completion_receipt(
                row.completion_receipt
            )
            etm_msg.type_telegram = get_msg_type(real_tg_msg)
            etm_msg.put_telegram_file(real_tg_msg)
            self.channel.db.add_or_update_message_log(
                etm_msg,
                real_tg_msg,
                old_msg_id,
                sender_bot_id=sender_bot_id,
            )
        except Exception as error:
            self.logger.warning(
                "MsgLog reconciliation failed for durable queue row %s: %s", row.id, error
            )
            return False
        self._run_database_update_callback(self._pop_queued_completion_callback(row.id))
        return True

    def record_queued_success(
        self, row, result: object, selection: SenderSelection
    ) -> QueuedCompletionDecision:
        if getattr(row, "log_context", None) is None:
            self._finish_queued_database_update(
                getattr(row, "id", None),
                cast(TelegramMessage, result),
                sender_bot_id=selection.sender_bot_id,
            )
        if selection.sender_bot_id is not None and self.bot_pool and row.slave_id:
            self.bot_pool.record_successful_auxiliary_send(row.slave_id, selection.sender_bot_id)
        return QueuedCompletionDecision(QueuedCompletionKind.SUCCESS)

    def record_queued_failure(
        self, row, error: BaseException, selection: SenderSelection
    ) -> QueuedCompletionDecision:
        if row.priority == 0 and isinstance(error, QueuedChatMigrationRetry):
            return QueuedCompletionDecision(
                QueuedCompletionKind.RETRY_EVENTUAL,
                time.monotonic(),
                "migration",
            )

        telegram_chat_id = getattr(error, "_etm_telegram_chat_id", row.telegram_chat_id)
        key = (selection.sender_bot_id, telegram_chat_id)
        if selection.sender_bot_id is not None and row.slave_id:
            with self._get_bot_chat_state_lock():
                affinities = getattr(self, "_membership_failure_affinities", None)
                if affinities is None:
                    affinities = self._membership_failure_affinities = {}
                affinities.setdefault(key, set()).add(row.slave_id)

        if row.priority == 0 and isinstance(error, telegram.error.RetryAfter):
            retry_after = self._retry_after_seconds(error)
            retry_at = time.monotonic() + retry_after
            with self._get_bot_chat_state_lock():
                self._bot_chat_disabled_until[key] = retry_at
            return QueuedCompletionDecision(QueuedCompletionKind.RETRY_EVENTUAL, retry_at)

        if (
            row.priority == 0
            and isinstance(error, telegram.error.NetworkError)
            and not isinstance(error, telegram.error.BadRequest)
        ):
            return QueuedCompletionDecision(
                QueuedCompletionKind.RETRY_EVENTUAL,
                time.monotonic() + self.TRANSPORT_RETRY_SECONDS,
            )

        cooldown_seconds = self._rate_limit_retry_after_seconds(cast(Exception, error))
        if cooldown_seconds is not None:
            with self._get_bot_chat_state_lock():
                self._bot_chat_disabled_until[key] = time.monotonic() + cooldown_seconds
        self._finish_queued_database_update(getattr(row, "id", None))
        self._run_database_update_callback(
            self._pop_queued_completion_callback(getattr(row, "id", None))
        )
        return QueuedCompletionDecision(QueuedCompletionKind.TERMINAL_FAILURE)

    def record_queued_retry_after(
        self, row, error: telegram.error.RetryAfter, selection: SenderSelection
    ) -> None:
        """Record a sender/chat cooldown without completing the queued database update."""
        retry_at = time.monotonic() + self._retry_after_seconds(error)
        with self._get_bot_chat_state_lock():
            self._bot_chat_disabled_until[(selection.sender_bot_id, row.telegram_chat_id)] = retry_at

    def remove_confirmed_non_member_affinity_for_sender_chat(
        self, sender_bot_id: str, telegram_chat_id: int
    ) -> None:
        with self._get_bot_chat_state_lock():
            affinities = getattr(self, "_membership_failure_affinities", {})
            slave_ids = affinities.pop((sender_bot_id, telegram_chat_id), set())
        if self.bot_pool:
            for slave_id in slave_ids:
                self.bot_pool.remove_failed_membership_affinity(slave_id, sender_bot_id)

    def _queued_send_worker(self):
        self.logger.debug("Outbound queue worker started")
        try:
            while not self._send_worker_stop.is_set() and not self._outbound_scheduler.stopping:
                self._outbound_scheduler.harvest_completed()
                self._outbound_scheduler.dispatch_once()
                deadline = self._outbound_scheduler.next_deadline
                timeout = 0.25 if deadline is None else max(0.0, min(0.25, deadline - time.monotonic()))
                self._outbound_scheduler.wake_event.wait(timeout=timeout)
                self._outbound_scheduler.wake_event.clear()
        finally:
            self._outbound_scheduler.stop_and_drain(self.SHUTDOWN_DRAIN_TIMEOUT)
            self._finalize_outbound_resources()
            self.logger.debug("Outbound queue worker stopped")

    @staticmethod
    def _retry_after_seconds(error: telegram.error.RetryAfter) -> float:
        retry_after_value = error.retry_after
        if isinstance(retry_after_value, timedelta):
            return retry_after_value.total_seconds()
        return float(retry_after_value)

    @classmethod
    def _rate_limit_retry_after_seconds(cls, error: Exception) -> Optional[float]:
        if isinstance(error, telegram.error.RetryAfter):
            return cls._retry_after_seconds(error)
        response = getattr(getattr(error, "__cause__", None), "response", None)
        if getattr(response, "status_code", None) == 429:
            return cls.TELEGRAM_RATE_LIMIT_FALLBACK_SECONDS
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
                return cls.TELEGRAM_RATE_LIMIT_FALLBACK_SECONDS
        return None

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

        worker_thread = getattr(self, '_send_worker_thread', None)
        if worker_thread is not None and worker_thread.is_alive():
            self.logger.debug("Waiting for durable outbound worker to stop...")
            worker_thread.join(
                timeout=self.SHUTDOWN_DRAIN_TIMEOUT + self.SHUTDOWN_JOIN_GRACE
            )

            if worker_thread.is_alive():
                self.logger.warning("Durable outbound worker did not stop within timeout")
                return
        self._finalize_outbound_resources()
        self.logger.debug("Durable outbound worker stopped")

    def _finalize_outbound_resources(self) -> None:
        """Close caller-owned scheduler resources after the worker has exited."""
        with self._outbound_finalization_lock:
            if self._outbound_resources_finalized:
                return
            self._outbound_resources_finalized = True
            try:
                self._send_executor.shutdown(wait=False)
            finally:
                self._outbound_queue.close()

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
        return self._route_affixed_queued_operation(
            "send_message", args, kwargs, eventual_capable=True,
            content_key="text", content_index=1, prefix=prefix, suffix=suffix,
        )

    @Decorators.retry_on_chat_migration
    def edit_message_text(self, *args, prefix='', suffix='', **kwargs):
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
        return self._route_affixed_queued_operation(
            "edit_message_text", args, kwargs, eventual_capable=False,
            content_key="text", content_index=0, prefix=prefix, suffix=suffix,
        )

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
        return self._route_affixed_queued_operation(
            "send_audio", args, kwargs, eventual_capable=True, content_key="caption", content_index=2
        )

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
        return self._route_affixed_queued_operation(
            "send_voice", args, kwargs, eventual_capable=True, content_key="caption", content_index=2
        )

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
        return self._route_affixed_queued_operation(
            "send_video", args, kwargs, eventual_capable=True, content_key="caption", content_index=2
        )

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
        return self._route_affixed_queued_operation(
            "send_document", args, kwargs, eventual_capable=True, content_key="caption", content_index=2
        )

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
        return self._route_affixed_queued_operation(
            "send_animation", args, kwargs, eventual_capable=True, content_key="caption", content_index=2
        )

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
        return self._route_affixed_queued_operation(
            "send_photo", args, kwargs, eventual_capable=True, content_key="caption", content_index=2
        )

    @Decorators.retry_on_chat_migration
    def send_media_group(self, *args, **kwargs):
        return self._route_queued_operation("send_media_group", args, kwargs, eventual_capable=True)

    @Decorators.retry_on_chat_migration
    def send_chat_action(self, *args, **kwargs):
        queued_kwargs = dict(kwargs)
        message_thread_id = queued_kwargs.pop('message_thread_id', None)
        if message_thread_id is not None:
            api_kwargs = dict(cast(Mapping[str, object], queued_kwargs.get('api_kwargs', {})))
            api_kwargs['message_thread_id'] = message_thread_id
            queued_kwargs['api_kwargs'] = api_kwargs
        return self._call_direct_operation("send_chat_action", args, queued_kwargs)

    @Decorators.retry_on_chat_migration
    def edit_message_reply_markup(self, *args, **kwargs):
        if (args and args[0] is None) or (not args and kwargs.get("chat_id") is None):
            return self._call_direct_operation("edit_message_reply_markup", args, kwargs)
        return self._enqueue_main_chat_mutation("edit_message_reply_markup", args, kwargs)

    @Decorators.retry_on_chat_migration
    def send_location(self, *args, **kwargs):
        return self._enqueue_main_chat_mutation("send_location", args, kwargs)

    @Decorators.retry_on_chat_migration
    def send_venue(self, *args, **kwargs):
        return self._enqueue_main_chat_mutation("send_venue", args, kwargs)

    @Decorators.retry_on_chat_migration
    def send_sticker(self, *args, **kwargs):
        return self._route_queued_operation("send_sticker", args, kwargs, eventual_capable=True)

    @Decorators.retry_on_chat_migration
    def forward_message(self, *args, **kwargs):
        return self._route_queued_operation("forward_message", args, kwargs, eventual_capable=True)

    @Decorators.retry_on_chat_migration
    def copy_message(self, *args, **kwargs):
        return self._route_queued_operation("copy_message", args, kwargs, eventual_capable=True)

    def get_me(self, *args, **kwargs):
        return self._call_direct_operation("get_me", args, kwargs)

    def session_expired(self, update: Update, context: CallbackContext):
        assert isinstance(update, Update)
        assert update.effective_message
        assert update.effective_chat
        if update.callback_query:
            self.answer_callback_query(update.callback_query.id)
        self.edit_message_text(text=self._("Session expired. Please try again. (SE01)"),
                               chat_id=update.effective_chat.id,
                               message_id=update.effective_message.message_id)

    @Decorators.retry_on_chat_migration
    def edit_message_caption(self, *args, **kwargs):
        return self._route_affixed_queued_operation(
            "edit_message_caption", args, kwargs, eventual_capable=False, content_key="caption", content_index=3
        )

    @Decorators.retry_on_chat_migration
    def edit_message_media(self, *args, **kwargs):
        return self._route_queued_operation("edit_message_media", args, kwargs, eventual_capable=False)

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
            return self._bot.answer_callback_query(*args, text=truncated, **kwargs)
        self.logger.debug(f"answer_callback_query({args}, {kwargs})")
        return self._bot.answer_callback_query(
            *args, text=prefix + text + suffix, **kwargs
        )

    @Decorators.retry_on_chat_migration
    def get_chat_info(self, *args, **kwargs):
        return self._bot.get_chat(*args, **kwargs)

    def create_forum_topic(self, *args, **kwargs) -> ForumTopic:
        return cast(ForumTopic, self._enqueue_main_chat_mutation("create_forum_topic", args, kwargs))

    def edit_forum_topic(self, *args, **kwargs):
        return self._enqueue_main_chat_mutation("edit_forum_topic", args, kwargs)

    def reopen_forum_topic(self, *args, **kwargs) -> bool:
        return cast(bool, self._enqueue_main_chat_mutation("reopen_forum_topic", args, kwargs))

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
        graceful_stop_lock = getattr(self, '_graceful_stop_lock', None)
        if graceful_stop_lock is None:
            graceful_stop_lock = self._graceful_stop_lock = threading.Lock()

        with graceful_stop_lock:
            if getattr(self, '_graceful_stop_complete', False):
                return
            TelegramBotManager._graceful_stop(self)
            self._graceful_stop_complete = True

    def _graceful_stop(self) -> None:
        if not hasattr(self, '_stopping') or not hasattr(self._stopping, 'set'):
            self._stopping = threading.Event()
        self._stopping.set()
        self.logger.info("Starting graceful shutdown...")

        # Log the durable queue depth before setting the shutdown boundary.
        pending_count = 0
        if hasattr(self, '_outbound_queue'):
            pending_count = int(
                self._outbound_queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0]
            )

        if pending_count > 0:
            self.logger.info("Found %d pending queued send tasks", pending_count)

        # Stop the queued send worker first
        self.stop_queued_worker()

        TelegramBotManager._stop_metrics_server(self)

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

    def _stop_metrics_server(self) -> None:
        """Stop the serving metrics thread without joining an unstarted thread."""
        metrics_httpd = getattr(self, '_metrics_httpd', None)
        if metrics_httpd is None:
            return
        self._metrics_httpd = None
        thread = getattr(metrics_httpd, 'thread', None)
        try:
            if thread is not None and thread.is_alive():
                metrics_httpd.shutdown()
        finally:
            metrics_httpd.server_close()
        if thread is not None and thread.is_alive() and thread.ident != threading.get_ident():
            thread.join(timeout=self.SHUTDOWN_JOIN_GRACE)

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
