# coding=utf-8
import asyncio
import inspect
import threading
from functools import wraps
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, TYPE_CHECKING, cast

import telegram
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    BaseHandler,
    CallbackQueryHandler as PTBCallbackQueryHandler,
    CommandHandler as PTBCommandHandler,
    ConversationHandler as PTBConversationHandler,
    MessageHandler as PTBMessageHandler,
)

if TYPE_CHECKING:
    from telegram import Chat, Message
    from telegram.ext import CallbackContext


class SyncTelegramBot:
    """Synchronous facade for PTB's asyncio-based Bot methods."""

    def __init__(self, bot: telegram.Bot):
        self._bot = bot

    @property
    def wrapped_bot(self) -> telegram.Bot:
        return self._bot

    def __getattr__(self, item: str) -> Any:
        attr = getattr(self._bot, item)
        if callable(attr):
            @wraps(attr)
            def call(*args, **kwargs):
                return run_sync(attr(*args, **kwargs))

            return call
        return attr


class SyncApplication:
    """Expose the PTB v13 dispatcher surface used by this package."""

    def __init__(self, application: Application):
        self.application = application
        self.bot = SyncTelegramBot(application.bot)
        self._thread: Optional[threading.Thread] = None

    @property
    def dispatcher(self) -> "SyncApplication":
        return self

    def add_handler(self, handler: BaseHandler, *args, **kwargs):
        return self.application.add_handler(handler, *args, **kwargs)

    def add_error_handler(self, callback: Callable, *args, **kwargs):
        return self.application.add_error_handler(wrap_callback(callback), *args, **kwargs)

    def start_polling(self, *args, **kwargs):
        kwargs.setdefault("stop_signals", None)
        return self._start_thread(self.application.run_polling, *args, **kwargs)

    def start_webhook(self, *args, **kwargs):
        kwargs.setdefault("stop_signals", None)
        return self._start_thread(self.application.run_webhook, *args, **kwargs)

    def stop(self):
        stop_running = getattr(self.application, "stop_running", None)
        if callable(stop_running):
            try:
                stop_running()
            except RuntimeError:
                pass
        if getattr(self.application, "running", False):
            run_sync(self.application.stop())
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        return None

    def _start_thread(self, target: Callable, *args, **kwargs):
        if self._thread is not None and self._thread.is_alive():
            return None

        def run():
            target(*args, **kwargs)

        self._thread = threading.Thread(target=run, daemon=True, name="ETM Telegram application")
        self._thread.start()
        return None


class SyncCallbackQuery:
    def __init__(self, callback_query: Any, bot: Optional[SyncTelegramBot] = None):
        self._callback_query = callback_query
        self._bot = bot

    def __getattr__(self, item: str) -> Any:
        attr = getattr(self._callback_query, item)
        if callable(attr):
            @wraps(attr)
            def call(*args, **kwargs):
                return run_sync(attr(*args, **kwargs))

            return call
        return attr

    @property
    def message(self) -> Any:
        message = self._callback_query.message
        if message is None:
            return None
        return SyncMessage(message, self._bot)


class SyncMessage:
    def __init__(self, message: Any, bot: Optional[SyncTelegramBot] = None):
        self._message = message
        self._bot = bot

    def __getattr__(self, item: str) -> Any:
        attr = getattr(self._message, item)
        if callable(attr):
            @wraps(attr)
            def call(*args, **kwargs):
                if item.startswith("reply_") and "quote" in kwargs and "do_quote" not in kwargs:
                    kwargs["do_quote"] = kwargs.pop("quote")
                return run_sync(attr(*args, **kwargs))

            return call
        return attr

    @property
    def bot(self):
        if self._bot is not None:
            return self._bot
        bot = getattr(self._message, "bot", None)
        if bot is None:
            return None
        return SyncTelegramBot(bot)

    @property
    def wrapped_message(self) -> Any:
        return self._message


class SyncUpdate:
    def __init__(self, update: Any, bot: Optional[SyncTelegramBot] = None):
        self._update = update
        self._bot = bot

    def __getattr__(self, item: str) -> Any:
        return getattr(self._update, item)

    @property
    def callback_query(self) -> Any:
        callback_query = self._update.callback_query
        if callback_query is None:
            return None
        return SyncCallbackQuery(callback_query, self._bot)

    def _message(self, attr: str) -> Any:
        message = getattr(self._update, attr)
        if message is None:
            return None
        return SyncMessage(message, self._bot)

    @property
    def message(self):
        return self._message("message")

    @property
    def edited_message(self):
        return self._message("edited_message")

    @property
    def channel_post(self):
        return self._message("channel_post")

    @property
    def edited_channel_post(self):
        return self._message("edited_channel_post")

    @property
    def effective_message(self):
        return self._message("effective_message")


SUPPORTED_REQUEST_KWARGS = {
    "connection_pool_size",
    "connect_timeout",
    "http_version",
    "media_write_timeout",
    "pool_timeout",
    "proxy",
    "read_timeout",
    "socket_options",
    "write_timeout",
}


def _proxy_with_auth(proxy: Any, username: Any, password: Any) -> Any:
    if (
        username is None
        or password is None
        or not isinstance(proxy, str)
        or "://" not in proxy
        or "@" in proxy
    ):
        return proxy
    scheme, rest = proxy.split("://", 1)
    return f"{scheme}://{username}:{password}@{rest}"


def build_request_kwargs(request_kwargs: Optional[dict]) -> Dict[str, Any]:
    if not request_kwargs:
        return {}
    kwargs = dict(request_kwargs)
    if "proxy_url" in kwargs:
        kwargs["proxy"] = kwargs.pop("proxy_url")
    username = kwargs.pop("username", None)
    password = kwargs.pop("password", None)
    proxy_auth = kwargs.pop("urllib3_proxy_kwargs", None)
    if proxy_auth:
        username = proxy_auth.get("username", username)
        password = proxy_auth.get("password", password)
    if "proxy" in kwargs:
        kwargs["proxy"] = _proxy_with_auth(kwargs["proxy"], username, password)
    return {key: value for key, value in kwargs.items() if key in SUPPORTED_REQUEST_KWARGS}


def build_request(request_kwargs: Optional[dict]) -> Optional[HTTPXRequest]:
    kwargs = build_request_kwargs(request_kwargs)
    if not kwargs:
        return None
    return HTTPXRequest(**kwargs)


def build_application(token: str, *, base_url=None, base_file_url=None,
                      request_kwargs: Optional[dict] = None) -> SyncApplication:
    builder: ApplicationBuilder = Application.builder().token(token)
    if base_url:
        builder = builder.base_url(base_url)
    if base_file_url:
        builder = builder.base_file_url(base_file_url)
    for name, value in build_request_kwargs(request_kwargs).items():
        setter = getattr(builder, name, None)
        if callable(setter):
            builder = setter(value)
    return SyncApplication(builder.build())


def build_bot(token: str, *, base_url=None, base_file_url=None,
              request_kwargs: Optional[dict] = None) -> SyncTelegramBot:
    kwargs: Dict[str, Any] = {}
    if base_url:
        kwargs["base_url"] = base_url
    if base_file_url:
        kwargs["base_file_url"] = base_file_url
    request = build_request(request_kwargs)
    if request is not None:
        kwargs["request"] = request
    return SyncTelegramBot(telegram.Bot(token=token, **kwargs))


def run_sync(value):
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)

    result = {}
    error = {}

    def runner():
        try:
            result["value"] = asyncio.run(value)
        except BaseException as e:
            error["value"] = e

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error["value"]
    return result.get("value")


def wrap_callback(callback: Callable) -> Callable:
    if inspect.iscoroutinefunction(callback):
        return callback

    @wraps(callback)
    async def wrapped(update, context):
        sync_update = SyncUpdate(update, SyncTelegramBot(context.bot))
        return callback(sync_update, context)

    return wrapped


def wrap_handler_callback(handler: BaseHandler) -> BaseHandler:
    callback = getattr(handler, "callback", None)
    if callable(callback):
        handler.callback = wrap_callback(callback)
    return handler


def wrap_handler_callbacks(handlers: Iterable[BaseHandler]) -> list:
    return [wrap_handler_callback(handler) for handler in handlers]


def wrap_conversation_state_handlers(states: Mapping[Any, Iterable[BaseHandler]]) -> Dict[Any, list]:
    return {
        state: wrap_handler_callbacks(handlers)
        for state, handlers in states.items()
    }


def CommandHandler(command, callback, *args, **kwargs):
    return PTBCommandHandler(command, wrap_callback(callback), *args, **kwargs)


def CallbackQueryHandler(callback, *args, **kwargs):
    return PTBCallbackQueryHandler(wrap_callback(callback), *args, **kwargs)


def MessageHandler(filters, callback, *args, **kwargs):
    return PTBMessageHandler(filters, wrap_callback(callback), *args, **kwargs)


class ConversationHandler(PTBConversationHandler):
    def __init__(self, entry_points, states, fallbacks, *args, **kwargs):
        super().__init__(
            wrap_handler_callbacks(entry_points),
            wrap_conversation_state_handlers(states),
            wrap_handler_callbacks(fallbacks),
            *args,
            **kwargs,
        )

    @property
    def conversations(self):
        return self._conversations


def conversation_state(handler: PTBConversationHandler) -> Dict[Any, object]:
    return cast(Dict[Any, object], handler._conversations)


def sync_update(update: telegram.Update) -> Any:
    return cast(Any, update)


def sync_message(message: Any) -> Any:
    return cast(SyncMessage, message)


def sync_callback_query(callback_query: Any) -> Any:
    return cast(SyncCallbackQuery, callback_query)


def forwarded_from_chat(message: Any) -> Optional["Chat"]:
    forward_origin = getattr(message, "forward_origin", None)
    if forward_origin is not None:
        origin_chat = getattr(forward_origin, "chat", None)
        if origin_chat is not None:
            return cast("Chat", origin_chat)
    return cast(Optional["Chat"], getattr(message, "forward_from_chat", None))


def forbidden_errors():
    return tuple(
        error for error in (
            getattr(telegram.error, "Forbidden", None),
            getattr(telegram.error, "InvalidToken", None),
        ) if error is not None
    )
