"""PTB application construction and polling lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from pathlib import Path
from socket import socket
from typing import Coroutine, Literal, Optional, ParamSpec, TypedDict, TypeVar, cast

import telegram
import telegram.error
from telegram import Update
from telegram.ext import Application, CallbackContext
from telegram.request import HTTPXRequest
from typing_extensions import NotRequired

from .telegram_sync_bridge import AsyncTelegramRuntime, SyncBotFacade
from .utils import normalize_request_kwargs

P = ParamSpec("P")
T = TypeVar("T")
LifecycleCallback = Callable[["TelegramPollingRuntime"], Awaitable[None]]
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


def build_request(request_kwargs: Mapping[str, object]) -> HTTPXRequest:
    return HTTPXRequest(
        connection_pool_size=cast(int, request_kwargs.get("connection_pool_size", 1)),
        read_timeout=cast(Optional[float], request_kwargs.get("read_timeout")),
        write_timeout=cast(Optional[float], request_kwargs.get("write_timeout")),
        connect_timeout=cast(Optional[float], request_kwargs.get("connect_timeout")),
        pool_timeout=cast(Optional[float], request_kwargs.get("pool_timeout")),
        media_write_timeout=cast(Optional[float], request_kwargs.get("media_write_timeout")),
        http_version=cast(Literal["1.1", "2.0", "2"], request_kwargs.get("http_version") or "1.1"),
        socket_options=cast(
            Optional[Collection[tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]]],
            request_kwargs.get("socket_options"),
        ),
        proxy=cast(Optional[str], request_kwargs.get("proxy")),
        httpx_kwargs=cast(Optional[dict[str, object]], request_kwargs.get("httpx_kwargs")),
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
        self._stop_event: Optional[asyncio.Event] = None
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

    @property
    def application(self) -> Application:
        if self._application is None:
            raise RuntimeError("Telegram application has not been built.")
        return self._application

    def as_async_callback(self, callback: Callable[P, T]) -> Callable[P, Coroutine[object, object, T]]:
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            return await asyncio.to_thread(callback, *args, **kwargs)

        return wrapper

    def add_base_dispatchers(self, admins: Collection[int], update_locale: LocaleUpdateCallback) -> None:
        from telegram.ext import MessageHandler, TypeHandler

        from .ptb_compat import Filters

        self.application.add_handler(MessageHandler(~Filters.user(user_id=admins), self.as_async_callback(lambda update, context: None)))
        self.application.add_handler(TypeHandler(Update, self.as_async_callback(update_locale)), group=-1)

    @staticmethod
    def _default_connection_pool_size(config: Mapping[str, object]) -> int:
        multiplier = 2.0
        try:
            configured = float(os.getenv("ETM_HTTPX_POOL_MULTIPLIER", multiplier))
            multiplier = configured if configured > 0 else multiplier
        except ValueError:
            pass
        return max(1, int(round(8 * multiplier)))

    async def _post_init(self, _application: Application) -> None:
        self.async_runtime.bind_loop(asyncio.get_running_loop())
        self._shutdown_complete.clear()
        self.me = await self.async_bot.get_me()
        assert self.me, "Invalid bot credential provided."
        try:
            await self._on_started(self)
        except Exception as error:
            self.logger.exception(
                "Telegram runtime start callback failed",
                extra={"event": "telegram_runtime.start_callback_failed", "error_type": type(error).__name__},
            )
            raise
        self.logger.info("Telegram polling runtime started", extra={"event": "telegram_runtime.start"})

    async def _post_shutdown(self, _application: Application) -> None:
        try:
            await self._on_stopped(self)
        except Exception as error:
            self.logger.exception(
                "Telegram runtime stop callback failed",
                extra={"event": "telegram_runtime.stop_callback_failed", "error_type": type(error).__name__},
            )
            raise
        finally:
            self.async_runtime.clear_loop()
            self._shutdown_complete.set()
            self.logger.info("Telegram polling runtime stopped", extra={"event": "telegram_runtime.stop"})

    async def _run_application_lifecycle(self, *, drop_pending_updates: bool, timeout: int) -> None:
        stop_event = asyncio.Event()
        try:
            await self.application.initialize()
            if self.application.post_init:
                await self.application.post_init(self.application)
            self._stop_event = stop_event
            updater = self.application.updater
            if updater is None:
                raise RuntimeError("Application.run_polling requires an Updater.")
            await updater.start_polling(
                poll_interval=0.0,
                timeout=timeout,
                bootstrap_retries=0,
                allowed_updates=None,
                drop_pending_updates=drop_pending_updates,
                error_callback=self._handle_polling_error,
            )
            await self.application.start()
            await stop_event.wait()
        finally:
            self._stop_event = None
            await self._shutdown_application()

    def _handle_polling_error(self, error: telegram.error.TelegramError) -> None:
        self.application.create_task(self.application.process_error(error=error, update=None))

    async def _shutdown_application(self) -> None:
        try:
            updater = self.application.updater
            if updater is not None and updater.running:
                await updater.stop()
        except Exception as error:
            self.logger.exception("Telegram updater stop failed", extra={"event": "telegram_runtime.updater_stop_failed", "error_type": type(error).__name__})
        try:
            if self.application.running:
                await self.application.stop()
        except Exception as error:
            self.logger.exception("Telegram application stop failed", extra={"event": "telegram_runtime.application_stop_failed", "error_type": type(error).__name__})
        try:
            if self.application.post_stop:
                await self.application.post_stop(self.application)
        except Exception as error:
            self.logger.exception("Telegram post-stop hook failed", extra={"event": "telegram_runtime.post_stop_failed", "error_type": type(error).__name__})
        try:
            await self.application.shutdown()
        except Exception as error:
            self.logger.exception("Telegram application shutdown failed", extra={"event": "telegram_runtime.shutdown_failed", "error_type": type(error).__name__})
        try:
            if self.application.post_shutdown:
                await self.application.post_shutdown(self.application)
        except Exception as error:
            self.logger.exception("Telegram post-shutdown hook failed", extra={"event": "telegram_runtime.post_shutdown_failed", "error_type": type(error).__name__})

    def poll(self, drop_pending_updates: bool = False, timeout: int = 10) -> None:
        start_webhook: _WebhookStartArguments | None = None
        if self._webhook is not None:
            configured_webhook = self._webhook.get("start_webhook")
            if not isinstance(configured_webhook, Mapping):
                raise ValueError("webhook.start_webhook must be a mapping")
            start_webhook = _webhook_start_arguments(configured_webhook)
        with self._stop_lock:
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
            asyncio.run(self._run_application_lifecycle(drop_pending_updates=drop_pending_updates, timeout=timeout))
        except BaseException as error:
            self.logger.exception(
                "Telegram polling lifecycle failed",
                extra={"event": "telegram_runtime.polling_failed", "error_type": type(error).__name__},
            )
            raise
        finally:
            self._stop_event = None
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
    config: Mapping[str, object],
    channel: object,
    logger: logging.Logger,
    on_started: LifecycleCallback,
    on_stopped: LifecycleCallback,
) -> TelegramPollingRuntime:
    request_config: dict[str, object] = {"read_timeout": 15.0, "connection_pool_size": TelegramPollingRuntime._default_connection_pool_size(config)}
    configured = config.get("request_kwargs")
    if isinstance(configured, Mapping):
        request_config.update(configured)
    request_kwargs = normalize_request_kwargs(request_config)
    identity: _BotArguments = {
        "token": cast(str, config["token"]),
        "local_mode": bool(getattr(channel, "flag")("local_tdlib_api")),
        "request": build_request(request_kwargs),
        "get_updates_request": build_request(request_kwargs),
    }
    base_url = getattr(channel, "flag")("api_base_url")
    if base_url:
        identity["base_url"] = base_url
    base_file_url = getattr(channel, "flag")("api_base_file_url")
    if base_file_url:
        identity["base_file_url"] = base_file_url
    async_bot = telegram.Bot(**identity)
    async_runtime = AsyncTelegramRuntime(logger)
    if (webhook := config.get("webhook")) is not None and not isinstance(webhook, Mapping):
        raise ValueError("webhook must be a mapping")
    runtime = TelegramPollingRuntime(
        logger,
        None,
        async_bot,
        async_runtime,
        on_started,
        on_stopped,
        webhook,
    )

    async def post_init(application: Application) -> None:
        await runtime._post_init(application)

    async def post_shutdown(application: Application) -> None:
        await runtime._post_shutdown(application)

    application = Application.builder().bot(async_bot).job_queue(None).post_init(post_init).post_shutdown(post_shutdown).build()
    runtime._application = application
    return runtime
