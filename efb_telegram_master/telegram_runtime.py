"""PTB construction, polling, and synchronous Bot API access."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Collection, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import wraps
from typing import Coroutine, Literal, NotRequired, Optional, TypedDict, TypeVar, cast
from unittest.mock import patch

import telegram
import telegram.error
from telegram.ext import Application
from telegram.ext import _applicationbuilder as ptb_applicationbuilder
from telegram.request import HTTPXRequest

from .utils import normalize_request_kwargs

T = TypeVar("T")
BotMethod = Callable[..., object]
LifecycleCallback = Callable[["TelegramPollingRuntime"], Awaitable[None]]


class _BotIdentity(TypedDict):
    token: str
    local_mode: bool
    base_url: NotRequired[str]
    base_file_url: NotRequired[str]


class _BotArguments(_BotIdentity):
    request: HTTPXRequest
    get_updates_request: HTTPXRequest


class AsyncTelegramRuntime:
    """Thread-safe bridge into the PTB event loop."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._loop_thread_id: Optional[int] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._owns_loop_thread = False
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop, *, owns_thread: bool = False) -> None:
        old_loop = old_thread = None
        with self._lock:
            if self._loop is loop:
                self._loop_thread_id, self._loop_thread = threading.get_ident(), threading.current_thread()
                self._owns_loop_thread = owns_thread
                self._ready.set()
                return
            if self._owns_loop_thread and self._loop is not None:
                old_loop, old_thread = self._loop, self._loop_thread
            self._loop, self._loop_thread_id = loop, threading.get_ident()
            self._loop_thread, self._owns_loop_thread = threading.current_thread(), owns_thread
            self._ready.set()
        if old_loop is not None and old_thread is not None and old_thread.ident != threading.get_ident():
            old_loop.call_soon_threadsafe(old_loop.stop)
            old_thread.join(timeout=5)

    def clear_loop(self, expected_loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        with self._lock:
            if expected_loop is not None and self._loop is not expected_loop:
                return
            self._loop = self._loop_thread_id = self._loop_thread = None
            self._owns_loop_thread = False
            self._ready.clear()

    def _ensure_background_loop(self) -> None:
        with self._lock:
            if self._ready.is_set() and self._loop is not None:
                return
            if self._loop_thread is not None and self._loop_thread.is_alive():
                return
            started = threading.Event()

            def runner() -> None:
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

            self._loop_thread = threading.Thread(target=runner, daemon=True, name="ETMAsyncTelegramRuntime")
            self._loop_thread.start()
        if not started.wait(timeout=30):
            raise RuntimeError("Failed to start Telegram runtime loop thread.")

    def shutdown(self) -> None:
        with self._lock:
            loop = self._loop if self._owns_loop_thread else None
            thread = self._loop_thread if self._owns_loop_thread else None
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
        if not self._ready.wait(timeout=2.0):
            self._ensure_background_loop()
        with self._lock:
            loop, loop_thread_id = self._loop, self._loop_thread_id
        if loop is None:
            self._ensure_background_loop()
            with self._lock:
                loop, loop_thread_id = self._loop, self._loop_thread_id
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


def build_request(request_kwargs: Mapping[str, object]) -> HTTPXRequest:
    socket_options = request_kwargs.get("socket_options")
    return HTTPXRequest(
        connection_pool_size=cast(int, request_kwargs.get("connection_pool_size", 1)),
        read_timeout=cast(Optional[float], request_kwargs.get("read_timeout")),
        write_timeout=cast(Optional[float], request_kwargs.get("write_timeout")),
        connect_timeout=cast(Optional[float], request_kwargs.get("connect_timeout")),
        pool_timeout=cast(Optional[float], request_kwargs.get("pool_timeout")),
        media_write_timeout=cast(Optional[float], request_kwargs.get("media_write_timeout")),
        http_version=cast(Literal["1.1", "2.0", "2"], request_kwargs.get("http_version") or "1.1"),
        socket_options=cast(
            Optional[Collection[
                tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]
            ]],
            socket_options,
        ),
        proxy=cast(Optional[str], request_kwargs.get("proxy")),
        httpx_kwargs=cast(Optional[dict[str, object]], request_kwargs.get("httpx_kwargs")),
    )


class _UnusedJobQueueStub:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


class TelegramPollingRuntime:
    """Own one PTB application and its explicit polling lifecycle."""

    def __init__(
        self,
        config: Mapping[str, object],
        channel: object,
        logger: logging.Logger,
        on_started: LifecycleCallback,
        on_stopped: LifecycleCallback,
    ) -> None:
        self.logger, self._on_started, self._on_stopped = logger, on_started, on_stopped
        self._stop_event: Optional[asyncio.Event] = None
        self._shutdown_complete = threading.Event()
        self._stop_lock = threading.Lock()
        self._stopped = False
        request_config = {
            "read_timeout": 15.0,
            "connection_pool_size": self._default_connection_pool_size(config),
        }
        configured = config.get("request_kwargs")
        if isinstance(configured, Mapping):
            request_config.update(configured)
        self._request_kwargs = normalize_request_kwargs(request_config)
        identity: _BotIdentity = {
            "token": cast(str, config["token"]),
            "local_mode": bool(getattr(channel, "flag")("local_tdlib_api")),
        }
        base_url = getattr(channel, "flag")("api_base_url")
        if base_url:
            identity["base_url"] = base_url
        base_file_url = getattr(channel, "flag")("api_base_file_url")
        if base_file_url:
            identity["base_file_url"] = base_file_url
        self.async_runtime = AsyncTelegramRuntime(logger)
        self.async_bot = self._build_bot(identity)
        self.bot = SyncBotFacade(self.async_bot, self.async_runtime)
        with patch.object(ptb_applicationbuilder, "JobQueue", _UnusedJobQueueStub):
            self.application = (
                Application.builder().bot(self.async_bot).job_queue(None)
                .post_init(self._post_init).post_shutdown(self._post_shutdown).build()
            )
        self.me: telegram.User | None = None
        webhook = config.get("webhook")
        self._webhook: Mapping[str, object] | None = (
            cast(Mapping[str, object], webhook) if isinstance(webhook, Mapping) else None
        )

    @staticmethod
    def _default_connection_pool_size(config: Mapping[str, object]) -> int:
        multiplier = 2.0
        try:
            configured = float(os.getenv("ETM_HTTPX_POOL_MULTIPLIER", multiplier))
            multiplier = configured if configured > 0 else multiplier
        except ValueError:
            pass
        return max(1, int(round(8 * multiplier)))

    def _build_bot(self, identity: _BotIdentity) -> telegram.Bot:
        kwargs: _BotArguments = {
            **identity,
            "request": build_request(self._request_kwargs),
            "get_updates_request": build_request(self._request_kwargs),
        }
        return telegram.Bot(**kwargs)

    async def _post_init(self, _application: Application) -> None:
        self.async_runtime.bind_loop(asyncio.get_running_loop())
        self._shutdown_complete.clear()
        self.me = await self.async_bot.get_me()
        assert self.me, "Invalid bot credential provided."
        await self._on_started(self)
        self.logger.info("Telegram polling runtime started", extra={"event": "telegram_runtime.start"})

    async def _post_shutdown(self, _application: Application) -> None:
        try:
            await self._on_stopped(self)
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
        except Exception:
            self.logger.exception("Error during updater.stop")
        try:
            if self.application.running:
                await self.application.stop()
        except Exception:
            self.logger.exception("Error during application.stop")
        try:
            if self.application.post_stop:
                await self.application.post_stop(self.application)
        except Exception:
            self.logger.exception("Error during post_stop")
        try:
            await self.application.shutdown()
        except Exception:
            self.logger.exception("Error during application.shutdown")
        try:
            if self.application.post_shutdown:
                await self.application.post_shutdown(self.application)
        except Exception:
            self.logger.exception("Error during post_shutdown")

    def poll(self, drop_pending_updates: bool = False, timeout: int = 10) -> None:
        if self._webhook is not None:
            start_webhook = self._webhook.get("start_webhook")
            if not isinstance(start_webhook, Mapping):
                raise ValueError("webhook.start_webhook must be a mapping")
            self.application.run_webhook(
                **dict(start_webhook),
                drop_pending_updates=drop_pending_updates,
                close_loop=True,
                stop_signals=None,
            )
            return
        try:
            asyncio.run(self._run_application_lifecycle(drop_pending_updates=drop_pending_updates, timeout=timeout))
        except BaseException:
            self.logger.exception("Polling thread crashed")
            raise
        finally:
            self._stop_event = None
            self._shutdown_complete.set()

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        stop_event = self._stop_event
        if stop_event is not None:
            self.async_runtime.call_soon(stop_event.set)
        elif not self.async_runtime.call_soon(self.application.stop_running):
            try:
                self.application.stop_running()
            except RuntimeError:
                pass
        if stop_event is not None and not self._shutdown_complete.wait(timeout=30):
            self.logger.warning("Telegram post_shutdown hook did not fire within 30s.")
        self.async_runtime.shutdown()


class SyncBotFacade:
    """Expose PTB async Bot methods through synchronous wrappers."""

    def __init__(self, bot: telegram.Bot, runtime: AsyncTelegramRuntime):
        self._bot, self._runtime = bot, runtime

    def __getattr__(self, item: str) -> BotMethod:
        attr = getattr(self._bot, item)
        if not callable(attr):
            raise AttributeError(f"{type(self._bot).__name__}.{item} is not callable")

        @wraps(attr)
        def wrapper(*args: object, **kwargs: object) -> object:
            return self._runtime.call(cast(Coroutine[object, object, object], attr(*args, **kwargs)))

        return wrapper
