"""Telegram update handler registration and serial inbound processing."""

from __future__ import annotations

import logging
import threading
import time
from queue import Empty, Queue
from threading import Thread
from typing import Callable, Optional

from telegram import Update
from telegram.ext import CallbackContext, CommandHandler, MessageHandler

from .ptb_compat import Filters, sync_reply_text


class MasterMessageWorkerShutdownTimeout(RuntimeError):
    """The inbound message worker retained a delivery beyond its shutdown deadline."""


class MasterMessageWorker:
    """Register inbound handlers and process Telegram updates in order."""

    DEFAULT_STOP_TIMEOUT = 5.0

    def __init__(self, runtime, bot, inbound, mutations, localize: Callable[[str], str], logger: logging.Logger) -> None:
        self.runtime = runtime
        self.bot = bot
        self.inbound = inbound
        self.mutations = mutations
        self.localize = localize
        self.logger = logger
        self.message_queue: Queue[Optional[tuple[Update, CallbackContext]]] = Queue()
        self._stopping = threading.Event()
        self._queue_lock = threading.Lock()
        self._stop_sentinel_enqueued = False
        self._register_handlers()
        self.message_worker_thread = Thread(target=self.message_worker, name="ETM master messages worker thread")
        self.message_worker_thread.start()

    def _register_handlers(self) -> None:
        self.runtime.application.add_handler(CommandHandler("rm", self.runtime.as_async_callback(self.mutations.delete_message)))
        message_update_filter = Filters.update.message | Filters.update.channel_post | Filters.update.edited_message | Filters.update.edited_channel_post
        supported_filter = (
            Filters.text
            | Filters.photo
            | Filters.sticker
            | Filters.document
            | Filters.venue
            | Filters.location
            | Filters.audio
            | Filters.voice
            | Filters.video
            | Filters.animation
            | Filters.contact
            | Filters.video_note
            | Filters.dice
        )
        self.runtime.application.add_handler(MessageHandler(supported_filter & message_update_filter, self.runtime.as_async_callback(self.enqueue_message)))
        unsupported_filter = Filters.passport_data | Filters.invoice | Filters.game | Filters.successful_payment | Filters.poll
        self.runtime.application.add_handler(MessageHandler(unsupported_filter & message_update_filter, self.runtime.as_async_callback(self.mutations.unsupported_message)))

    def message_worker(self) -> None:
        while True:
            content = self.message_queue.get()
            if content is None:
                self.message_queue.task_done()
                return
            update, context = content
            try:
                self.inbound.msg(update, context)
            except Exception as error:
                self.logger.exception("Failed to process Telegram update (%s).", type(error).__name__)
                if update.effective_message and not self._stopping.is_set():
                    sync_reply_text(
                        self.bot, update.effective_message, self.localize("Unknown error has occurred while trying to process this message. See log for details.\n\n{error!r}").format(error=error)
                    )
            finally:
                self.message_queue.task_done()

    def stop_worker(self, join_timeout: Optional[float] = None) -> tuple[BaseException, ...]:
        if join_timeout is None:
            join_timeout = self.DEFAULT_STOP_TIMEOUT
        deadline = time.monotonic() + max(0.0, join_timeout)
        with self._queue_lock:
            self._stopping.set()
            drained_count = 0
            while True:
                try:
                    self.message_queue.get_nowait()
                    self.message_queue.task_done()
                    drained_count += 1
                except Empty:
                    break
            if self.message_worker_thread.is_alive() and not self._stop_sentinel_enqueued:
                self.message_queue.put(None)
                self._stop_sentinel_enqueued = True
        if drained_count:
            self.logger.info("Drained %d pending messages from queue during shutdown", drained_count)
        if self.message_worker_thread is not threading.current_thread():
            self.message_worker_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self.message_worker_thread.is_alive():
            self.logger.warning("Message worker thread did not stop within timeout")
            return (MasterMessageWorkerShutdownTimeout(f"Master message worker did not stop within {join_timeout:g}s."),)
        return ()

    def enqueue_message(self, update: Update, context: CallbackContext) -> None:
        assert isinstance(update, Update)
        with self._queue_lock:
            if self._stopping.is_set():
                return
            self.message_queue.put((update, context))
        if not self.message_worker_thread.is_alive() and update.effective_message and not self._stopping.is_set():
            sync_reply_text(self.bot, update.effective_message, self.localize("ETM message worker is not running due to unforeseen reason. This might be a bug. Please see log for details."))
