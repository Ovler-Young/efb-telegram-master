"""Durable replay of persisted chat-history migration entries."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from typing import Optional

from . import utils
from .history_migration_repository import HistoryMigrationRepository
from .utils import EFBChannelChatIDStr, TelegramTopicID, TgChatMsgIDStr, bounded_error_message


def _identity(message: str) -> str:
    return message


def history_location_url(source_storage_key: tuple[int, int]) -> Optional[str]:
    source_chat_id, source_message_id = map(int, source_storage_key)
    if str(source_chat_id).startswith("-100"):
        return f"https://t.me/c/{str(source_chat_id)[4:]}/{source_message_id}"
    return None


def history_location_text(translate: Callable[[str], str], source_storage_key: tuple[int, int]) -> str:
    link = history_location_url(source_storage_key)
    if link is None:
        return translate("This chat was previously linked. History messages are not migrated.")
    return translate("This chat was previously linked. History messages are not migrated. You can view previous messages here: {link}").format(link=link)


class HistoryReplayShutdownTimeout(RuntimeError):
    """The replay loop retained a Bot API call beyond its shutdown deadline."""


class HistoryReplayWorker:
    """Own one replay loop that drains durable migration entries."""

    DEFAULT_JOIN_TIMEOUT = 5.0
    SOURCE_PAGE_SIZE = 100
    REPLAY_PAGE_SIZE = 100

    def __init__(self, bot, msglogs, history_migrations: HistoryMigrationRepository, message_reconstructor, logger: logging.Logger, translate: Callable[[str], str] = _identity) -> None:
        self.bot, self.msglogs, self.history_migrations, self.message_reconstructor = bot, msglogs, history_migrations, message_reconstructor
        self.logger, self._ = logger, translate
        self._process_lock = threading.Lock()
        self._condition = threading.Condition()
        self._requests: dict[tuple[str, int, Optional[int]], tuple[EFBChannelChatIDStr, int, Optional[TelegramTopicID], Optional[tuple[int, int]]]] = {}
        self._resume_requested = False
        self._stopping = False
        self._thread: Optional[threading.Thread] = None
        self._active_target: Optional[str] = None

    def start(self, slave_chat_id: EFBChannelChatIDStr, target_chat_id: int, thread_id: Optional[TelegramTopicID] = None, source_storage_key: Optional[tuple[int, int]] = None) -> bool:
        key = (str(slave_chat_id), target_chat_id, int(thread_id) if thread_id is not None else None)
        with self._condition:
            if self._stopping:
                return False
            self._requests[key] = (slave_chat_id, target_chat_id, thread_id, source_storage_key)
            self._ensure_loop_locked()
            self._condition.notify()
        return True

    def resume(self) -> bool:
        try:
            pending = self.history_migrations.has_pending_entries()
        except Exception as error:
            self.logger.warning("Failed to check pending history migrations (%s).", type(error).__name__)
            return False
        if not pending:
            return False
        with self._condition:
            if self._stopping:
                return False
            self._resume_requested = True
            self._ensure_loop_locked()
            self._condition.notify()
        return True

    def stop(self, join_timeout: float = DEFAULT_JOIN_TIMEOUT) -> tuple[BaseException, ...]:
        deadline = time.monotonic() + join_timeout
        with self._condition:
            self._stopping = True
            self._requests.clear()
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
        if thread is not None and thread.is_alive():
            target = self._active_target or "durable queue"
            return (HistoryReplayShutdownTimeout(f"History replay worker did not stop within {join_timeout:g}s (active target: {target})."),)
        return ()

    def _ensure_loop_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="HistoryMigrationReplay")
            self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._stopping and not self._requests and not self._resume_requested:
                        self._condition.wait()
                    if self._stopping:
                        return
                    requests = tuple(self._requests.values())
                    self._requests.clear()
                    self._resume_requested = False
                for request in requests:
                    self._queue_and_process(*request)
                self.process_pending(block=True)
        finally:
            with self._condition:
                self._active_target = None
                self._condition.notify_all()

    def _queue_and_process(self, slave_chat_id: EFBChannelChatIDStr, target_chat_id: int, thread_id: Optional[TelegramTopicID], source_storage_key: Optional[tuple[int, int]]) -> None:
        target = f"{slave_chat_id}->{target_chat_id}:{thread_id if thread_id is not None else 'root'}"
        self._active_target = target
        try:
            if self.queue_entries(slave_chat_id, target_chat_id, thread_id):
                self.process_pending(block=True)
            elif source_storage_key is not None:
                self.send_history_location(target_chat_id, thread_id, source_storage_key)
        except Exception as error:
            self.logger.error(
                "History migration failed for %s.",
                slave_chat_id,
                extra={"event": "chat_binding.history_migration_failed", "error_type": type(error).__name__, "error_message": bounded_error_message(error)},
            )
        finally:
            self._active_target = None

    def queue_entries(self, slave_chat_id: EFBChannelChatIDStr, target_chat_id: int, thread_id: Optional[TelegramTopicID] = None) -> int:
        def entries() -> Iterator[dict[str, object]]:
            after = None
            position = 0
            while page := self.msglogs.get_recent_message_page(slave_chat_id, after, self.SOURCE_PAGE_SIZE):
                for msg_log in page:
                    text = msg_log.text or ""
                    formatted_text = None
                    if msg_log.provenance != "mtproto_ingested" and text.strip() and not (msg_log.media_type and msg_log.media_type != "Text"):
                        message = self.message_reconstructor.build(msg_log, recur=False)
                        timestamp = msg_log.time.strftime("%Y-%m-%d %H:%M") if msg_log.time else "Unknown"
                        author = message.author.display_name if message.author else "Unknown"
                        formatted_text = f"*{author}* `{timestamp}`\n{text}\n\n"
                    yield {
                        "slave_chat_id": str(slave_chat_id),
                        "target_chat_id": str(target_chat_id),
                        "message_thread_id": str(thread_id) if thread_id is not None else None,
                        "source_master_msg_id": msg_log.master_msg_id,
                        "formatted_text": formatted_text,
                        "media_type": msg_log.media_type,
                        "source_time": msg_log.time,
                        "position": position,
                    }
                    position += 1
                last = page[-1]
                after = (last.time, last.master_msg_id)

        count = self.history_migrations.replace_entries(slave_chat_id, target_chat_id, thread_id, entries())
        self.logger.info("Queued %s historical messages for chat %s", count, slave_chat_id)
        return count

    def send_history_location(self, target_chat_id: int, thread_id: Optional[TelegramTopicID], source_storage_key: tuple[int, int]) -> None:
        kwargs: dict[str, object] = {"chat_id": target_chat_id, "text": history_location_text(self._, source_storage_key), "disable_notification": True}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        self.bot.send_message(**kwargs)

    def process_pending(self, block: bool = False) -> None:
        if not self._process_lock.acquire(blocking=block):
            return
        try:
            while not self._stopping and (target := self.history_migrations.get_next_target()) is not None:
                self._active_target = f"{target.slave_chat_id}->{target.target_chat_id}:{target.message_thread_id or 'root'}"
                if not self.process_target(target):
                    return
        finally:
            self._active_target = None
            self._process_lock.release()

    def process_target(self, target) -> bool:
        slave_chat_id = EFBChannelChatIDStr(target.slave_chat_id)
        target_chat_id = int(target.target_chat_id)
        thread_id = TelegramTopicID(int(target.message_thread_id)) if target.message_thread_id is not None else None
        after = None
        logged = False
        while True:
            entries = self.history_migrations.get_entries_page(slave_chat_id, target_chat_id, thread_id, after, self.REPLAY_PAGE_SIZE)
            if not entries:
                return True
            if not logged:
                self.logger.info("Migrating pending historical messages for chat %s", slave_chat_id)
                logged = True
            for entry in entries:
                after = (entry.position, entry.id)
                try:
                    prepared = self.prepare_call(entry, target_chat_id, thread_id)
                    if prepared is None:
                        self.history_migrations.delete_entry(entry.id)
                        self.logger.info("History migration entry %d completed 0 calls", entry.id)
                        continue
                    operation, kwargs = prepared
                    getattr(self.bot, operation)(**kwargs)
                    self.history_migrations.delete_entry(entry.id)
                except BaseException as error:
                    self._log_failure(entry.id, 0, error)
                    return False
        return True

    @staticmethod
    def prepare_call(entry, target_chat_id: int, thread_id: Optional[TelegramTopicID]) -> Optional[tuple[str, dict[str, object]]]:
        if entry.formatted_text == "":
            return None
        if entry.formatted_text is not None:
            kwargs: dict[str, object] = {"chat_id": target_chat_id, "text": entry.formatted_text, "parse_mode": "Markdown", "disable_notification": True}
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            return "send_message", kwargs
        original_chat_id, original_msg_id = utils.message_id_str_to_id(TgChatMsgIDStr(entry.source_master_msg_id))
        kwargs = {"chat_id": target_chat_id, "from_chat_id": original_chat_id, "message_id": original_msg_id, "disable_notification": True}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        return "copy_message", kwargs

    def _log_failure(self, entry_id: int, completed_call_count: int, error: BaseException) -> None:
        self.logger.warning(
            "History migration entry %d retained after %d completed calls.",
            entry_id,
            completed_call_count,
            extra={"event": "chat_binding.history_migration_entry_failed", "error_type": type(error).__name__, "error_message": bounded_error_message(error)},
        )
