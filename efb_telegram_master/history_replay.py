"""Durable replay of persisted chat-history migration entries."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Optional

from . import utils
from .history_migration_repository import HistoryMigrationRepository
from .utils import EFBChannelChatIDStr, TelegramTopicID, TgChatMsgIDStr


def _bounded_error_message(error: BaseException) -> str:
    return str(error)[:200]


def _identity(message: str) -> str:
    return message


def history_location_text(translate: Callable[[str], str], source_storage_key: tuple[int, int]) -> str:
    source_chat_id, source_message_id = map(int, source_storage_key)
    if str(source_chat_id).startswith("-100"):
        link = f"https://t.me/c/{str(source_chat_id)[4:]}/{source_message_id}"
    elif source_chat_id < 0:
        link = f"https://t.me/{abs(source_chat_id)}/{source_message_id}"
    else:
        link = f"https://t.me/c/{source_chat_id}/{source_message_id}"
    return translate("This chat was previously linked. History messages are not migrated. You can view previous messages here: {link}").format(link=link)


class HistoryReplayWorker:
    """Queue and replay history entries without holding PTB handler threads."""

    def __init__(self, bot, msglogs, history_migrations: HistoryMigrationRepository, chat_manager, logger: logging.Logger, translate: Callable[[str], str] = _identity) -> None:
        self.bot = bot
        self.msglogs = msglogs
        self.history_migrations = history_migrations
        self.chat_manager = chat_manager
        self.logger = logger
        self._ = translate
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(
        self,
        slave_chat_id: EFBChannelChatIDStr,
        target_chat_id: int,
        thread_id: Optional[TelegramTopicID] = None,
        source_storage_key: Optional[tuple[int, int]] = None,
    ) -> None:
        threading.Thread(
            target=self._queue_and_process,
            args=(slave_chat_id, target_chat_id, thread_id, source_storage_key),
            daemon=True,
            name=f"HistoryMigration-{slave_chat_id}",
        ).start()

    def resume(self) -> None:
        try:
            if not self.history_migrations.has_pending_entries():
                return
        except Exception as error:
            self.logger.warning("Failed to check pending history migrations (%s).", type(error).__name__)
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.process_pending, daemon=True, name="HistoryMigrationResume")
        self._thread.start()

    def _queue_and_process(
        self,
        slave_chat_id: EFBChannelChatIDStr,
        target_chat_id: int,
        thread_id: Optional[TelegramTopicID],
        source_storage_key: Optional[tuple[int, int]],
    ) -> None:
        try:
            with self._lock:
                if self.queue_entries(slave_chat_id, target_chat_id, thread_id):
                    self._process_locked()
                elif source_storage_key is not None:
                    self.send_history_location(target_chat_id, thread_id, source_storage_key)
        except Exception as error:
            self.logger.error(
                "History migration failed for %s.",
                slave_chat_id,
                extra={"event": "chat_binding.history_migration_failed", "error_type": type(error).__name__, "error_message": _bounded_error_message(error)},
            )

    def queue_entries(self, slave_chat_id: EFBChannelChatIDStr, target_chat_id: int, thread_id: Optional[TelegramTopicID] = None) -> int:
        entries = []
        for position, msg_log in enumerate(self.msglogs.get_recent_messages(slave_chat_id, limit=0)):
            text = msg_log.text or ""
            formatted_text = None
            if msg_log.provenance != "mtproto_ingested" and text.strip() and not (msg_log.media_type and msg_log.media_type != "Text"):
                message = msg_log.build_etm_msg(self.chat_manager, recur=False)
                timestamp = msg_log.time.strftime("%Y-%m-%d %H:%M") if msg_log.time else "Unknown"
                author = message.author.display_name if message.author else "Unknown"
                formatted_text = f"*{author}* `{timestamp}`\n{text}\n\n"
            entries.append(
                {
                    "slave_chat_id": str(slave_chat_id),
                    "target_chat_id": str(target_chat_id),
                    "message_thread_id": str(thread_id) if thread_id is not None else None,
                    "source_master_msg_id": msg_log.master_msg_id,
                    "formatted_text": formatted_text,
                    "media_type": msg_log.media_type,
                    "source_time": msg_log.time,
                    "position": position,
                }
            )
        count = self.history_migrations.replace_entries(slave_chat_id, target_chat_id, thread_id, entries)
        self.logger.info("Queued %s historical messages for chat %s", count, slave_chat_id)
        return count

    def send_history_location(self, target_chat_id: int, thread_id: Optional[TelegramTopicID], source_storage_key: tuple[int, int]) -> None:
        kwargs: dict[str, object] = {
            "chat_id": target_chat_id,
            "text": history_location_text(self._, source_storage_key),
            "disable_notification": True,
        }
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        self.bot.send_message(**kwargs)

    def process_pending(self, block: bool = False) -> None:
        if not self._lock.acquire(blocking=block):
            return
        try:
            self._process_locked()
        finally:
            self._lock.release()

    def _process_locked(self) -> None:
        while (target := self.history_migrations.get_next_target()) is not None:
            if not self.process_target(target):
                return

    def process_target(self, target) -> bool:
        slave_chat_id = EFBChannelChatIDStr(target.slave_chat_id)
        target_chat_id = int(target.target_chat_id)
        thread_id = TelegramTopicID(int(target.message_thread_id)) if target.message_thread_id is not None else None
        entries = self.history_migrations.get_entries(slave_chat_id, target_chat_id, thread_id)
        self.logger.info("Migrating %s pending historical messages for chat %s", len(entries), slave_chat_id)
        for entry in entries:
            try:
                prepared = self.prepare_call(entry, target_chat_id, thread_id)
            except Exception as error:
                self._log_failure(entry.id, 0, error)
                return False
            if prepared is None:
                self.history_migrations.delete_entry(entry.id)
                self.logger.info("History migration entry %d completed 0 calls", entry.id)
                continue
            operation, kwargs = prepared
            try:
                getattr(self.bot, operation)(**kwargs)
            except BaseException as error:
                self._log_failure(entry.id, 0, error)
                return False
            self.history_migrations.delete_entry(entry.id)
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
            extra={"event": "chat_binding.history_migration_entry_failed", "error_type": type(error).__name__, "error_message": _bounded_error_message(error)},
        )
