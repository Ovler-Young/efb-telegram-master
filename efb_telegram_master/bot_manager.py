# coding=utf-8
import bisect
import collections
import heapq
import html
import io
import logging
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Callable, Deque, List, NamedTuple, Optional, Tuple, cast
from urllib.parse import urlparse
from urllib.request import url2pathname
from unittest.mock import Mock

import telegram.constants
import telegram.error
from retrying import retry
from telegram import File, ForumTopic, InlineKeyboardMarkup, InputFile, Update, User
from telegram import Message as TelegramMessage
from telegram.ext import CallbackContext, Dispatcher, Filters, MessageHandler, Updater

from .auxiliary_bot import AuxiliaryBot
from .bot_pool import BotPool
from .locale_handler import LocaleHandler
from .locale_mixin import LocaleMixin
from .msg_type import get_msg_type


class DelayedTask(NamedTuple):
    """Represents a delayed message task."""
    execute_time: float  # When to execute (timestamp)
    chat_id: int        # Target chat ID
    function: Callable  # Function to execute
    args: tuple        # Function arguments
    kwargs: dict       # Function keyword arguments
    task_id: str       # Unique task identifier
    cleanup_files: list = []  # Files to delete after task completes

if TYPE_CHECKING:
    from . import TelegramChannel

MAX_CALLBACK_QUERY_ANSWER_LENGTH = 200


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
    updater: Updater
    _bot: telegram.Bot
    me: User
    admins: List[int]
    dispatcher: Dispatcher
    bot_pool: Optional['BotPool']
    _delayed_worker_stop: threading.Event
    _delayed_queue_lock: threading.Lock
    _delayed_queue: List[Tuple[float, int, 'DelayedTask']]
    _cleanup_tls: threading.local
    _tls: threading.local
    _rate_limit_lock: threading.Lock
    _global_timestamps: list[float]
    _chat_timestamps: 'defaultdict[int, Deque[float]]'
    _aux_recent_use: dict[int, float]

    @staticmethod
    def _is_html_parse_mode(parse_mode) -> bool:
        return str(parse_mode or '').lower() == "html"

    @classmethod
    def _format_affix(cls, prefix: str = '', suffix: str = '', parse_mode=None) -> Tuple[str, str]:
        if cls._is_html_parse_mode(parse_mode):
            prefix = (prefix and (f"<code>{html.escape(prefix.strip())}</code>\n")) or prefix
            suffix = (suffix and ("\n" + html.escape(suffix.strip()))) or suffix
        else:
            prefix = (prefix and (prefix.rstrip() + "\n")) or prefix
            suffix = (suffix and ("\n" + suffix.lstrip())) or suffix
        return prefix, suffix

    @classmethod
    def _message_file_buffer(cls, text: str, parse_mode=None) -> io.BytesIO:
        if cls._is_html_parse_mode(parse_mode):
            text = (
                "<html><head><meta charset='utf-8'></head>"
                "<body><pre style='white-space:pre-wrap'>" + text + "</pre></body></html>"
            )
        return io.BytesIO(text.encode('utf-8'))

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
            """Apply rate limiting to API calls with two-mode routing for multi-bot pool."""
            @wraps(fn)
            def rate_limit_wrapper(self: 'TelegramBotManager', *args, **kwargs):
                # Bypass: caller already reserved a slot and set _using_bot
                if kwargs.pop('_bypass_rate_limit', False):
                    return fn(self, *args, **kwargs)

                # Pop the _sender_bot_id kwarg (used for forced routing on edits/deletes)
                sender_bot_id = kwargs.pop('_sender_bot_id', None)

                # Extract chat_id from arguments
                chat_id = None
                if args:
                    chat_id = args[0]
                elif 'chat_id' in kwargs:
                    chat_id = kwargs['chat_id']

                # if _delayed_worker_stop is set, we should not schedule new tasks
                if self._delayed_worker_stop.is_set():
                    self.logger.warning(f"Delayed worker is stopped. Not scheduling new tasks for chat {chat_id}.")
                    return None

                # MODE 1: Forced routing (edits/deletes must go to the specific bot that sent the original)
                if sender_bot_id and self.bot_pool:
                    aux_bot = self.bot_pool.get_bot_by_id(sender_bot_id)
                    if aux_bot and not aux_bot.disabled:
                        try:
                            with self._using_bot(aux_bot.bot):
                                result = fn(self, *args, **kwargs)
                            if result and hasattr(result, '__dict__'):
                                result._sender_bot_id = str(sender_bot_id)
                            return result
                        except telegram.error.Unauthorized:
                            aux_bot.mark_disabled("Unauthorized during forced-route API call")
                            self._notify_admin_disabled_bot(aux_bot)
                    # Fallback: sender bot unavailable, try main bot
                    return fn(self, *args, **kwargs)

                # MODE 2: Pool routing (new sends pick best available bot)
                # Only attempt aux bots when the main bot would impose a delay.
                if chat_id and self.bot_pool:
                    # Skip pool for messages with callback keyboards (EC-4)
                    has_callback = _has_callback_keyboard(kwargs.get('reply_markup'))
                    if not has_callback:
                        main_delay, _, _ = self._calculate_rate_limit_delay(chat_id, peek_only=True)
                        slot = self.bot_pool.acquire_send_slot(chat_id, max_delay=main_delay) if main_delay > 0 else None
                        if slot is not None:
                            aux_bot, aux_delay = slot
                            try:
                                if aux_delay > 0:
                                    time.sleep(aux_delay)
                                with self._using_bot(aux_bot.bot):
                                    result = fn(self, *args, **kwargs)
                                if result and hasattr(result, '__dict__'):
                                    result._sender_bot_id = str(aux_bot.bot_id)
                                self._record_aux_use(chat_id)
                                return result
                            except telegram.error.Unauthorized:
                                aux_bot.mark_disabled("Unauthorized during pool-route API call")
                                self._notify_admin_disabled_bot(aux_bot)
                                # Fallback: continue to main bot path below

                # Default: main bot with rate limit check and delayed queue
                if chat_id:
                    # Calculate rate limiting delay
                    delay_time, chat_count, global_count = self._calculate_rate_limit_delay(chat_id)  # pylint: disable=protected-access

                    if delay_time > 0:
                        # Schedule for delayed execution instead of blocking
                        self.logger.debug(f"Scheduling message for chat {chat_id} with {delay_time:.2f}s delay")

                        # Grab any pending cleanup files before scheduling
                        cleanup_files = getattr(self._cleanup_tls, 'pending_cleanup', [])[:]  # pylint: disable=protected-access
                        self._cleanup_tls.pending_cleanup = []  # pylint: disable=protected-access

                        # Schedule the delayed execution using the new system
                        task_id = self._schedule_delayed_task(  # pylint: disable=protected-access
                            chat_id=chat_id,
                            delay_time=delay_time,
                            function=fn,
                            args=(self,) + args,
                            kwargs=kwargs,
                            cleanup_files=cleanup_files
                        )

                        # Return a placeholder response to indicate message was scheduled
                        placeholder = self._create_delayed_message_placeholder(chat_id, delay_time, task_id)  # pylint: disable=protected-access
                        return placeholder

                return fn(self, *args, **kwargs)

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
                    if not (chat_id and hasattr(self, '_chat_timestamps') and hasattr(self, '_global_timestamps')):
                        return ""

                    current_time = time.time()
                    chat_timestamps = list(self._chat_timestamps.get(chat_id, []))
                    global_timestamps = self._global_timestamps

                    # Format recent timestamps relative to current time
                    chat_info = [f"{ts - current_time:.3f}s" for ts in chat_timestamps]
                    global_info = [f"{ts - current_time:.3f}s" for ts in global_timestamps]

                    return f" [chat_recent: {chat_info}, global_recent: {global_info}]"

                for attempt in range(max_retries + 1):
                    try:
                        return fn(self, *args, **kwargs)
                    except telegram.error.RetryAfter as e:
                        timestamp_info = get_timestamp_info()
                        if attempt >= max_retries:
                            cls.logger.error(f"Max retries exceeded for rate limit error: {e} (chat_id: {chat_id}){timestamp_info}")
                            raise

                        retry_after = e.retry_after
                        cls.logger.warning(f"Rate limit hit, waiting {retry_after}s before retry {attempt + 1}/{max_retries} (chat_id: {chat_id}){timestamp_info}")

                        # Use interruptible sleep for rate limit waits
                        if hasattr(self, '_delayed_worker_stop'):
                            # Sleep in small chunks to allow for interruption during shutdown
                            remaining = retry_after
                            while remaining > 0 and not self._delayed_worker_stop.is_set():
                                sleep_chunk = min(1.0, remaining)
                                time.sleep(sleep_chunk)
                                remaining -= sleep_chunk
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
                            if chat_id and hasattr(self, '_chat_timestamps'):
                                for timestamp in self._chat_timestamps[chat_id]:
                                    timestamp += delay

                            # Use interruptible sleep for rate limit waits
                            if hasattr(self, '_delayed_worker_stop'):
                                # Sleep in small chunks to allow for interruption during shutdown
                                remaining = delay
                                while remaining > 0 and not self._delayed_worker_stop.is_set():
                                    sleep_chunk = min(1.0, remaining)
                                    time.sleep(sleep_chunk)
                                    remaining -= sleep_chunk
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

                prefix, suffix = self._format_affix(prefix, suffix, kwargs.get('parse_mode', ''))

                if len(prefix + text + suffix) >= telegram.constants.MAX_CAPTION_LENGTH:
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
                with self._delayed_queue_lock:
                    if self._delayed_queue:
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

        req_kwargs = {'read_timeout': 15}
        conf_req_kwargs = config.get('request_kwargs')
        if isinstance(conf_req_kwargs, collections.abc.Mapping):
            req_kwargs.update(conf_req_kwargs)

        self.logger.debug("Setting up Telegram bot updater...")
        self.updater: Updater = Updater(config['token'],
                                        base_url=channel.flag('api_base_url'),
                                        base_file_url=channel.flag('api_base_file_url'),
                                        request_kwargs=req_kwargs,
                                        use_context=True)

        if isinstance(config.get('webhook'), dict):
            self.logger.debug("Setting up webhook...")
            self.webhook = True
            self.logger.debug("Webhook is set...")

        self.logger.debug("Checking connection to Telegram bot API...")
        # Updater.__init__ uses __slots__ + @no_type_check, so mypy cannot
        # determine the types of .bot / .dispatcher.  getattr returns Any,
        # then cast narrows to the correct type.
        self._bot = cast(telegram.Bot, getattr(self.updater, 'bot'))
        me = self._bot.get_me()
        assert me, "Invalid bot credential provided."
        self.me = me
        self.logger.debug("Connection to Telegram bot API is OK...")
        self.admins = config['admins']
        self.dispatcher = cast(Dispatcher, getattr(self.updater, 'dispatcher'))

        # Initialize sliding window rate limiting
        self._rate_limit_lock = threading.Lock()
        self._global_timestamps: list[float] = []  # Sorted list for global timestamps
        self._chat_timestamps: defaultdict[int, Deque[float]] = defaultdict(deque)

        # Rate limits based on Telegram API documentation
        self.GLOBAL_LIMIT = 30    # messages per second
        self.GLOBAL_WINDOW = 1.0
        self.CHAT_LIMIT = 20      # messages per minute per chat
        self.CHAT_WINDOW = 60.0

        self._cleanup_tls = threading.local()  # Thread-local for pending cleanup files
        self._tls = threading.local()  # Thread-local for bot override (_active_bot)
        self._aux_recent_use: dict[int, float] = {}  # chat_id -> timestamp of last aux bot use
        self.logger.debug("Rate limiter initialized...")

        # Initialize auxiliary bot pool
        self.bot_pool: Optional[BotPool] = None
        aux_configs = config.get('auxiliary_bots', [])
        if aux_configs:
            self._init_bot_pool(aux_configs, config, channel)
        self.logger.debug("Bot pool initialization complete...")

        # Initialize delayed message queue system
        self._delayed_queue: List[Tuple[float, int, DelayedTask]] = []
        self._delayed_queue_lock = threading.Lock()
        self._task_counter = 0  # Counter for tie-breaking in heapq
        self._delayed_worker_stop = threading.Event()
        self._pending_delayed_logs: dict[str, tuple] = {}  # Store pending database updates: task_id -> (etm_msg, old_msg_id)
        self._pending_logs_lock = threading.Lock()
        self._delayed_worker_thread = threading.Thread(
            target=self._delayed_message_worker,
            name="ETM delayed messages worker",
            daemon=True
        )
        self._delayed_worker_thread.start()
        self.logger.debug("Delayed message system initialized...")

        self.logger.debug("Adding base dispatchers...")
        # New whitelist handler
        whitelist_filter = ~Filters.user(user_id=self.admins)
        self.dispatcher.add_handler(
            MessageHandler(whitelist_filter, lambda update, context: ...))
        self.dispatcher.add_handler(LocaleHandler(channel))
        self.Decorators.enable_retry = channel.flag('retry_on_error')
        self.logger.debug("Base dispatchers added...")

    def _init_bot_pool(self, aux_configs: list, config: dict, channel: 'TelegramChannel'):
        """Initialize the auxiliary bot pool from config."""
        req_kwargs = {'read_timeout': 15}
        conf_req_kwargs = config.get('request_kwargs')
        if isinstance(conf_req_kwargs, collections.abc.Mapping):
            req_kwargs.update(conf_req_kwargs)

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
                request_kwargs=req_kwargs,
                base_url=channel.flag('api_base_url') or None,
                base_file_url=channel.flag('api_base_file_url') or None,
                global_limit=self.GLOBAL_LIMIT,
                global_window=self.GLOBAL_WINDOW,
                chat_limit=self.CHAT_LIMIT,
                chat_window=self.CHAT_WINDOW,
            )
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
        return getattr(self._tls, 'override_bot', None) or self._bot

    @contextmanager
    def _using_bot(self, bot):
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
                    except telegram.error.Unauthorized:
                        aux_bot.mark_disabled("Unauthorized during migration send")
                        self._notify_admin_disabled_bot(aux_bot)

            # Try main bot
            if main_delay == 0.0:
                self._calculate_rate_limit_delay(chat_id)  # reserve
                return send_callable(_bypass_rate_limit=True)

            time.sleep(0.2)

        # Timeout fallback: reserve on main and send anyway
        self.logger.warning("send_blocking_migration timed out for chat %d, sending on main bot", chat_id)
        self._calculate_rate_limit_delay(chat_id)
        return send_callable(_bypass_rate_limit=True)

    def _cleanup_old_timestamps(self):
        """Remove timestamps older than the time window."""
        current_time = time.time()
        # global rate limit
        while self._global_timestamps and self._global_timestamps[0] <= current_time - self.GLOBAL_WINDOW:
            self._global_timestamps.pop(0)

        # chat-specific rate limit
        for chat_id, timestamps in self._chat_timestamps.items():
            while timestamps and timestamps[0] <= current_time - self.CHAT_WINDOW:
                timestamps.popleft()

    def _calculate_rate_limit_delay(self, chat_id: int, peek_only: bool = False):
        """
        Calculate rate limiting delay using sliding window algorithm.

        Args:
            chat_id: Telegram chat ID
            peek_only: If True, compute delay without reserving a slot
                (no insort/append). Used to check main-bot availability
                before committing to a pool decision.

        Returns:
            tuple: (delay_time, chat_count, global_count) - delay in seconds, current queue counts
        """
        sleep_time = 0.0

        with self._rate_limit_lock:
            current_time = time.time()
            self._cleanup_old_timestamps()

            # --------------------------------------------------
            # Chat-specific rate limit (FIFO – per-chat window)
            # --------------------------------------------------
            chat_delay = 0.0
            chat_timestamps = self._chat_timestamps[chat_id]
            if len(chat_timestamps) >= self.CHAT_LIMIT - 2:
                safe_index = len(chat_timestamps) - (self.CHAT_LIMIT - 2)
                reference_timestamp = chat_timestamps[safe_index]
                chat_delay = max(0.0, (reference_timestamp + self.CHAT_WINDOW) - current_time)

            # Earliest candidate time that satisfies chat limit
            candidate_time = current_time + chat_delay

            # --------------------------------------------------
            # Global limit – find the first slot that fits
            # --------------------------------------------------
            scan_count = 0
            while True:
                left_bound = candidate_time - self.GLOBAL_WINDOW
                idx = bisect.bisect_right(self._global_timestamps, left_bound)
                right_idx = bisect.bisect_right(self._global_timestamps, candidate_time)
                in_window = right_idx - idx
                if in_window < self.GLOBAL_LIMIT - 2:
                    break
                candidate_time = self._global_timestamps[idx] + self.GLOBAL_WINDOW
                scan_count += 1

            sleep_time = max(0.0, candidate_time - current_time)

            if not peek_only:
                # Record the scheduled time in both queues
                bisect.insort(self._global_timestamps, candidate_time)
                chat_timestamps.append(candidate_time)

            chat_count = len(chat_timestamps)
            global_count = len(self._global_timestamps)

        # Log rate limiting status but don't sleep
        if sleep_time > 0:
            self.logger.info(f"Rate limit reached, need to delay {sleep_time:.2f}s for chat {chat_id}. "
                           f"Chat: {chat_count}/{self.CHAT_LIMIT}, Global: {right_idx}/{self.GLOBAL_LIMIT}, Scan count: {scan_count}")

        else:
            self.logger.debug(f"Rate limit not reached for chat {chat_id}. "
                           f"Chat: {chat_count}/{self.CHAT_LIMIT}, Global: {right_idx}/{self.GLOBAL_LIMIT}, Scan count: {scan_count}")

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

        # Create a mock message object that represents a delayed message
        mock_msg = Mock(spec=TelegramMessage)
        mock_msg.chat_id = chat_id
        mock_msg.message_id = int(time.time() * 1000)  # Use timestamp as temp ID
        mock_msg.date = int(time.time())
        mock_msg.text = f"[Message scheduled for delivery in {delay_time:.2f}s due to rate limiting]"
        mock_msg.is_delayed = True  # Custom attribute to mark as delayed
        mock_msg.delay_time = delay_time
        mock_msg.task_id = task_id  # Store task ID for tracking
        mock_msg._delayed_execution_pending = True  # Flag for database logging

        self.logger.debug(f"Created delayed message placeholder for chat {chat_id} with {delay_time:.2f}s delay")
        return mock_msg

    def _schedule_delayed_task(self, chat_id: int, delay_time: float, function: Callable,
                              args: tuple, kwargs: dict, cleanup_files: Optional[list] = None) -> str:
        """
        Schedule a task for delayed execution.

        Args:
            chat_id: Telegram chat ID
            delay_time: Delay in seconds
            function: Function to execute
            args: Function arguments
            kwargs: Function keyword arguments

        Returns:
            Task ID for tracking
        """
        execute_time = time.time() + delay_time
        task_id = f"{chat_id}_{str(time.time_ns() + delay_time * 1_000_000_000)}_{id(function)}"

        task = DelayedTask(
            execute_time=execute_time,
            chat_id=chat_id,
            function=function,
            args=args,
            kwargs=kwargs,
            task_id=task_id,
            cleanup_files=cleanup_files or []
        )

        with self._delayed_queue_lock:
            heapq.heappush(self._delayed_queue, (execute_time, self._task_counter, task))
            self._task_counter += 1

        self.logger.debug(f"Scheduled delayed task {task_id} for chat {chat_id} in {delay_time:.2f}s")
        return task_id

    def _delayed_message_worker(self):
        """
        Worker thread that processes delayed messages.
        """
        self.logger.debug("Delayed message worker started")

        while not self._delayed_worker_stop.is_set():
            try:
                current_time = time.time()
                tasks_to_execute = []

                # Check for tasks ready to execute
                with self._delayed_queue_lock:
                    while self._delayed_queue and self._delayed_queue[0][0] <= current_time:
                        _, _, task = heapq.heappop(self._delayed_queue)
                        tasks_to_execute.append(task)

                # Execute ready tasks outside of lock
                for task in tasks_to_execute:
                    # Check if we should stop before executing each task
                    if self._delayed_worker_stop.is_set():
                        break

                    try:
                        self.logger.debug(f"Executing delayed task {task.task_id} for chat {task.chat_id}")

                        # Re-check pool: if an aux bot has become available, route through it
                        result = None
                        routed_to_aux = False
                        if self.bot_pool and task.chat_id:
                            slot = self.bot_pool.acquire_send_slot(task.chat_id, max_delay=0.01)
                            if slot is not None:
                                aux_bot, _ = slot
                                try:
                                    with self._using_bot(aux_bot.bot):
                                        result = task.function(*task.args, **task.kwargs)
                                    if result and hasattr(result, '__dict__'):
                                        result._sender_bot_id = str(aux_bot.bot_id)
                                    self._record_aux_use(task.chat_id)
                                    routed_to_aux = True
                                    self.logger.debug(f"Delayed task {task.task_id} routed to aux bot @{aux_bot.username}")
                                except telegram.error.Unauthorized:
                                    aux_bot.mark_disabled("Unauthorized during delayed task")
                                    self._notify_admin_disabled_bot(aux_bot)

                        if not routed_to_aux:
                            result = task.function(*task.args, **task.kwargs)
                        self.logger.debug(f"Delayed task {task.task_id} completed successfully")

                        # Handle database update for delayed execution
                        if result and hasattr(result, 'message_id'):
                            self._handle_delayed_database_update(task.task_id, result)

                    except Exception as e:
                        self.logger.exception(f"Error executing delayed task {task.task_id}: {e}")
                    finally:
                        # Clean up temp files associated with this delayed task
                        for path in task.cleanup_files:
                            try:
                                os.unlink(path)
                                self.logger.debug("Cleaned up delayed task temp file: %s", path)
                            except OSError as e:
                                self.logger.warning("Failed to clean up delayed task temp file %s: %s", path, e)

                # Use event wait with timeout instead of sleep for faster shutdown response
                if not self._delayed_worker_stop.wait(timeout=0.1):
                    continue

            except Exception as e:
                self.logger.exception(f"Error in delayed message worker: {e}")
                # Use event wait with timeout for error cases too
                self._delayed_worker_stop.wait(timeout=1)

        self.logger.debug("Delayed message worker stopped")

    def register_delayed_database_update(self, task_id: str, etm_msg, old_msg_id=None):
        """
        Register a pending database update for a delayed task.

        Args:
            task_id: Task identifier from delayed execution
            etm_msg: ETMMsg object to log
            old_msg_id: Optional old message ID for updates
        """
        with self._pending_logs_lock:
            self._pending_delayed_logs[task_id] = (etm_msg, old_msg_id)
        self.logger.debug(f"Registered delayed database update for task {task_id}")

    def _handle_delayed_database_update(self, task_id: str, real_tg_msg):
        """
        Handle database update when delayed task completes.

        Args:
            task_id: Task identifier
            real_tg_msg: The real Telegram message that was sent
        """
        with self._pending_logs_lock:
            if task_id in self._pending_delayed_logs:
                etm_msg, old_msg_id = self._pending_delayed_logs.pop(task_id)

                # Update the ETM message with real Telegram data
                etm_msg.type_telegram = get_msg_type(real_tg_msg)
                etm_msg.put_telegram_file(real_tg_msg)

                # Capture sender_bot_id from the real message (annotated by rate_limit_decorator)
                sender_bot_id = getattr(real_tg_msg, '_sender_bot_id', None)

                # Update database with real message
                self.channel.db.add_or_update_message_log(
                    etm_msg, real_tg_msg, old_msg_id, sender_bot_id=sender_bot_id)
                self.logger.debug(f"Updated database with real message for delayed task {task_id}")
            else:
                self.logger.warning(f"No pending database update found for task {task_id}")

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
        prefix, suffix = self._format_affix(prefix, suffix, kwargs.get('parse_mode', ''))
        text: str
        if args[1:]:
            text = args[1]
        else:
            text = kwargs.pop('text')
        args = args[:1]
        if len(prefix + text + suffix) >= telegram.constants.MAX_MESSAGE_LENGTH:
            full_message = self._message_file_buffer(prefix + text + suffix, kwargs.get('parse_mode', ''))
            truncated = prefix + text[:100] + "\n...\n" + text[-100:] + suffix
            msg = self._bot_send_message_fallback(args[0], text=truncated, **kwargs)
            filename = "%s_%s" % (args[0], msg.message_id)
            if not kwargs.get('parse_mode'):
                filename += ".txt"
            elif kwargs.get('parse_mode', '').lower() == 'markdown':
                filename += ".md"
            elif self._is_html_parse_mode(kwargs.get('parse_mode', '')):
                filename += ".html"
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
        prefix, suffix = self._format_affix(prefix, suffix, kwargs.get('parse_mode', ''))
        text = kwargs.pop('text', '')
        if len(prefix + text + suffix) >= telegram.constants.MAX_MESSAGE_LENGTH:
            full_message = self._message_file_buffer(prefix + text + suffix, kwargs.get('parse_mode', ''))
            truncated = prefix + text[:100] + "\n...\n" + text[-100:] + suffix
            msg = self._bot_edit_message_text_fallback(text=truncated, **kwargs)
            filename = "%s_%s" % (kwargs['chat_id'], msg.message_id)
            if kwargs.get('parse_mode', '').lower() == 'markdown':
                filename += ".md"
            elif self._is_html_parse_mode(kwargs.get('parse_mode', '')):
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
            update.callback_query.answer()
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
        return self._active_bot.get_file(file_id)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def delete_message(self, chat_id, message_id, _sender_bot_id=None):
        if _sender_bot_id and self.bot_pool:
            aux_bot = self.bot_pool.get_bot_by_id(_sender_bot_id)
            if aux_bot and not aux_bot.disabled:
                try:
                    return aux_bot.bot.delete_message(chat_id, message_id)
                except telegram.error.Unauthorized:
                    aux_bot.mark_disabled("Unauthorized during delete_message")
                    self._notify_admin_disabled_bot(aux_bot)
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
            self._bot.send_document(args[0], full_message_buffer, filename,
                                           reply_to_message_id=message_id,
                                           caption=self._("Response is truncated due to its length. "
                                                          "Full message is sent as attachment."))
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
        return self._bot.create_forum_topic(*args, **kwargs)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def edit_forum_topic(self, *args, **kwargs):
        return self._bot.edit_forum_topic(*args, **kwargs)

    @Decorators.retry_on_timeout
    @Decorators.retry_on_chat_migration
    def reopen_forum_topic(self, *args, **kwargs) -> bool:
        return self._bot.reopen_forum_topic(*args, **kwargs)

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

    def polling(self, drop_pending_updates: bool = False):
        """
        Poll message from Telegram Bot API. Can be used to extend for web hook.
        This method must NOT be blocking.

        Args:
            drop_pending_updates: Whether to clean any pending updates on
                Telegram servers before actually starting to poll.
                Default is False.
        """
        if self.webhook:
            start_webhook = self.channel.config['webhook']['start_webhook']
            self.updater.start_webhook(**start_webhook)
        else:
            self.updater.start_polling(timeout=10, drop_pending_updates=drop_pending_updates)

    def graceful_stop(self):
        """Gracefully stop the bot"""
        self.logger.info("Starting graceful shutdown...")

        # Log pending tasks count before stopping
        pending_count = 0
        if hasattr(self, '_delayed_queue'):
            with self._delayed_queue_lock:
                pending_count = len(self._delayed_queue)

        if pending_count > 0:
            self.logger.info(f"Found {pending_count} pending delayed tasks")

        # Stop the delayed message worker first
        self.stop_delayed_worker()

        # Shut down auxiliary bot pool
        if self.bot_pool:
            self.bot_pool.shutdown()

        # Then stop the updater
        self.logger.debug("Stopping Telegram updater...")
        self.updater.stop()
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
