# coding=utf-8
from __future__ import annotations

import asyncio
import collections
import collections.abc
import html
import io
import logging
import numbers
import os
import re
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace
from typing import TYPE_CHECKING, BinaryIO, Callable, Collection, Coroutine, List, Literal, Mapping, NamedTuple, Optional, ParamSpec, Protocol, TypeAlias, Tuple, TypeVar, cast
from urllib.parse import quote, urlparse, urlunparse
from urllib.request import url2pathname
from unittest.mock import patch

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
from .mtproto import MTProtoRetryableError
from .outbound import (
    OutboundQueueScheduler,
    QUEUED_OPERATIONS,
    QueueEnqueueError,
    QueueRequest,
    SchedulerStoppedError,
    SenderSelection,
    SenderSelectionResult,
    QueuedCall,
    retry_after_seconds,
)
from .ptb_compat import Filters
from .rate_limiter import SlidingWindowRateLimiter
from .utils import TelegramChatID, TelegramMessageID, message_id_to_str
from .telegram_runtime import AsyncTelegramRuntime, SyncBotFacade


BotChatKey: TypeAlias = Tuple[Optional[str], int]


if TYPE_CHECKING:
    from . import TelegramChannel
    from .message import ETMMsg

MAX_CALLBACK_QUERY_ANSWER_LENGTH = 200
P = ParamSpec("P")
T = TypeVar("T")
BotMethod: TypeAlias = Callable[..., object]
_INTERNAL_KWARGS = frozenset({
    'prefix',
    'suffix',
    '_sender_bot_id',
    '_slave_id',
    '_force_main_bot',
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


@dataclass
class SendReceipt:
    message: object
    sender_bot_id: Optional[str] = None

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
        }

        @classmethod
        def exception_filter(cls, exception: Exception):
            cls.logger.warning("Telegram request failed (%s).", type(exception).__name__)
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
        self._last_metrics_snapshot = 0.0
        from .etm_metrics import Metrics, start_metrics_server
        metrics_top_n, metrics_endpoint = self._parse_metrics_config(config.get('metrics'), self.logger)
        self._metrics = Metrics(namespace="etm")
        channel.db.set_metrics(self._metrics)
        if self.bot_pool:
            for auxiliary in self.bot_pool.bots:
                auxiliary.bind_metrics(self._metrics)
        self._metrics_httpd = None

        self._send_worker_count = self.DEFAULT_SEND_WORKER_COUNT
        self._send_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=self._send_worker_count, thread_name_prefix="ETM-send",
        )
        self._outbound_finalization_lock = threading.Lock()
        self._outbound_resources_finalized = False
        self._outbound_scheduler = OutboundQueueScheduler(
            self._send_executor,
            worker_count=self._send_worker_count,
            select_sender=self.select_sender,
            acquire_sender_limits=self.acquire_sender_limits,
            execute_call=self.execute_queued_call,
            record_retry_after=self.record_retry_after,
        )
        self._register_runtime_metric_collectors(metrics_top_n)

        if metrics_endpoint is not None:
            metrics_host, metrics_port = metrics_endpoint
            self._metrics_httpd = start_metrics_server(
                metrics_host,
                metrics_port,
                registry=self._metrics.registry,
            )

        self._send_worker_thread = threading.Thread(
            target=self._queued_send_worker,
            name="ETM queued send worker",
            daemon=True
        )
        self._send_worker_thread.start()
        self.logger.debug("Outbound worker initialized...")

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
        self._shutdown_complete_event.clear()
        mtproto = self.channel.mtproto
        if not getattr(mtproto, "enabled", False):
            return
        try:
            await mtproto.connect()
        except (ConnectionError, TimeoutError, OSError, MTProtoRetryableError) as error:
            self.logger.warning(
                "MTProto startup is unavailable; MsgLog ingestion remains pending (%s).",
                type(error).__name__,
            )
            return
        if not getattr(mtproto, "connected", False):
            self.logger.warning("MTProto startup did not establish a connection; MsgLog ingestion remains pending.")
            return
        chat_binding = getattr(self.channel, "chat_binding", None)
        if chat_binding is not None:
            chat_binding.resume_pending_msglog_ingestions()


    async def _post_shutdown(self, application: Application):
        try:
            await self.channel.mtproto.disconnect()
        finally:
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
    ) -> SendReceipt:
        return SendReceipt(
            message=message,
            sender_bot_id=sender_bot_id,
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
        chat_id_index = 1 if operation == "edit_message_text" else 0
        return args[chat_id_index] if len(args) > chat_id_index else kwargs.get("chat_id")

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
        return self._route_queued_operation(operation, tuple(queued_args), queued_kwargs, eventual_capable=eventual_capable)

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
        force_main_bot = queued_kwargs.pop("_force_main_bot", False)
        chat_id = self._queued_chat_id_argument(operation, args, queued_kwargs)
        normalized_chat_id = self._normalize_telegram_chat_id(chat_id)
        has_callback = _has_callback_keyboard(queued_kwargs.get("reply_markup"))
        cleanup_tls = getattr(self, "_cleanup_tls", None)
        cleanup_files = getattr(cleanup_tls, "pending_cleanup", [])[:]
        if cleanup_tls is not None:
            cleanup_tls.pending_cleanup = []

        required_sender_bot_id = str(sender_bot_id) if sender_bot_id and not eventual_capable else None
        if force_main_bot or (eventual_capable and has_callback) or not eventual_capable and required_sender_bot_id is None:
            required_sender_bot_id = "__main__"
        receipt = self._enqueue_and_wait(QueueRequest(
            operation, args, queued_kwargs, normalized_chat_id,
            str(slave_id) if slave_id else None, required_sender_bot_id,
        ))
        for path in cleanup_files:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        return receipt

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
            logger.warning(
                "Invalid metrics config type %s; Prometheus endpoint disabled.",
                type(metrics_cfg).__name__,
            )
            return top_n, None

        try:
            parsed_top_n = int(metrics_cfg.get('top_n', top_n))
            if parsed_top_n < 0:
                raise ValueError
            top_n = parsed_top_n
        except (TypeError, ValueError):
            logger.warning("Invalid metrics top_n type %s; using default %d.", type(metrics_cfg.get('top_n')).__name__, top_n)

        host = metrics_cfg.get('host', '127.0.0.1')
        if not isinstance(host, str) or not host:
            logger.warning("Invalid metrics host type %s; Prometheus endpoint disabled.", type(host).__name__)
            return top_n, None

        try:
            port = int(metrics_cfg.get('port', 9101))
            if not 0 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            logger.warning(
                "Invalid metrics port type %s; Prometheus endpoint disabled.",
                type(metrics_cfg.get('port')).__name__,
            )
            return top_n, None

        return top_n, (host, port)

    def _register_runtime_metric_collectors(self, top_n: int) -> None:
        """Bind bounded scrape callbacks after all outbound runtime state exists."""
        from .etm_metrics import DestinationQueueSnapshot, WorkerSnapshot

        def destination_snapshot() -> list[DestinationQueueSnapshot]:
            return [
                DestinationQueueSnapshot(destination, depth, oldest_age)
                for destination, depth, oldest_age in self._outbound_scheduler.destination_snapshot()
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
                self.logger.warning("Invalid auxiliary_bots entry type %s; skipping.", type(entry).__name__)
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

    def _enqueue_and_wait(self, request: QueueRequest) -> SendReceipt:
        waiter = self._outbound_scheduler.enqueue(request)
        try:
            return cast(SendReceipt, waiter.result(timeout=self.BLOCKING_SEND_TIMEOUT))
        except FutureTimeoutError as error:
            raise RuntimeError(
                f"Telegram call to chat {request.telegram_chat_id} timed out after {self.BLOCKING_SEND_TIMEOUT:g}s"
            ) from error

    def enqueue_history_operation(
        self, *, operation: str, args: tuple, kwargs: Mapping[str, object]
    ) -> Future:
        chat_id = self._normalize_telegram_chat_id(self._queued_chat_id_argument(operation, args, kwargs))
        return self._outbound_scheduler.enqueue(
            QueueRequest(operation, args, dict(kwargs), chat_id)
        )

    def _enqueue_blocking_api_operation(
        self,
        *,
        target_chat_id: int,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
        required_sender_bot_id: Optional[str],
    ) -> object:
        return self._enqueue_and_wait(QueueRequest(
            operation, args, dict(kwargs), target_chat_id,
            required_sender_bot_id=required_sender_bot_id,
        ))

    def _enqueue_main_chat_mutation(
        self, operation: str, args: tuple, kwargs: Mapping[str, object]
    ) -> object:
        telegram_kwargs = self._strip_private_queue_metadata(kwargs)
        target_chat_id = self._normalize_telegram_chat_id(
            self._queued_chat_id_argument(operation, args, telegram_kwargs)
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

    def execute_queued_call(self, row: QueuedCall, selection: SenderSelection) -> SendReceipt:
        sender = cast(SyncBotProtocol, selection.sender)
        method = getattr(sender, row.operation)
        telegram_kwargs = self._strip_private_queue_metadata(row.kwargs)
        telegram_args = row.args
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
            result = method(*telegram_args, **telegram_kwargs)
        except telegram.error.BadRequest as error:
            if not error.message.lower().startswith("can't parse entities") or "parse_mode" not in telegram_kwargs:
                raise
            telegram_kwargs.pop("parse_mode")
            self._rewind_queued_files(telegram_args, telegram_kwargs)
            result = method(*telegram_args, **telegram_kwargs)
        if attachment is None or content_key is None:
            if selection.sender_bot_id is not None and self.bot_pool and row.slave_id:
                self.bot_pool.record_successful_auxiliary_send(row.slave_id, selection.sender_bot_id)
            return self._make_send_receipt(result, selection.sender_bot_id)
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
        if selection.sender_bot_id is not None and self.bot_pool and row.slave_id:
            self.bot_pool.record_successful_auxiliary_send(row.slave_id, selection.sender_bot_id)
        return self._make_send_receipt(result, selection.sender_bot_id)

    def record_retry_after(
        self, row: QueuedCall, error: telegram.error.RetryAfter, selection: SenderSelection
    ) -> None:
        with self._get_bot_chat_state_lock():
            self._bot_chat_disabled_until[(selection.sender_bot_id, row.telegram_chat_id)] = (
                time.monotonic() + retry_after_seconds(error)
            )

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

    def _run_database_update_callback(self, on_complete: Optional[Callable[[], None]]):
        if on_complete is None:
            return
        try:
            on_complete()
        except Exception as e:
            self.logger.warning("Database update completion callback failed (%s).", type(e).__name__)

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
                "DB write failed for Telegram message %s; dropping mapping (%s).",
                getattr(real_tg_msg, 'message_id', '?'),
                type(e).__name__,
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
            self.logger.debug("Waiting for outbound worker to stop...")
            worker_thread.join(
                timeout=self.SHUTDOWN_DRAIN_TIMEOUT + self.SHUTDOWN_JOIN_GRACE
            )

            if worker_thread.is_alive():
                self.logger.warning("Outbound worker did not stop within timeout")
                return
        self._finalize_outbound_resources()
        self.logger.debug("Outbound worker stopped")

    def _finalize_outbound_resources(self) -> None:
        """Close caller-owned scheduler resources after the worker has exited."""
        with self._outbound_finalization_lock:
            if self._outbound_resources_finalized:
                return
            self._outbound_resources_finalized = True
            self._send_executor.shutdown(wait=False)

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
                        self.logger.debug("Telegram application loop not ready for stop_running() (%s).", type(exc).__name__)
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
                        self.logger.warning("PTB shutdown coroutine did not complete cleanly (%s).", type(exc).__name__)

                if not application_stopped:
                    stop_requested = False
                    if hasattr(self, '_runtime'):
                        stop_requested = self._runtime.call_soon(self.application.stop_running)
                    if not stop_requested:
                        try:
                            self.application.stop_running()
                        except RuntimeError as exc:
                            self.logger.debug("Telegram application loop not ready for stop_running() (%s).", type(exc).__name__)

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
