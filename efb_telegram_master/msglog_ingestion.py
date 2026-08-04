"""Durable MTProto ingestion of mapped forum-topic messages into MsgLog."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .db import DatabaseManager
from .mtproto import MTProtoClient, MTProtoRetryableError
from .utils import EFBChannelChatIDStr


@dataclass(frozen=True)
class IngestedMsgLog:
    text: str
    media_type: str
    msg_type: str
    mime: Optional[str]
    file_id: None = None
    pickle: None = None
    provenance: str = "mtproto_ingested"


class MsgLogIngestionService:
    """Scan one source group from its configured ceiling down to message 1."""

    BATCH_SIZE = 100
    EXISTING_STREAK_LIMIT = 500

    def __init__(self, db: DatabaseManager, mtproto: MTProtoClient, *, lease_seconds: int = 120) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        self.db = db
        self.mtproto = mtproto
        self.lease_seconds = lease_seconds
        self.logger = logging.getLogger(__name__)

    async def run(self, source_chat_id: int, *, lease_owner: str) -> None:
        """Resume a source-group scan unless another worker owns its lease."""
        scan_ceiling = getattr(getattr(self.mtproto, "config", None), "scan_ceiling", 100_000)
        if isinstance(scan_ceiling, bool) or not isinstance(scan_ceiling, int) or scan_ceiling <= 0:
            raise ValueError("MTProto scan ceiling must be a positive integer")
        self.db.get_or_create_msglog_ingestion_scan(source_chat_id, scan_ceiling)
        scan = self.db.claim_msglog_ingestion_scan(source_chat_id, lease_owner, self.lease_seconds)
        if scan is None:
            return

        try:
            source_channel = await self.mtproto.get_input_channel(source_chat_id)
            while scan.cursor > 0 and scan.existing_streak < self.EXISTING_STREAK_LIMIT:
                renewed_scan = self.db.claim_msglog_ingestion_scan(
                    source_chat_id, lease_owner, self.lease_seconds,
                )
                if renewed_scan is None:
                    return
                scan = renewed_scan
                lower_bound = max(1, scan.cursor - self.BATCH_SIZE + 1)
                message_ids = list(range(scan.cursor, lower_bound - 1, -1))
                messages = await self.mtproto.get_channel_messages(source_channel, message_ids)
                by_id = {
                    message_id: message for message in messages
                    if (message_id := self._message_id(message)) is not None
                }
                for message_id in message_ids:
                    classification, slave_uid, content = self._classify(
                        by_id.get(message_id), source_chat_id,
                    )
                    self.db.persist_msglog_ingestion_item(
                        scan,
                        source_message_id=message_id,
                        classification=classification,
                        slave_uid=slave_uid,
                        message=content,
                        lease_owner=lease_owner,
                    )
                    if scan.existing_streak >= self.EXISTING_STREAK_LIMIT or scan.cursor <= 0:
                        self.db.finish_msglog_ingestion_scan(
                            scan, status="complete", lease_owner=lease_owner,
                        )
                        return
            self.db.finish_msglog_ingestion_scan(scan, status="complete", lease_owner=lease_owner)
        except MTProtoRetryableError as error:
            self.db.finish_msglog_ingestion_scan(
                scan, status="retryable-error", error=str(error), lease_owner=lease_owner,
            )
            self.logger.warning("MsgLog ingestion retained at cursor %d: %s", scan.cursor, error)
        except Exception as error:
            self.db.finish_msglog_ingestion_scan(
                scan, status="error", error=str(error), lease_owner=lease_owner,
            )
            self.logger.exception("MsgLog ingestion failed at cursor %d", scan.cursor)

    def _classify(
        self, message: object, source_chat_id: int,
    ) -> tuple[str, Optional[EFBChannelChatIDStr], Optional[IngestedMsgLog]]:
        if message is None or type(message).__name__ == "MessageEmpty":
            return "deleted", None, None
        if getattr(message, "action", None) is not None:
            return "service", None, None
        reply_to = getattr(message, "reply_to", None)
        if reply_to is None or not getattr(reply_to, "forum_topic", False):
            return "not-topic", None, None
        topic_id = getattr(reply_to, "reply_to_top_id", None)
        if isinstance(topic_id, bool) or not isinstance(topic_id, int) or topic_id <= 1:
            return "general-topic", None, None
        slave_uid = self.db.get_topic_assoc_slave_uid(source_chat_id, topic_id)
        if slave_uid is None:
            return "unbound-topic", None, None
        return "eligible", slave_uid, self._content(message)

    @staticmethod
    def _message_id(message: object) -> Optional[int]:
        value = getattr(message, "id", None)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    @staticmethod
    def _content(message: object) -> IngestedMsgLog:
        media = getattr(message, "media", None)
        media_name = type(media).__name__ if media is not None else ""
        text = str(getattr(message, "message", "") or "")
        if media_name == "MessageMediaPhoto" or getattr(media, "photo", None) is not None:
            return IngestedMsgLog(text, "Photo", "Image", "image/jpeg")
        if media_name == "MessageMediaGeo" or getattr(media, "geo", None) is not None:
            return IngestedMsgLog(text or "Location", "Location", "Location", None)
        if media_name == "MessageMediaContact" or getattr(media, "phone_number", None) is not None:
            return IngestedMsgLog(text, "Contact", "Text", None)
        document = getattr(media, "document", None)
        if document is not None:
            mime = getattr(document, "mime_type", None)
            attributes = getattr(document, "attributes", ()) or ()
            attribute_names = {type(attribute).__name__ for attribute in attributes}
            if "DocumentAttributeSticker" in attribute_names:
                return IngestedMsgLog(text, "Sticker", "Sticker", mime)
            if "DocumentAttributeAnimated" in attribute_names:
                return IngestedMsgLog(text, "Animation", "Animation", mime)
            if "DocumentAttributeAudio" in attribute_names:
                voice = any(bool(getattr(attribute, "voice", False)) for attribute in attributes)
                return IngestedMsgLog(text, "Voice" if voice else "Audio", "Voice" if voice else "File", mime)
            if "DocumentAttributeVideo" in attribute_names:
                round_message = any(bool(getattr(attribute, "round_message", False)) for attribute in attributes)
                return IngestedMsgLog(text, "Video_note" if round_message else "Video", "Video", mime)
            if mime == "video/mp4":
                return IngestedMsgLog(text, "Video", "Video", mime)
            return IngestedMsgLog(text, "Document", "File", mime)
        return IngestedMsgLog(text, "Text", "Text", None)
