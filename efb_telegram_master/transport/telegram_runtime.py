"""PTB application construction and polling lifecycle."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from socket import socket
from typing import Coroutine, Optional, ParamSpec, TypedDict, TypeVar

import telegram
from telegram import Update
from telegram.ext import Application, CallbackContext
from telegram.request import HTTPXRequest
from typing_extensions import NotRequired

from ..config.request import RequestConfiguration, parse_request_configuration
from ..config.runtime import RuntimeConfiguration
from .telegram_application_lifecycle import LifecycleCallback, TelegramApplicationLifecycle
from .telegram_sync_bridge import AsyncTelegramRuntime, SyncBotFacade

P = ParamSpec("P")
T = TypeVar("T")
LocaleUpdateCallback = Callable[[Update, CallbackContext], None]


class TelegramRuntimeShutdownTimeout(RuntimeError):
    """The PTB application did not finish shutdown before its deadline."""


class _BotArguments(TypedDict):
    token: str
    local_mode: bool
    request: HTTPXRequest
    get_updates_request: HTTPXRequest
    base_url: NotRequired[str]
    base_file_url: NotRequired[str]


class _WebhookStartArguments(TypedDict, total=False):
    listen: str
    port: int
    url_path: str
    cert: str | Path
    key: str | Path
    bootstrap_retries: int
    webhook_url: str | None
    allowed_updates: Sequence[str] | None
    ip_address: str | None
    max_connections: int
    secret_token: str | None
    unix: str | Path | socket | None


def _webhook_start_arguments(config: Mapping[str, object]) -> _WebhookStartArguments:
    arguments: _WebhookStartArguments = {}
    if unexpected := set(config).difference(
        ("listen", "port", "url_path", "cert", "key", "bootstrap_retries", "webhook_url", "allowed_updates", "ip_address", "max_connections", "secret_token", "unix")
    ):
        raise ValueError(f"webhook.start_webhook contains unsupported option(s): {', '.join(sorted(unexpected))}")
    for name in ("listen", "url_path"):
        value = config.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"webhook.start_webhook.{name} must be a string")
            if name == "listen":
                arguments["listen"] = value
            else:
                arguments["url_path"] = value
    for name in ("port", "bootstrap_retries", "max_connections"):
        value = config.get(name)
        if value is not None:
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"webhook.start_webhook.{name} must be an integer")
            if name == "port":
                arguments["port"] = value
            elif name == "bootstrap_retries":
                arguments["bootstrap_retries"] = value
            else:
                arguments["max_connections"] = value
    for name in ("cert", "key"):
        value = config.get(name)
        if value is not None:
            if not isinstance(value, (str, Path)):
                raise ValueError(f"webhook.start_webhook.{name} must be a string or path")
            if name == "cert":
                arguments["cert"] = value
            else:
                arguments["key"] = value
    for name in ("webhook_url", "ip_address", "secret_token"):
        value = config.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"webhook.start_webhook.{name} must be a string or null")
            if name == "webhook_url":
                arguments["webhook_url"] = value
            elif name == "ip_address":
                arguments["ip_address"] = value
            else:
                arguments["secret_token"] = value
    if "allowed_updates" in config:
        allowed_updates = config["allowed_updates"]
        if allowed_updates is None:
            arguments["allowed_updates"] = None
        elif isinstance(allowed_updates, Sequence) and not isinstance(allowed_updates, str):
            values: list[str] = []
            for value in allowed_updates:
                if not isinstance(value, str):
                    raise ValueError("webhook.start_webhook.allowed_updates must contain strings")
                values.append(value)
            arguments["allowed_updates"] = values
        else:
            raise ValueError("webhook.start_webhook.allowed_updates must be a sequence or null")
    if "unix" in config:
        unix = config["unix"]
        if unix is None or isinstance(unix, (str, Path, socket)):
            arguments["unix"] = unix
        else:
            raise ValueError("webhook.start_webhook.unix must be a string, path, socket, or null")
    return arguments


def build_request(request_kwargs: Mapping[str, object] | RequestConfiguration) -> HTTPXRequest:
    configuration = request_kwargs if isinstance(request_kwargs, RequestConfiguration) else parse_request_configuration(request_kwargs)
    return HTTPXRequest(
        connection_pool_size=configuration.connection_pool_size,
        read_timeout=configuration.read_timeout,
        write_timeout=configuration.write_timeout,
        connect_timeout=configuration.connect_timeout,
        pool_timeout=configuration.pool_timeout,
        media_write_timeout=configuration.media_write_timeout,
        http_version=configuration.http_version,
        socket_options=configuration.socket_options,
        proxy=configuration.proxy,
        httpx_kwargs=configuration.httpx_kwargs,
    )


class TelegramPollingRuntime:
    """Own one PTB application and its explicit polling lifecycle."""

    def __init__(
        self,
        logger: logging.Logger,
        application: Application | None,
        async_bot: telegram.Bot,
        async_runtime: AsyncTelegramRuntime,
        on_started: LifecycleCallback,
        on_stopped: LifecycleCallback,
        webhook: Mapping[str, object] | None = None,
    ) -> None:
        self.logger, self._on_started, self._on_stopped = logger, on_started, on_stopped
        self._shutdown_complete = threading.Event()
        self._stop_lock = threading.Lock()
        self._stop_requested = False
        self._stopped = False
        self._lifecycle_active = False
        self.async_runtime = async_runtime
        self.async_bot = async_bot
        self.bot = SyncBotFacade(self.async_bot, self.async_runtime)
        self._application = application
        self.me: telegram.User | None = None
        self._webhook = webhook
        self._application_lifecycle = TelegramApplicationLifecycle(self, logger, async_runtime, on_started, on_stopped)

    @property
    def application(self) -> Application:
        if self._application is None:
            raise RuntimeError("Telegram application has not been built.")
        return self._application

    @property
    def _stop_event(self) -> Optional[asyncio.Event]:
        return self._application_lifecycle.stop_event

    @_stop_event.setter
    def _stop_event(self, value: Optional[asyncio.Event]) -> None:
        self._application_lifecycle.stop_event = value

    def as_async_callback(self, callback: Callable[P, T]) -> Callable[P, Coroutine[object, object, T]]:
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await asyncio.to_thread(callback, *args, **kwargs)

        return wrapper

    def add_base_dispatchers(self, admins: Collection[int], update_locale: LocaleUpdateCallback) -> None:
        from telegram.ext import MessageHandler, TypeHandler

        from .ptb_compat import Filters

        self.application.add_handler(MessageHandler(~Filters.user(user_id=admins), self.as_async_callback(lambda update, context: None)))
        self.application.add_handler(TypeHandler(Update, self.as_async_callback(update_locale)), group=-1)

    def _stop_requested_for_lifecycle(self) -> bool:
        with self._stop_lock:
            return self._stop_requested

    def poll(self, drop_pending_updates: bool = False, timeout: int = 10) -> None:
        start_webhook: _WebhookStartArguments | None = None
        if self._webhook is not None:
            configured_webhook = self._webhook.get("start_webhook")
            if not isinstance(configured_webhook, Mapping):
                raise ValueError("webhook.start_webhook must be a mapping")
            start_webhook = _webhook_start_arguments(configured_webhook)
        with self._stop_lock:
            if self._stopped or self._stop_requested:
                raise RuntimeError("Telegram polling runtime has been stopped.")
            if self._lifecycle_active:
                raise RuntimeError("Telegram polling runtime is already active.")
            self._shutdown_complete.clear()
            self._lifecycle_active = True
        if start_webhook is not None:
            self.logger.info("Telegram webhook runtime starting", extra={"event": "telegram_runtime.webhook_start"})
            try:
                self.application.run_webhook(
                    **start_webhook,
                    drop_pending_updates=drop_pending_updates,
                    close_loop=True,
                    stop_signals=None,
                )
            except BaseException as error:
                self.logger.exception(
                    "Telegram webhook lifecycle failed",
                    extra={"event": "telegram_runtime.webhook_failed", "error_type": type(error).__name__},
                )
                raise
            finally:
                self._shutdown_complete.set()
                with self._stop_lock:
                    self._lifecycle_active = False
                self.logger.info("Telegram webhook runtime stopped", extra={"event": "telegram_runtime.webhook_stop"})
            return
        try:
            asyncio.run(
                self._application_lifecycle.run(
                    drop_pending_updates=drop_pending_updates,
                    timeout=timeout,
                    stop_requested=lambda: self._stop_requested_for_lifecycle(),
                )
            )
        except BaseException as error:
            self.logger.exception(
                "Telegram polling lifecycle failed",
                extra={"event": "telegram_runtime.polling_failed", "error_type": type(error).__name__},
            )
            raise
        finally:
            self._shutdown_complete.set()
            with self._stop_lock:
                self._lifecycle_active = False

    def stop(self, deadline: float | None = None) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            stop_event = self._stop_event
            lifecycle_active = self._lifecycle_active
            if not self._stop_requested:
                self._stop_requested = True
                if stop_event is not None:
                    self.async_runtime.call_soon(stop_event.set)
                elif not self.async_runtime.call_soon(self.application.stop_running):
                    try:
                        self.application.stop_running()
                    except RuntimeError:
                        pass
        remaining = 30.0 if deadline is None else max(0.0, deadline - time.monotonic())
        if (lifecycle_active or stop_event is not None) and not self._shutdown_complete.wait(timeout=remaining):
            self.logger.warning("Telegram post-shutdown hook timed out", extra={"event": "telegram_runtime.shutdown_timeout", "timeout_seconds": remaining})
            raise TelegramRuntimeShutdownTimeout(f"Telegram runtime did not stop within {remaining:g}s.")
        self.async_runtime.shutdown(deadline)
        with self._stop_lock:
            self._stopped = True


def build_telegram_polling_runtime(
    config: RuntimeConfiguration,
    channel: object,
    logger: logging.Logger,
    on_started: LifecycleCallback,
    on_stopped: LifecycleCallback,
) -> TelegramPollingRuntime:
    identity: _BotArguments = {
        "token": config.token,
        "local_mode": bool(getattr(channel, "flag")("local_tdlib_api")),
        "request": build_request(config.request),
        "get_updates_request": build_request(config.request),
    }
    base_url = getattr(channel, "flag")("api_base_url")
    if base_url:
        identity["base_url"] = base_url
    base_file_url = getattr(channel, "flag")("api_base_file_url")
    if base_file_url:
        identity["base_file_url"] = base_file_url
    async_bot = telegram.Bot(**identity)
    async_runtime = AsyncTelegramRuntime(logger)
    runtime = TelegramPollingRuntime(
        logger,
        None,
        async_bot,
        async_runtime,
        on_started,
        on_stopped,
        config.webhook,
    )

    application = Application.builder().bot(async_bot).job_queue(None).post_init(runtime._application_lifecycle.post_init).post_shutdown(runtime._application_lifecycle.post_shutdown).build()
    runtime._application = application
    return runtime
