# coding=utf-8
from __future__ import annotations

import asyncio
import html
import logging
import numbers
import os
import threading
from functools import wraps
from typing import TYPE_CHECKING, Callable, Coroutine, List, Mapping, Optional, ParamSpec, Protocol, TypeAlias, TypeVar, cast

import telegram.constants
import telegram.error
from telegram import File, ForumTopic, InlineKeyboardMarkup, Update, User
from telegram.ext import CallbackContext, MessageHandler, TypeHandler

from .auxiliary_bot import AuxiliaryBot
from .bot_pool import BotPool
from .etm_metrics import Metrics, parse_metrics_config, start_metrics_server
from .locale_mixin import LocaleMixin
from .outbound import (
    QUEUED_OPERATIONS,
    OutboundQueue,
    QueueEnqueueError,
    QueueRequest,
    SendReceipt,
    _strip_private_queue_metadata,
)
from .ptb_compat import Filters
from .rate_limiter import SlidingWindowRateLimiter
from .telegram_runtime import TelegramPollingRuntime, build_telegram_polling_runtime
from .utils import normalize_request_kwargs

if TYPE_CHECKING:
    from . import TelegramChannel

MAX_CALLBACK_QUERY_ANSWER_LENGTH = 200
P = ParamSpec("P")
T = TypeVar("T")
BotMethod: TypeAlias = Callable[..., object]


class SyncBotProtocol(Protocol):
    def __getattr__(self, item: str) -> BotMethod: ...


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

    logger = logging.getLogger(__name__)
    DEFAULT_SEND_WORKER_COUNT = 8
    BLOCKING_SEND_TIMEOUT = 300.0
    BLOCKING_SEND_TARGET_SLAVE_ID = "__blocking__"
    TELEGRAM_RATE_LIMIT_FALLBACK_SECONDS = 60.0
    SHUTDOWN_DRAIN_TIMEOUT = 5.0
    SHUTDOWN_JOIN_GRACE = 1.0

    # Type declarations for instance attributes assigned in __init__
    _bot: SyncBotProtocol
    admins: List[int]
    bot_pool: Optional["BotPool"]
    _stopping: threading.Event
    _cleanup_tls: threading.local
    _aux_recent_use: dict[int, float]

    class Decorators:
        logger = logging.getLogger(__name__)
        _POSITIONAL_CHAT_ID_INDICES = {
            "edit_message_text": 1,
        }

        @classmethod
        def exception_filter(cls, exception: Exception):
            cls.logger.warning("Telegram request failed", extra={"event": "telegram_bot.request_failed", "error_type": type(exception).__name__})
            return isinstance(exception, telegram.error.TimedOut)

        @classmethod
        def retry_on_chat_migration(cls, fn: Callable):
            @wraps(fn)
            def retry_on_chat_migration_wrap(self: "TelegramBotManager", *args, **kwargs):
                try:
                    return fn(self, *args, **kwargs)
                except telegram.error.ChatMigrated as e:
                    if "chat_id" in kwargs:
                        chat_id = kwargs["chat_id"]
                        self.channel.chat_binding.chat_migration_by_id(chat_id, e.new_chat_id)
                        kwargs["chat_id"] = e.new_chat_id
                        return fn(self, *args, **kwargs)
                    else:
                        chat_id_index = cls._POSITIONAL_CHAT_ID_INDICES.get(fn.__name__, 0)
                        chat_id = args[chat_id_index]
                        self.channel.chat_binding.chat_migration_by_id(chat_id, e.new_chat_id)
                        args = (
                            *args[:chat_id_index],
                            e.new_chat_id,
                            *args[chat_id_index + 1 :],
                        )
                        return fn(self, *args, **kwargs)

            return retry_on_chat_migration_wrap

    def __init__(self, channel: "TelegramChannel"):
        self.channel: "TelegramChannel" = channel
        config = self.channel.config
        self._stopping = threading.Event()

        self.telegram_runtime = build_telegram_polling_runtime(
            config,
            channel,
            self.logger,
            channel._telegram_runtime_started,
            channel._telegram_runtime_stopped,
        )
        self._runtime = self.telegram_runtime.async_runtime
        self._async_bot = self.telegram_runtime.async_bot
        self._bot = self.telegram_runtime.bot
        self.application = self.telegram_runtime.application
        self.admins = config["admins"]
        self.dispatcher = self.application

        self._cleanup_tls = threading.local()  # Thread-local for pending cleanup files
        self._aux_recent_use: dict[int, float] = {}  # chat_id -> timestamp of last aux bot use
        self.logger.debug("Rate limiter initialized", extra={"event": "telegram_bot.rate_limiter_initialized"})

        # Initialize auxiliary bot pool
        self.bot_pool: Optional[BotPool] = None
        aux_configs = config.get("auxiliary_bots", [])
        if aux_configs:
            self._init_bot_pool(aux_configs, config, channel)
        self.logger.debug("Bot pool initialization complete", extra={"event": "telegram_bot.pool_initialized"})

        metrics_top_n, metrics_endpoint = parse_metrics_config(config.get("metrics"), self.logger)
        self._metrics = Metrics(namespace="etm")
        channel.db.set_metrics(self._metrics)
        if self.bot_pool:
            for auxiliary in self.bot_pool.bots:
                auxiliary.bind_metrics(self._metrics)
        self._metrics_httpd = None

        self.outbound_queue = OutboundQueue(
            self._bot,
            self.bot_pool,
            SlidingWindowRateLimiter(),
            worker_count=self.DEFAULT_SEND_WORKER_COUNT,
            blocking_timeout=self.BLOCKING_SEND_TIMEOUT,
            shutdown_drain_timeout=self.SHUTDOWN_DRAIN_TIMEOUT,
            shutdown_join_grace=self.SHUTDOWN_JOIN_GRACE,
        )
        self._register_runtime_metric_collectors(metrics_top_n)

        if metrics_endpoint is not None:
            metrics_host, metrics_port = metrics_endpoint
            self._metrics_httpd = start_metrics_server(
                metrics_host,
                metrics_port,
                registry=self._metrics.registry,
            )

        self.outbound_queue.start()
        self.logger.debug("Outbound worker initialized", extra={"event": "telegram_bot.outbound_worker_initialized"})

        self._add_base_dispatchers()
        self.logger.debug("Base dispatchers added", extra={"event": "telegram_bot.dispatchers_initialized"})

    @property
    def me(self) -> Optional[User]:
        return self.telegram_runtime.me

    @me.setter
    def me(self, user: Optional[User]) -> None:
        self.telegram_runtime.me = user

    def as_async_callback(self, callback: Callable[P, T]) -> Callable[P, Coroutine[object, object, T]]:
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await asyncio.to_thread(callback, *args, **kwargs)

        return wrapper

    def _add_base_dispatchers(self):
        whitelist_filter = ~Filters.user(user_id=self.admins)
        self.dispatcher.add_handler(MessageHandler(whitelist_filter, self.as_async_callback(lambda update, context: None)))
        # Register update_locale in a negative group so it runs BEFORE group 0
        # handlers and does NOT block them. PTB 22 runs one matching handler
        # per group; group 0 is the default and is where commands live.
        self.dispatcher.add_handler(
            TypeHandler(Update, self.as_async_callback(self.channel.update_locale)),
            group=-1,
        )

    @staticmethod
    def _normalize_telegram_chat_id(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, numbers.Integral):
            raise QueueEnqueueError("chat_id must be a non-Boolean integral value.")
        return int(value)

    @staticmethod
    def _queued_chat_id_argument(operation: str, args: tuple, kwargs: Mapping[str, object]) -> object:
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
            queued_args[content_index] = self._affix_queued_content(content, prefix, suffix, queued_kwargs.get("parse_mode", ""))
        else:
            content = queued_kwargs.get(content_key, "")
            queued_kwargs[content_key] = self._affix_queued_content(content, prefix, suffix, queued_kwargs.get("parse_mode", ""))
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
        receipt = self.outbound_queue.enqueue_and_wait(
            QueueRequest(
                operation,
                args,
                queued_kwargs,
                normalized_chat_id,
                str(slave_id) if slave_id else None,
                required_sender_bot_id,
            )
        )
        for path in cleanup_files:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        return receipt

    def _call_direct_operation(self, operation: str, args: tuple, kwargs: Mapping[str, object]) -> object:
        return getattr(self._bot, operation)(*args, **_strip_private_queue_metadata(kwargs))

    def _register_runtime_metric_collectors(self, top_n: int) -> None:
        self._metrics.register_outbound_queue_collectors(self.outbound_queue, top_n)
        self._metrics.register_auxiliary_count_collector(self._auxiliary_count_snapshot)
        self._metrics.register_membership_cache_collector(self._membership_cache_snapshot)
        self._metrics.register_rate_limit_occupancy_collector(self._rate_limit_occupancy_snapshot)

    def _auxiliary_count_snapshot(self) -> dict[str, int]:
        if not self.bot_pool:
            return {"enabled": 0, "disabled": 0}
        return self.bot_pool.auxiliary_count_snapshot()

    def _membership_cache_snapshot(self) -> dict[str, int]:
        if not self.bot_pool:
            return {"member": 0, "not_member": 0, "unknown_probe_pending": 0}
        return self.bot_pool.membership_cache_snapshot()

    def _rate_limit_occupancy_snapshot(self) -> dict[str, float]:
        return self.outbound_queue.rate_limit_occupancy_snapshot()

    def _init_bot_pool(self, aux_configs: list, config: dict, channel: "TelegramChannel"):
        """Initialize the auxiliary bot pool from config."""
        req_kwargs = {
            "read_timeout": 15.0,
            "connection_pool_size": TelegramPollingRuntime._default_connection_pool_size(config),
        }
        conf_req_kwargs = config.get("request_kwargs")
        if isinstance(conf_req_kwargs, Mapping):
            req_kwargs.update(conf_req_kwargs)
        request_kwargs = normalize_request_kwargs(req_kwargs)

        main_token = config["token"]
        seen_tokens = {main_token}
        aux_bots: list = []

        for entry in aux_configs:
            if not isinstance(entry, dict) or not isinstance(entry.get("token"), str):
                self.logger.warning("Skipping invalid auxiliary bot configuration", extra={"event": "telegram_bot.auxiliary_configuration_invalid", "entry_type": type(entry).__name__})
                continue
            token = entry["token"]
            if token in seen_tokens:
                self.logger.warning("Skipping duplicate auxiliary bot", extra={"event": "telegram_bot.auxiliary_duplicate"})
                continue
            seen_tokens.add(token)

            aux_bot = AuxiliaryBot(
                token=token,
                request_kwargs=request_kwargs,
                base_url=channel.flag("api_base_url") or None,
                base_file_url=channel.flag("api_base_file_url") or None,
                local_mode=bool(channel.flag("local_tdlib_api")),
            )
            aux_bot.bind_runtime(self._runtime)
            if aux_bot.initialize():
                aux_bots.append(aux_bot)
            else:
                self.logger.error("Skipping unavailable auxiliary bot", extra={"event": "telegram_bot.auxiliary_unavailable"})

        if aux_bots:
            self.bot_pool = BotPool(aux_bots)
            self.logger.info("Initialized auxiliary bot pool", extra={"event": "telegram_bot.auxiliary_initialized", "bot_count": len(aux_bots)})

    def _enqueue_blocking_api_operation(
        self,
        *,
        target_chat_id: int,
        operation: str,
        args: tuple,
        kwargs: Mapping[str, object],
        required_sender_bot_id: Optional[str],
    ) -> object:
        return self.outbound_queue.enqueue_and_wait(
            QueueRequest(
                operation,
                args,
                dict(kwargs),
                target_chat_id,
                required_sender_bot_id=required_sender_bot_id,
            )
        )

    def _enqueue_main_chat_mutation(self, operation: str, args: tuple, kwargs: Mapping[str, object]) -> object:
        telegram_kwargs = _strip_private_queue_metadata(kwargs)
        target_chat_id = self._normalize_telegram_chat_id(self._queued_chat_id_argument(operation, args, telegram_kwargs))
        return self._enqueue_blocking_api_operation(
            target_chat_id=target_chat_id,
            operation=operation,
            args=args,
            kwargs=telegram_kwargs,
            required_sender_bot_id="__main__",
        )

    @Decorators.retry_on_chat_migration
    def send_message(self, *args, prefix: str = "", suffix: str = "", **kwargs):
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
            "send_message",
            args,
            kwargs,
            eventual_capable=True,
            content_key="text",
            content_index=1,
            prefix=prefix,
            suffix=suffix,
        )

    @Decorators.retry_on_chat_migration
    def edit_message_text(self, *args, prefix="", suffix="", **kwargs):
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
            "edit_message_text",
            args,
            kwargs,
            eventual_capable=False,
            content_key="text",
            content_index=0,
            prefix=prefix,
            suffix=suffix,
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
        return self._route_affixed_queued_operation("send_audio", args, kwargs, eventual_capable=True, content_key="caption", content_index=2)

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
        return self._route_affixed_queued_operation("send_voice", args, kwargs, eventual_capable=True, content_key="caption", content_index=2)

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
        return self._route_affixed_queued_operation("send_video", args, kwargs, eventual_capable=True, content_key="caption", content_index=2)

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
        return self._route_affixed_queued_operation("send_document", args, kwargs, eventual_capable=True, content_key="caption", content_index=2)

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
        return self._route_affixed_queued_operation("send_animation", args, kwargs, eventual_capable=True, content_key="caption", content_index=2)

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
        return self._route_affixed_queued_operation("send_photo", args, kwargs, eventual_capable=True, content_key="caption", content_index=2)

    @Decorators.retry_on_chat_migration
    def send_media_group(self, *args, **kwargs):
        return self._route_queued_operation("send_media_group", args, kwargs, eventual_capable=True)

    @Decorators.retry_on_chat_migration
    def send_chat_action(self, *args, **kwargs):
        queued_kwargs = dict(kwargs)
        message_thread_id = queued_kwargs.pop("message_thread_id", None)
        if message_thread_id is not None:
            api_kwargs = dict(cast(Mapping[str, object], queued_kwargs.get("api_kwargs", {})))
            api_kwargs["message_thread_id"] = message_thread_id
            queued_kwargs["api_kwargs"] = api_kwargs
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
        self.edit_message_text(text=self._("Session expired. Please try again. (SE01)"), chat_id=update.effective_chat.id, message_id=update.effective_message.message_id)

    @Decorators.retry_on_chat_migration
    def edit_message_caption(self, *args, **kwargs):
        return self._route_affixed_queued_operation("edit_message_caption", args, kwargs, eventual_capable=False, content_key="caption", content_index=3)

    @Decorators.retry_on_chat_migration
    def edit_message_media(self, *args, **kwargs):
        return self._route_queued_operation("edit_message_media", args, kwargs, eventual_capable=False)

    def reply_error(self, update, errmsg):
        """
        A wrap that quote-reply a message with error details.

        Returns:
            telegram.Message: Message sent
        """
        return self.send_message(update.effective_chat.id, errmsg, reply_to_message_id=update.effective_message.message_id)

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
    def answer_callback_query(self, *args, prefix="", suffix="", text=None, message_id=None, **kwargs):
        kwargs.pop("chat_id", None)
        if text is None:
            return self._bot.answer_callback_query(*args, **kwargs)
        prefix = (prefix and (prefix + "\n")) or prefix
        suffix = (suffix and ("\n" + suffix)) or suffix

        if len(prefix + text + suffix) >= MAX_CALLBACK_QUERY_ANSWER_LENGTH:
            full_message = prefix + text + suffix
            keep_size = MAX_CALLBACK_QUERY_ANSWER_LENGTH // 3
            truncated = full_message[:keep_size] + "..." + full_message[-keep_size:]
            return self._bot.answer_callback_query(*args, text=truncated, **kwargs)
        return self._bot.answer_callback_query(*args, text=prefix + text + suffix, **kwargs)

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

    def stop_channel_resources(self) -> None:
        """Stop resources owned by message delivery rather than PTB polling."""
        self._stopping.set()
        self.logger.info("Stopping Telegram delivery resources", extra={"event": "telegram_bot.stop_started"})
        self.outbound_queue.stop()
        self._stop_metrics_server()
        if self.bot_pool:
            self.bot_pool.shutdown()
        self.logger.info("Stopped Telegram delivery resources", extra={"event": "telegram_bot.stop_completed"})

    def _stop_metrics_server(self) -> None:
        """Stop the serving metrics thread without joining an unstarted thread."""
        metrics_httpd = getattr(self, "_metrics_httpd", None)
        if metrics_httpd is None:
            return
        self._metrics_httpd = None
        thread = getattr(metrics_httpd, "thread", None)
        try:
            if thread is not None and thread.is_alive():
                metrics_httpd.shutdown()
        finally:
            metrics_httpd.server_close()
        if thread is not None and thread.is_alive() and thread.ident != threading.get_ident():
            thread.join(timeout=self.SHUTDOWN_JOIN_GRACE)
