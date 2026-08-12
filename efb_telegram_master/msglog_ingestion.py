"""Command-invoked MTProto ingestion of mapped forum-topic messages into MsgLog."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Optional

from .db import DatabaseManager
from .mtproto import MTProtoClient
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

    def __init__(self, db: DatabaseManager, mtproto: MTProtoClient, should_stop: Optional[Callable[[], bool]] = None) -> None:
        self.db = db
        self.mtproto = mtproto
        self.should_stop = should_stop or (lambda: False)
        self.logger = logging.getLogger(__name__)

    def _log_event(self, event: str, source_chat_id: int) -> None:
        self.logger.info("MsgLog ingestion %s for source chat %d", event, source_chat_id, extra={"event": _INGESTION_EVENT_IDS[event]})

    async def run(self, source_chat_id: int) -> None:
        """Scan a source group from the configured ceiling during this invocation."""
        scan_ceiling = getattr(getattr(self.mtproto, "config", None), "scan_ceiling", 100_000)
        if isinstance(scan_ceiling, bool) or not isinstance(scan_ceiling, int) or scan_ceiling <= 0:
            raise ValueError("MTProto scan ceiling must be a positive integer")
        if self.should_stop():
            return
        self._log_event("start", source_chat_id)
        source_channel = await self.mtproto.get_input_channel(source_chat_id)
        existing_streak = 0
        for cursor in range(scan_ceiling, 0, -self.BATCH_SIZE):
            if self.should_stop():
                return
            lower_bound = max(1, cursor - self.BATCH_SIZE + 1)
            message_ids = list(range(cursor, lower_bound - 1, -1))
            messages = await self.mtproto.get_channel_messages(source_channel, message_ids)
            by_id = {message_id: message for message in messages if (message_id := self._message_id(message)) is not None}
            for message_id in message_ids:
                if self.should_stop():
                    return
                classification, slave_uid, content = self._classify(by_id.get(message_id), source_chat_id)
                if classification != "eligible":
                    continue
                outcome = self.db.persist_ingested_msglog(source_chat_id, message_id, slave_uid, content)
                existing_streak = existing_streak + 1 if outcome == "existing" else 0
                if existing_streak >= self.EXISTING_STREAK_LIMIT:
                    self._log_event("complete", source_chat_id)
                    return
        self._log_event("complete", source_chat_id)

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
        slave_uid = self.db.get_topic_assoc_slave_uid(source_chat_id, topic_id)
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
