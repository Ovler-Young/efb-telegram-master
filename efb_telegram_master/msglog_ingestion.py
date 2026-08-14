"""Durable MTProto ingestion of mapped forum-topic messages into MsgLog."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional

from .chat_association_repository import ChatAssociationRepository
from .models import MsgLogIngestionLeaseLostError
from .msglog_ingestion_repository import MsgLogIngestionRepository
from .mtproto import MTProtoClient, MTProtoRetryableError
from .utils import EFBChannelChatIDStr

_INGESTION_EVENT_IDS = {"start": "msglog_ingestion.start", "complete": "msglog_ingestion.complete"}


@dataclass(frozen=True)
class IngestedMsgLog:
    text: str
    media_type: str
    msg_type: str
    mime: Optional[str]
    time: Optional[datetime] = None


class MsgLogIngestionService:
    """Scan one source group from its configured ceiling down to message 1."""

    BATCH_SIZE = 100
    EXISTING_STREAK_LIMIT = 500

    def __init__(self, ingestion: MsgLogIngestionRepository, chat_associations: ChatAssociationRepository, mtproto: MTProtoClient, *, lease_seconds: int = 120) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        self.ingestion = ingestion
        self.chat_associations = chat_associations
        self.mtproto = mtproto
        self.lease_seconds = lease_seconds
        self.logger = logging.getLogger(__name__)

    def _log_event(self, event: str, source_chat_id: int) -> None:
        self.logger.info("MsgLog ingestion %s for source chat %d", event, source_chat_id, extra={"event": _INGESTION_EVENT_IDS[event]})

    async def run(self, source_chat_id: int, *, lease_owner: str, stop_requested: Callable[[], bool] = lambda: False) -> None:
        """Resume a source-group scan unless another worker owns its lease."""
        scan_ceiling = getattr(getattr(self.mtproto, "config", None), "scan_ceiling", 100_000)
        if isinstance(scan_ceiling, bool) or not isinstance(scan_ceiling, int) or scan_ceiling <= 0:
            raise ValueError("MTProto scan ceiling must be a positive integer")
        if stop_requested():
            return
        self.ingestion.get_or_create_scan(source_chat_id, scan_ceiling)
        if stop_requested():
            return
        scan = self.ingestion.claim_scan(source_chat_id, lease_owner, self.lease_seconds)
        if scan is None:
            return
        self._log_event("start", source_chat_id)

        try:
            if stop_requested():
                self._release_for_shutdown(source_chat_id, lease_owner)
                return
            source_channel = await self.mtproto.get_input_channel(source_chat_id)
            if stop_requested():
                self._release_for_shutdown(source_chat_id, lease_owner)
                return
            while True:
                while scan.cursor > 0 and scan.existing_streak < self.EXISTING_STREAK_LIMIT:
                    if stop_requested():
                        self._release_for_shutdown(source_chat_id, lease_owner)
                        return
                    renewed_scan = self.ingestion.claim_scan(
                        source_chat_id,
                        lease_owner,
                        self.lease_seconds,
                    )
                    if renewed_scan is None:
                        return
                    scan = renewed_scan
                    lower_bound = max(1, scan.cursor - self.BATCH_SIZE + 1)
                    message_ids = list(range(scan.cursor, lower_bound - 1, -1))
                    messages = await self.mtproto.get_channel_messages(source_channel, message_ids)
                    if stop_requested():
                        self._release_for_shutdown(source_chat_id, lease_owner)
                        return
                    by_id = {message_id: message for message in messages if (message_id := self._message_id(message)) is not None}
                    for message_id in message_ids:
                        if stop_requested():
                            self._release_for_shutdown(source_chat_id, lease_owner)
                            return
                        classification, slave_uid, content = self._classify(
                            by_id.get(message_id),
                            source_chat_id,
                        )
                        self.ingestion.persist_item(
                            scan,
                            source_message_id=message_id,
                            classification=classification,
                            slave_uid=slave_uid,
                            message=content,
                            lease_owner=lease_owner,
                        )
                        if scan.existing_streak >= self.EXISTING_STREAK_LIMIT or scan.cursor <= 0:
                            break
                if stop_requested():
                    self._release_for_shutdown(source_chat_id, lease_owner)
                    return
                if not self.ingestion.complete_scan(scan, lease_owner=lease_owner):
                    self._log_event("complete", source_chat_id)
                    return
        except MsgLogIngestionLeaseLostError:
            self.logger.info("MsgLog ingestion lease lost for source chat %d", source_chat_id, extra={"event": "msglog_ingestion.lease_lost"})
        except MTProtoRetryableError as error:
            self.ingestion.finish_scan(
                scan,
                status="retryable-error",
                error=str(error),
                lease_owner=lease_owner,
            )
            self.logger.warning("MsgLog ingestion retained at cursor %d (%s)", scan.cursor, type(error).__name__, extra={"event": "msglog_ingestion.retry", "error_type": type(error).__name__})
        except Exception as error:
            self.ingestion.finish_scan(
                scan,
                status="error",
                error=str(error),
                lease_owner=lease_owner,
            )
            self.logger.exception("MsgLog ingestion failed at cursor %d", scan.cursor, extra={"event": "msglog_ingestion.error", "error_type": type(error).__name__})

    def _release_for_shutdown(self, source_chat_id: int, lease_owner: str) -> None:
        self.ingestion.release_scan(source_chat_id, lease_owner)

    def _classify(
        self,
        message: object,
        source_chat_id: int,
    ) -> tuple[str, Optional[EFBChannelChatIDStr], Optional[IngestedMsgLog]]:
        if message is None or type(message).__name__ == "MessageEmpty":
            return "deleted", None, None
        if getattr(message, "action", None) is not None:
            return "service", None, None
        reply_to = getattr(message, "reply_to", None)
        if reply_to is None or not getattr(reply_to, "forum_topic", False):
            return "not-topic", None, None
        topic_id = getattr(reply_to, "reply_to_top_id", None)
        if topic_id is None:
            topic_id = getattr(reply_to, "reply_to_msg_id", None)
        if isinstance(topic_id, bool) or not isinstance(topic_id, int) or topic_id <= 1:
            return "general-topic", None, None
        slave_uid = self.chat_associations.get_topic_assoc_slave_uid(source_chat_id, topic_id)
        if slave_uid is None:
            return "unbound-topic", None, None
        content = self._content(message)
        source_time = getattr(message, "date", None)
        if isinstance(source_time, datetime):
            content = replace(content, time=source_time)
        return "eligible", slave_uid, content

    @staticmethod
    def _message_id(message: object) -> Optional[int]:
        value = getattr(message, "id", None)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    @staticmethod
    def _content(message: object) -> IngestedMsgLog:
        media = getattr(message, "media", None)
        text = str(getattr(message, "message", "") or "")
        if media is not None:
            document = getattr(media, "document", None)
            mime = getattr(document, "mime_type", None)
            return IngestedMsgLog(text, "Document", "File", mime)
        return IngestedMsgLog(text, "Text", "Text", None)
