"""Bounded, durable recovery of messages from a non-General forum topic."""

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Any, Optional

from .db import DatabaseManager
from .bot_manager import TelegramBotManager, TopicRecoveryQueueContext
from .mtproto import MTProtoRetryableError
from .utils import EFBChannelChatIDStr


@dataclass(frozen=True)
class TopicRecoveryRequest:
    source_chat_id: int
    source_thread_id: int
    target_chat_id: int
    target_thread_id: int
    slave_chat_id: EFBChannelChatIDStr
    scan_boundary: int


class TopicHistoryRecovery:
    """Recover one topic in ascending, idempotent MTProto batches."""

    def __init__(self, db: DatabaseManager, bot: Any, mtproto: Any, runtime: Any) -> None:
        self.db = db
        self.bot = bot
        self.mtproto = mtproto
        self.runtime = runtime
        self.logger = logging.getLogger(__name__)

    def recover(self, request: TopicRecoveryRequest) -> None:
        prepared_request = self.prepare(request)
        if prepared_request is None:
            return
        self.recover_prepared(prepared_request)

    def prepare(self, request: TopicRecoveryRequest) -> Optional[TopicRecoveryRequest]:
        """Persist a bounded recovery request before checking the MTProto connection."""
        if request.source_thread_id <= 1:
            raise ValueError("General or ambiguous forum topic cannot be recovered")
        scan_ceiling = getattr(getattr(self.mtproto, "config", None), "scan_ceiling", None)
        if isinstance(scan_ceiling, int) and not isinstance(scan_ceiling, bool) and scan_ceiling > 0:
            request = replace(request, scan_boundary=min(request.scan_boundary, scan_ceiling))
        if request.scan_boundary <= 0:
            return None
        self.db.get_or_create_topic_recovery_scan(
            source_chat_id=request.source_chat_id,
            source_thread_id=request.source_thread_id,
            target_chat_id=request.target_chat_id,
            target_thread_id=request.target_thread_id,
            slave_chat_id=request.slave_chat_id,
            scan_boundary=request.scan_boundary,
        )
        return request

    def recover_prepared(self, request: TopicRecoveryRequest) -> None:
        self.runtime.call(self._recover(request))

    async def _recover(self, request: TopicRecoveryRequest) -> None:
        scan = self.db.get_or_create_topic_recovery_scan(
            source_chat_id=request.source_chat_id,
            source_thread_id=request.source_thread_id,
            target_chat_id=request.target_chat_id,
            target_thread_id=request.target_thread_id,
            slave_chat_id=request.slave_chat_id,
            scan_boundary=request.scan_boundary,
        )
        try:
            source_channel = await self.mtproto.get_input_channel(request.source_chat_id)
            while scan.cursor < scan.scan_boundary:
                upper = min(scan.cursor + 100, scan.scan_boundary)
                ids = list(range(scan.cursor + 1, upper + 1))
                messages = await self.mtproto.get_channel_messages(source_channel, ids)
                indexed = {self._message_id(message): message for message in messages if self._message_id(message)}
                for message_id in ids:
                    await self._recover_one(scan, request, message_id, indexed.get(message_id))
                    self.db.advance_topic_recovery_scan(scan, message_id)
                scan.cursor = upper
            self.db.advance_topic_recovery_scan(scan, scan.cursor, status="complete")
        except BaseException as error:
            status = "retryable-error" if isinstance(error, MTProtoRetryableError) else "error"
            self.db.advance_topic_recovery_scan(scan, scan.cursor, status=status, error=str(error))
            self.logger.warning("Topic history recovery retained at cursor %s: %s", scan.cursor, error)

    async def _recover_one(self, scan: Any, request: TopicRecoveryRequest, message_id: int, message: object) -> None:
        key = f"{scan.id}:{message_id}"
        existing = self.db.get_topic_recovery_entry(scan.id, message_id)
        if existing is not None and existing.status in {"accepted", "skipped"}:
            return
        if existing is not None and existing.status == "delivered":
            self._record_msglog(request, message_id, message, existing.target_message_id)
            self.db.save_topic_recovery_entry(
                scan_id=scan.id, source_message_id=message_id, classification="accepted",
                status="accepted", idempotency_key=key,
                target_message_id=existing.target_message_id,
            )
            return
        classification = self._classify(message, request.source_thread_id)
        if classification != "accepted":
            self.db.save_topic_recovery_entry(
                scan_id=scan.id, source_message_id=message_id, classification=classification,
                status="skipped", idempotency_key=key,
            )
            return

        source_log = self.db.get_msg_log(master_msg_id=f"{request.source_chat_id}.{message_id}")
        if source_log is not None:
            self.db.save_topic_recovery_entry(
                scan_id=scan.id, source_message_id=message_id, classification="existing-msglog",
                status="skipped", idempotency_key=key,
            )
            return

        queue_id = getattr(existing, "delivery_queue_id", None) if existing is not None else None
        if queue_id is None:
            queue_id = f"topic-recovery:{scan.id}:{message_id}"
        self.db.save_topic_recovery_entry(
            scan_id=scan.id, source_message_id=message_id, classification="accepted",
            status="prepared", idempotency_key=key, delivery_queue_id=queue_id,
        )
        try:
            context = TelegramBotManager.encode_topic_recovery_log_context(
                TopicRecoveryQueueContext(
                    scan_id=scan.id, source_chat_id=request.source_chat_id,
                    source_message_id=message_id, target_chat_id=request.target_chat_id,
                    slave_chat_id=str(request.slave_chat_id),
                    text=str(getattr(message, "message", "") or ""), idempotency_key=key,
                )
            )
            waiter = self.bot.enqueue_history_operation(
                source_key=str(request.slave_chat_id), target_chat_id=request.target_chat_id,
                operation="copy_message", args=(),
                kwargs={
                    "chat_id": request.target_chat_id,
                    "from_chat_id": request.source_chat_id,
                    "message_id": message_id,
                    "message_thread_id": request.target_thread_id,
                    "disable_notification": True,
                },
                history_entry_ids=[],
                log_context=context,
                queue_id=queue_id,
            )
            receipt = await asyncio.wrap_future(waiter)
            target_message_id = self._receipt_message_id(receipt)
        except BaseException as error:
            self.db.save_topic_recovery_entry(
                scan_id=scan.id, source_message_id=message_id, classification="accepted",
                status="prepared", idempotency_key=key, error=str(error),
                delivery_queue_id=queue_id,
            )
            if isinstance(error, MTProtoRetryableError):
                raise
            raise MTProtoRetryableError(str(error)) from error
        existing = self.db.get_topic_recovery_entry(scan.id, message_id)
        if existing is None or existing.status != "accepted":
            self.db.save_topic_recovery_entry(
                scan_id=scan.id, source_message_id=message_id, classification="accepted",
                status="delivered", idempotency_key=key, target_message_id=target_message_id,
            )
            self._record_msglog(request, message_id, message, target_message_id)
            self.db.save_topic_recovery_entry(
                scan_id=scan.id, source_message_id=message_id, classification="accepted",
                status="accepted", idempotency_key=key, target_message_id=target_message_id,
            )

    def _record_msglog(
        self, request: TopicRecoveryRequest, message_id: int, message: object,
        target_message_id: Optional[int],
    ) -> None:
        self.db.add_topic_recovery_msg_log(
            source_chat_id=request.source_chat_id, source_message_id=message_id,
            target_chat_id=request.target_chat_id, target_message_id=target_message_id,
            slave_chat_id=request.slave_chat_id,
            text=str(getattr(message, "message", "") or ""),
        )

    @staticmethod
    def _message_id(message: object) -> Optional[int]:
        value = getattr(message, "id", None)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _receipt_message_id(receipt: object) -> Optional[int]:
        message = getattr(receipt, "message", receipt)
        value = getattr(message, "message_id", getattr(message, "id", None))
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @staticmethod
    def _classify(message: object, expected_top_id: int) -> str:
        if message is None or type(message).__name__ == "MessageEmpty":
            return "deleted"
        if getattr(message, "action", None) is not None:
            return "service"
        if getattr(message, "noforwards", False) or getattr(message, "protected", False):
            return "protected"
        if getattr(message, "forwards_restricted", False):
            return "unforwardable"
        reply = getattr(message, "reply_to", None)
        if reply is None or not getattr(reply, "forum_topic", False):
            return "ambiguous-topic"
        top_id = getattr(reply, "reply_to_top_id", None)
        if not isinstance(top_id, int) or isinstance(top_id, bool) or top_id <= 1:
            return "general-topic"
        if top_id != expected_top_id:
            return "cross-topic"
        if not hasattr(message, "id"):
            return "unforwardable"
        return "accepted"
