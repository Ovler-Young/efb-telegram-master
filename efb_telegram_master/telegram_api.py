"""Synchronous Telegram Bot API policy for ETM delivery."""

from __future__ import annotations

import html
import numbers
import threading
import time
from collections.abc import Callable, Mapping
from functools import wraps
from typing import TYPE_CHECKING, Protocol, cast

import telegram.error
from telegram import File, ForumTopic, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext

from .auxiliary_bot import MembershipProbeShutdownTimeout
from .bot_pool import BotPool
from .outbound import OutboundQueue
from .outbound_types import QueueEnqueueError, QueueRequest, SchedulerStoppedError, SendReceipt, UploadCleanup, cleanup_upload_paths, rewind_uploads
from .telegram_calls import QUEUED_OPERATIONS, stripped_telegram_kwargs

if TYPE_CHECKING:
    from . import TelegramChannel
    from .metrics_runtime import MetricsServer

MAX_CALLBACK_QUERY_ANSWER_LENGTH = 200


class MetricsServerShutdownTimeout(RuntimeError):
    """The metrics serving thread remained alive after its shutdown deadline."""


class SyncBotProtocol(Protocol):
    def __getattr__(self, item: str) -> Callable[..., object]: ...


def _has_callback_keyboard(reply_markup: object) -> bool:
    if not isinstance(reply_markup, InlineKeyboardMarkup):
        return False
    return any(button.callback_data and button.callback_data != "void" for row in reply_markup.inline_keyboard for button in row)


class TelegramAPI:
    """Route public Bot API operations through ETM's sender and queue policy."""

    _POSITIONAL_CHAT_ID_INDICES = {"edit_message_text": 1}

    def __init__(
        self,
        channel: "TelegramChannel",
        bot: SyncBotProtocol,
        outbound_queue: OutboundQueue,
        bot_pool: BotPool | None,
    ) -> None:
        self._channel = channel
        self._bot = bot
        self._outbound_queue = outbound_queue
        self.bot_pool = bot_pool
        self._cleanup_tls = threading.local()
        self._metrics_server: MetricsServer | None = None
        self._delivery_stop_lock = threading.Lock()
        self._outbound_stopped = False
        self._membership_shutdown_started = False
        self._delivery_resources_stopped = False

    def bind_metrics_server(self, metrics_server: "MetricsServer | None") -> None:
        self._metrics_server = metrics_server

    def begin_delivery_shutdown(self, deadline: float) -> tuple[BaseException, ...]:
        """Stop outbound work and cancel membership probes before runtime teardown."""
        with self._delivery_stop_lock:
            outbound_stopped = self._outbound_stopped
            metrics_server = self._metrics_server
            membership_shutdown_started = self._membership_shutdown_started
            bot_pool = self.bot_pool
        errors: list[BaseException] = []
        if not outbound_stopped:
            try:
                self._outbound_queue.stop(deadline)
            except BaseException as error:
                errors.append(error)
            else:
                with self._delivery_stop_lock:
                    self._outbound_stopped = True
        if metrics_server is not None:
            try:
                metrics_server.stop(max(0.0, deadline - time.monotonic()))
                thread = getattr(metrics_server, "thread", None)
                if thread is not None and thread.is_alive():
                    raise MetricsServerShutdownTimeout("Metrics server did not stop before the shutdown deadline.")
            except BaseException as error:
                errors.append(error)
            else:
                with self._delivery_stop_lock:
                    if self._metrics_server is metrics_server:
                        self._metrics_server = None
        if bot_pool and not membership_shutdown_started:
            try:
                bot_pool.begin_shutdown()
            except BaseException as error:
                errors.append(error)
            else:
                with self._delivery_stop_lock:
                    self._membership_shutdown_started = True
        return tuple(errors)

    def finish_delivery_shutdown(self, deadline: float) -> tuple[BaseException, ...]:
        """Join membership probe workers after the runtime has cancelled their calls."""
        with self._delivery_stop_lock:
            if self._delivery_resources_stopped:
                return ()
            bot_pool = self.bot_pool
            membership_shutdown_started = self._membership_shutdown_started
        errors: list[BaseException] = []
        if bot_pool and membership_shutdown_started:
            try:
                incomplete = bot_pool.wait_for_shutdown(deadline)
                if incomplete:
                    joined = ", ".join(map(str, incomplete))
                    errors.append(MembershipProbeShutdownTimeout(f"Auxiliary membership probes did not stop before the final deadline for bot IDs: {joined}"))
            except BaseException as error:
                errors.append(error)
        with self._delivery_stop_lock:
            delivery_complete = self._outbound_stopped and self._metrics_server is None
        if not errors and delivery_complete:
            with self._delivery_stop_lock:
                self._delivery_resources_stopped = True
        return tuple(errors)

    def stop_delivery_resources(self, deadline: float) -> tuple[BaseException, ...]:
        """Stop delivery resources without a polling runtime owner by one deadline."""
        errors = list(self.begin_delivery_shutdown(deadline))
        errors.extend(self.finish_delivery_shutdown(deadline))
        return tuple(errors)

    @staticmethod
    def _normalize_telegram_chat_id(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, numbers.Integral):
            raise QueueEnqueueError("chat_id must be a non-Boolean integral value.")
        return int(value)

    @staticmethod
    def _queued_chat_id_argument(operation: str, args: tuple[object, ...], kwargs: Mapping[str, object]) -> object:
        chat_id_index = 1 if operation == "edit_message_text" else 0
        return args[chat_id_index] if len(args) > chat_id_index else kwargs.get("chat_id")

    @staticmethod
    def _affix_content(content: object, prefix: object, suffix: object, parse_mode: object) -> str:
        prefix_text = f"{prefix}\n" if prefix else ""
        suffix_text = f"\n{suffix}" if suffix else ""
        if str(parse_mode).lower() == "html":
            prefix_text, suffix_text = html.escape(prefix_text), html.escape(suffix_text)
        return prefix_text + str(content) + suffix_text

    @staticmethod
    def _retry_on_chat_migration(function):
        @wraps(function)
        def wrapper(self: "TelegramAPI", *args: object, **kwargs: object) -> object:
            try:
                return function(self, *args, **kwargs)
            except telegram.error.ChatMigrated as error:
                try:
                    rewind_uploads(args, kwargs)
                except BaseException:
                    cleanup_upload_paths(self._claim_pending_upload_cleanup())
                    raise
                if "chat_id" in kwargs:
                    chat_id = kwargs["chat_id"]
                    self._channel.topic_sync.migrate_chat_associations(cast(int, chat_id), error.new_chat_id)
                    kwargs["chat_id"] = error.new_chat_id
                    return function(self, *args, **kwargs)
                index = TelegramAPI._POSITIONAL_CHAT_ID_INDICES.get(function.__name__, 0)
                chat_id = args[index]
                self._channel.topic_sync.migrate_chat_associations(cast(int, chat_id), error.new_chat_id)
                migrated_args = (*args[:index], error.new_chat_id, *args[index + 1 :])
                return function(self, *migrated_args, **kwargs)

        return wrapper

    def _route_affixed_operation(
        self,
        operation: str,
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
        *,
        eventual_capable: bool,
        content_key: str,
        content_index: int,
        prefix: object = "",
        suffix: object = "",
    ) -> SendReceipt:
        queued_kwargs = dict(kwargs)
        prefix, suffix = queued_kwargs.pop("prefix", prefix), queued_kwargs.pop("suffix", suffix)
        queued_args = list(args)
        if len(queued_args) > content_index:
            queued_args[content_index] = self._affix_content(queued_args[content_index], prefix, suffix, queued_kwargs.get("parse_mode", ""))
        else:
            queued_kwargs[content_key] = self._affix_content(queued_kwargs.get(content_key, ""), prefix, suffix, queued_kwargs.get("parse_mode", ""))
        return self._route_queued_operation(operation, tuple(queued_args), queued_kwargs, eventual_capable=eventual_capable)

    def _route_queued_operation(self, operation: str, args: tuple[object, ...], kwargs: Mapping[str, object], *, eventual_capable: bool) -> SendReceipt:
        if operation not in QUEUED_OPERATIONS:
            raise QueueEnqueueError(f"Unsupported queued operation: {operation}")
        queued_kwargs = dict(kwargs)
        sender_bot_id = queued_kwargs.pop("_sender_bot_id", None)
        slave_id = queued_kwargs.pop("_slave_id", None)
        force_main_bot = queued_kwargs.pop("_force_main_bot", False)
        normalized_chat_id = self._normalize_telegram_chat_id(self._queued_chat_id_argument(operation, args, queued_kwargs))
        required_sender_bot_id = str(sender_bot_id) if sender_bot_id and not eventual_capable else None
        if force_main_bot or (eventual_capable and _has_callback_keyboard(queued_kwargs.get("reply_markup"))) or (not eventual_capable and required_sender_bot_id is None):
            required_sender_bot_id = "__main__"
        return self._enqueue_request(
            QueueRequest(operation, args, queued_kwargs, normalized_chat_id, str(slave_id) if slave_id else None, required_sender_bot_id, self._claim_pending_upload_cleanup())
        )

    def _claim_pending_upload_cleanup(self) -> UploadCleanup:
        cleanup = UploadCleanup(tuple(getattr(self._cleanup_tls, "pending_cleanup", [])))
        self._cleanup_tls.pending_cleanup = []
        return cleanup

    def register_upload_cleanup(self, path: str) -> None:
        """Attach a locally created upload path to the next queued request."""
        pending = getattr(self._cleanup_tls, "pending_cleanup", [])
        self._cleanup_tls.pending_cleanup = [*pending, path]

    def _enqueue_request(self, request: QueueRequest) -> SendReceipt:
        try:
            return self._outbound_queue.enqueue_and_wait(request)
        except telegram.error.ChatMigrated:
            self._cleanup_tls.pending_cleanup = [*getattr(self._cleanup_tls, "pending_cleanup", []), *request.cleanup.paths]
            raise
        except (QueueEnqueueError, SchedulerStoppedError):
            cleanup_upload_paths(request.cleanup)
            raise

    def _call_direct_operation(self, operation: str, args: tuple[object, ...], kwargs: Mapping[str, object]) -> object:
        return getattr(self._bot, operation)(*args, **stripped_telegram_kwargs(kwargs))

    def _enqueue_main_chat_mutation(self, operation: str, args: tuple[object, ...], kwargs: Mapping[str, object]) -> SendReceipt:
        telegram_kwargs = stripped_telegram_kwargs(kwargs)
        target_chat_id = self._normalize_telegram_chat_id(self._queued_chat_id_argument(operation, args, telegram_kwargs))
        return self._enqueue_request(QueueRequest(operation, args, dict(telegram_kwargs), target_chat_id, required_sender_bot_id="__main__", cleanup=self._claim_pending_upload_cleanup()))

    @_retry_on_chat_migration
    def send_message(self, *args: object, prefix: str = "", suffix: str = "", **kwargs: object) -> SendReceipt:
        return self._route_affixed_operation("send_message", args, kwargs, eventual_capable=True, content_key="text", content_index=1, prefix=prefix, suffix=suffix)

    @_retry_on_chat_migration
    def edit_message_text(self, *args: object, prefix: str = "", suffix: str = "", **kwargs: object) -> SendReceipt:
        return self._route_affixed_operation("edit_message_text", args, kwargs, eventual_capable=False, content_key="text", content_index=0, prefix=prefix, suffix=suffix)

    def _send_captioned(self, operation: str, args: tuple[object, ...], kwargs: Mapping[str, object]) -> SendReceipt:
        return self._route_affixed_operation(operation, args, kwargs, eventual_capable=True, content_key="caption", content_index=2)

    @_retry_on_chat_migration
    def send_audio(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._send_captioned("send_audio", args, kwargs)

    @_retry_on_chat_migration
    def send_voice(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._send_captioned("send_voice", args, kwargs)

    @_retry_on_chat_migration
    def send_video(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._send_captioned("send_video", args, kwargs)

    @_retry_on_chat_migration
    def send_document(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._send_captioned("send_document", args, kwargs)

    @_retry_on_chat_migration
    def send_animation(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._send_captioned("send_animation", args, kwargs)

    @_retry_on_chat_migration
    def send_photo(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._send_captioned("send_photo", args, kwargs)

    @_retry_on_chat_migration
    def send_chat_action(self, *args: object, **kwargs: object):
        queued_kwargs = dict(kwargs)
        thread_id = queued_kwargs.pop("message_thread_id", None)
        if thread_id is not None:
            api_kwargs = dict(cast(Mapping[str, object], queued_kwargs.get("api_kwargs", {})))
            api_kwargs["message_thread_id"] = thread_id
            queued_kwargs["api_kwargs"] = api_kwargs
        return self._call_direct_operation("send_chat_action", args, queued_kwargs)

    @_retry_on_chat_migration
    def edit_message_reply_markup(self, *args: object, **kwargs: object):
        if (args and args[0] is None) or (not args and kwargs.get("chat_id") is None):
            return self._call_direct_operation("edit_message_reply_markup", args, kwargs)
        return self._enqueue_main_chat_mutation("edit_message_reply_markup", args, kwargs)

    @_retry_on_chat_migration
    def send_location(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._enqueue_main_chat_mutation("send_location", args, kwargs)

    @_retry_on_chat_migration
    def send_venue(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._enqueue_main_chat_mutation("send_venue", args, kwargs)

    @_retry_on_chat_migration
    def send_sticker(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._route_queued_operation("send_sticker", args, kwargs, eventual_capable=True)

    @_retry_on_chat_migration
    def send_media_group(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._route_queued_operation("send_media_group", args, kwargs, eventual_capable=True)

    @_retry_on_chat_migration
    def forward_message(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._route_queued_operation("forward_message", args, kwargs, eventual_capable=True)

    @_retry_on_chat_migration
    def copy_message(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._route_queued_operation("copy_message", args, kwargs, eventual_capable=True)

    @_retry_on_chat_migration
    def edit_message_caption(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._route_affixed_operation("edit_message_caption", args, kwargs, eventual_capable=False, content_key="caption", content_index=3)

    @_retry_on_chat_migration
    def edit_message_media(self, *args: object, **kwargs: object) -> SendReceipt:
        return self._route_queued_operation("edit_message_media", args, kwargs, eventual_capable=False)

    def get_me(self, *args: object, **kwargs: object):
        return self._call_direct_operation("get_me", args, kwargs)

    @_retry_on_chat_migration
    def get_file(self, file_id: str):
        return cast(File, self._bot.get_file(file_id))

    @_retry_on_chat_migration
    def get_chat_info(self, *args: object, **kwargs: object):
        return self._call_direct_operation("get_chat", args, kwargs)

    def delete_message(self, chat_id: int, message_id: int, _sender_bot_id: object = None) -> SendReceipt:
        required_sender = str(_sender_bot_id) if _sender_bot_id else "__main__"
        return self._enqueue_request(
            QueueRequest("delete_message", (chat_id, message_id), {}, self._normalize_telegram_chat_id(chat_id), required_sender_bot_id=required_sender, cleanup=self._claim_pending_upload_cleanup())
        )

    @_retry_on_chat_migration
    def answer_callback_query(self, *args: object, prefix: str = "", suffix: str = "", text: str | None = None, **kwargs: object):
        kwargs.pop("chat_id", None)
        kwargs.pop("message_id", None)
        if text is None:
            return self._bot.answer_callback_query(*args, **kwargs)
        full_text = f"{prefix + chr(10) if prefix else ''}{text}{chr(10) + suffix if suffix else ''}"
        if len(full_text) >= MAX_CALLBACK_QUERY_ANSWER_LENGTH:
            keep_size = MAX_CALLBACK_QUERY_ANSWER_LENGTH // 3
            full_text = full_text[:keep_size] + "..." + full_text[-keep_size:]
        return self._bot.answer_callback_query(*args, text=full_text, **kwargs)

    def create_forum_topic(self, *args: object, **kwargs: object):
        return cast(ForumTopic, self._enqueue_main_chat_mutation("create_forum_topic", args, kwargs))

    def edit_forum_topic(self, *args: object, **kwargs: object):
        return self._enqueue_main_chat_mutation("edit_forum_topic", args, kwargs)

    def reopen_forum_topic(self, *args: object, **kwargs: object):
        return cast(bool, self._enqueue_main_chat_mutation("reopen_forum_topic", args, kwargs))

    def set_chat_title(self, *args: object, **kwargs: object):
        return self._enqueue_main_chat_mutation("set_chat_title", args, kwargs)

    def set_chat_photo(self, *args: object, **kwargs: object):
        return self._enqueue_main_chat_mutation("set_chat_photo", args, kwargs)

    def pin_chat_message(self, *args: object, **kwargs: object):
        return self._enqueue_main_chat_mutation("pin_chat_message", args, kwargs)

    def set_chat_description(self, *args: object, **kwargs: object):
        return self._enqueue_main_chat_mutation("set_chat_description", args, kwargs)

    def session_expired(self, update: Update, _context: CallbackContext):
        assert update.effective_message and update.effective_chat
        if update.callback_query:
            self.answer_callback_query(update.callback_query.id)
        return self.edit_message_text(text=self._channel._("Session expired. Please try again. (SE01)"), chat_id=update.effective_chat.id, message_id=update.effective_message.message_id)

    def reply_error(self, update: Update, message: str):
        assert update.effective_chat and update.effective_message
        return self.send_message(update.effective_chat.id, message, reply_to_message_id=update.effective_message.message_id)
