import datetime
from typing import List, Optional

from ehforwarderbot.types import ChatID
from peewee import IntegrityError

from .database_observability import ObservedRepository, observe_database_method
from .models import MsgLog, MsgLogIngestionLeaseLostError, MsgLogIngestionScan, database
from .utils import EFBChannelChatIDStr, chat_id_str_to_id, chat_id_to_str


class MsgLogIngestionRepository(ObservedRepository):
    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id

    def get_or_create_scan(self, source_chat_id: int, scan_boundary: int) -> MsgLogIngestionScan:
        if scan_boundary <= 0:
            raise ValueError("scan boundary must be positive")
        source_id = str(source_chat_id)
        scan = MsgLogIngestionScan.get_or_none(MsgLogIngestionScan.source_chat_id == source_id)
        if scan is not None:
            return scan
        try:
            return MsgLogIngestionScan.create(source_chat_id=source_id, scan_boundary=scan_boundary, cursor=scan_boundary)
        except IntegrityError:
            return MsgLogIngestionScan.get(MsgLogIngestionScan.source_chat_id == source_id)

    def claim_scan(self, source_chat_id: int, lease_owner: str, lease_seconds: int) -> Optional[MsgLogIngestionScan]:
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        now = datetime.datetime.now()
        lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
        with database.atomic():
            updated = (
                MsgLogIngestionScan.update(lease_owner=lease_owner, lease_expires_at=lease_expires_at, status="running", error=None, updated_at=now)
                .where(
                    (MsgLogIngestionScan.source_chat_id == str(source_chat_id))
                    & (MsgLogIngestionScan.status != "complete")
                    & (MsgLogIngestionScan.lease_expires_at.is_null(True) | (MsgLogIngestionScan.lease_expires_at <= now) | (MsgLogIngestionScan.lease_owner == lease_owner))
                )
                .execute()
            )
            if updated != 1:
                return None
            return MsgLogIngestionScan.get(MsgLogIngestionScan.source_chat_id == str(source_chat_id))

    def persist_item(
        self, scan: MsgLogIngestionScan, *, source_message_id: int, classification: str, slave_uid: Optional[EFBChannelChatIDStr] = None, message: Optional[object] = None, lease_owner: str
    ) -> str:
        now = datetime.datetime.now()
        supports_for_update = bool(getattr(database.obj, "for_update", False))
        transaction = database.atomic() if supports_for_update else database.atomic("IMMEDIATE")
        with transaction:
            query = MsgLogIngestionScan.select().where(MsgLogIngestionScan.id == scan.id)
            current = query.for_update().get() if supports_for_update else query.get()
            if current.lease_owner != lease_owner or (current.lease_expires_at is not None and current.lease_expires_at < now):
                raise MsgLogIngestionLeaseLostError("MsgLog ingestion lease is no longer owned by this worker")
            if current.status == "complete":
                return "complete"
            current.cursor = source_message_id - 1
            current.scanned_count += 1
            if classification != "eligible":
                current.skipped_count += 1
                outcome = "skipped"
            else:
                if slave_uid is None or message is None:
                    raise ValueError("eligible ingestion record is missing its topic association or content")
                master_msg_id = f"{current.source_chat_id}.{source_message_id}"
                if MsgLog.get_or_none(MsgLog.master_msg_id == master_msg_id) is not None:
                    current.existing_count += 1
                    current.existing_streak += 1
                    outcome = "existing"
                else:
                    slave_channel_id, _, _ = chat_id_str_to_id(slave_uid)
                    source_time = getattr(message, "time", None)
                    MsgLog.create(
                        master_msg_id=master_msg_id,
                        slave_message_id=f"mtproto-ingested:{master_msg_id}",
                        text=str(getattr(message, "text")),
                        slave_origin_uid=str(slave_uid),
                        slave_member_uid=str(chat_id_to_str(slave_channel_id, ChatID("__self__"))),
                        media_type=str(getattr(message, "media_type")),
                        mime=getattr(message, "mime"),
                        msg_type=str(getattr(message, "msg_type")),
                        sent_to=self.channel_id,
                        provenance="mtproto_ingested",
                        time=source_time if isinstance(source_time, datetime.datetime) else now,
                    )
                    current.inserted_count += 1
                    current.existing_streak = 0
                    outcome = "inserted"
            if current.cursor <= 0 or current.existing_streak >= 500:
                current.status = "complete"
                current.lease_owner = None
                current.lease_expires_at = None
            current.updated_at = now
            current.save()
            scan.__data__.update(current.__data__)
            return outcome

    def finish_scan(self, scan: MsgLogIngestionScan, *, status: str, error: Optional[str] = None, lease_owner: str) -> bool:
        now = datetime.datetime.now()
        with database.atomic():
            updated = (
                MsgLogIngestionScan.update(status=status, error=error, lease_owner=None, lease_expires_at=None, updated_at=now)
                .where(
                    (MsgLogIngestionScan.id == scan.id)
                    & (MsgLogIngestionScan.lease_owner == lease_owner)
                    & MsgLogIngestionScan.lease_expires_at.is_null(False)
                    & (MsgLogIngestionScan.lease_expires_at > now)
                )
                .execute()
            )
            current = MsgLogIngestionScan.get_by_id(scan.id)
            if updated == 1 or current.status == "complete":
                scan.__data__.update(current.__data__)
            return updated == 1

    @observe_database_method("get_resumable_msglog_ingestion_scans")
    def get_resumable_scans(self) -> List[MsgLogIngestionScan]:
        now = datetime.datetime.now()
        return list(
            MsgLogIngestionScan.select()
            .where(
                MsgLogIngestionScan.status.in_(("pending", "retryable-error"))
                | ((MsgLogIngestionScan.status == "running") & (MsgLogIngestionScan.lease_expires_at.is_null(True) | (MsgLogIngestionScan.lease_expires_at <= now)))
            )
            .order_by(MsgLogIngestionScan.updated_at.asc(), MsgLogIngestionScan.id.asc())
        )
