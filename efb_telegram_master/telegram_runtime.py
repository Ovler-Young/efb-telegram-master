"""Synchronous access to PTB's asynchronous Bot API."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import wraps
from typing import Callable, Coroutine, Optional, TypeVar, cast

import telegram

T = TypeVar("T")
BotMethod = Callable[..., object]


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
                target=runner, daemon=True, name="ETMAsyncTelegramRuntime"
            )
            self._loop_thread.start()
        if not started.wait(timeout=30):
            raise RuntimeError("Failed to start Telegram runtime loop thread.")

    def shutdown(self):
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
    """Expose PTB async Bot methods through synchronous wrappers."""

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
