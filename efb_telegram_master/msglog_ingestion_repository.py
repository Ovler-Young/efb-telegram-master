import datetime
from enum import Enum
from typing import TYPE_CHECKING, List, Optional

from ehforwarderbot.types import ChatID
from peewee import IntegrityError

from .database_observability import ObservedRepository, bind_database, observe_database_method
from .models import UTC_LEASE_CLOCK, MsgLog, MsgLogIngestionLeaseLostError, MsgLogIngestionScan, utc_now_naive
from .utils import EFBChannelChatIDStr, chat_id_str_to_id, chat_id_to_str

if TYPE_CHECKING:
    from .msglog_ingestion import IngestedMsgLog


class MsgLogIngestionCompletion(str, Enum):
    RESCAN = "rescan"
    COMPLETE = "complete"
    LEASE_LOST = "lease_lost"


class MsgLogIngestionRepository(ObservedRepository):
    def __init__(self, channel_id: str, database=None) -> None:
        super().__init__(database)
        self.channel_id = channel_id

    @staticmethod
    def _lease_expired(scan: MsgLogIngestionScan, utc_now: datetime.datetime, local_now: datetime.datetime) -> bool:
        if scan.lease_expires_at is None:
            return True
        return scan.lease_expires_at <= (utc_now if scan.lease_clock == UTC_LEASE_CLOCK else local_now)

    @staticmethod
    def _expired_lease_condition(utc_now: datetime.datetime, local_now: datetime.datetime):
        return ((MsgLogIngestionScan.lease_clock == UTC_LEASE_CLOCK) & (MsgLogIngestionScan.lease_expires_at <= utc_now)) | (
            (MsgLogIngestionScan.lease_clock.is_null(True) | (MsgLogIngestionScan.lease_clock != UTC_LEASE_CLOCK)) & (MsgLogIngestionScan.lease_expires_at <= local_now)
        )

    @bind_database
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

    @bind_database
    def claim_scan(self, source_chat_id: int, lease_owner: str, lease_seconds: int) -> Optional[MsgLogIngestionScan]:
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        utc_now = utc_now_naive()
        local_now = datetime.datetime.now()
        lease_expires_at = utc_now + datetime.timedelta(seconds=lease_seconds)
        with self.database.atomic():
            updated = (
                MsgLogIngestionScan.update(lease_owner=lease_owner, lease_expires_at=lease_expires_at, lease_clock=UTC_LEASE_CLOCK, status="running", error=None, updated_at=utc_now)
                .where(
                    (MsgLogIngestionScan.source_chat_id == str(source_chat_id))
                    & (MsgLogIngestionScan.status != "complete")
                    & (MsgLogIngestionScan.lease_expires_at.is_null(True) | self._expired_lease_condition(utc_now, local_now) | (MsgLogIngestionScan.lease_owner == lease_owner))
                )
                .execute()
            )
            if updated != 1:
                return None
            return MsgLogIngestionScan.get(MsgLogIngestionScan.source_chat_id == str(source_chat_id))

    @staticmethod
    def _reset_for_association_rescan(scan: MsgLogIngestionScan) -> None:
        scan.cursor = scan.scan_boundary
        scan.existing_streak = 0
        scan.scanned_count = 0
        scan.inserted_count = 0
        scan.existing_count = 0
        scan.skipped_count = 0
        scan.rescan_requested = False
        scan.lease_owner = None
        scan.lease_expires_at = None
        scan.status = "pending"
        scan.error = None

    @bind_database
    def request_association_rescan(self, source_chat_id: int) -> Optional[str]:
        """Durably request a follow-up after a topic becomes eligible."""
        local_now = datetime.datetime.now()
        utc_now = utc_now_naive()
        supports_for_update = bool(getattr(self.database, "for_update", False))
        transaction = self.database.atomic() if supports_for_update else self.database.atomic("IMMEDIATE")
        with transaction:
            query = MsgLogIngestionScan.select().where(MsgLogIngestionScan.source_chat_id == str(source_chat_id))
            scan = query.for_update().get_or_none() if supports_for_update else query.get_or_none()
            if scan is None:
                return None
            if scan.status == "complete":
                self._reset_for_association_rescan(scan)
            elif scan.status == "running":
                if scan.lease_owner is not None and not self._lease_expired(scan, utc_now, local_now):
                    scan.rescan_requested = True
                else:
                    self._reset_for_association_rescan(scan)
            scan.updated_at = utc_now
            scan.save()
            return scan.status

    @bind_database
    def persist_item(
        self, scan: MsgLogIngestionScan, *, source_message_id: int, classification: str, slave_uid: Optional[EFBChannelChatIDStr] = None, message: Optional["IngestedMsgLog"] = None, lease_owner: str
    ) -> str:
        local_now = datetime.datetime.now()
        utc_now = utc_now_naive()
        supports_for_update = bool(getattr(self.database, "for_update", False))
        transaction = self.database.atomic() if supports_for_update else self.database.atomic("IMMEDIATE")
        with transaction:
            query = MsgLogIngestionScan.select().where(MsgLogIngestionScan.id == scan.id)
            current = query.for_update().get() if supports_for_update else query.get()
            if current.lease_owner != lease_owner or self._lease_expired(current, utc_now, local_now):
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
                    source_time = message.time
                    MsgLog.create(
                        master_msg_id=master_msg_id,
                        slave_message_id=f"mtproto-ingested:{master_msg_id}",
                        text=str(message.text),
                        slave_origin_uid=str(slave_uid),
                        slave_member_uid=str(chat_id_to_str(slave_channel_id, ChatID("__self__"))),
                        media_type=str(message.media_type),
                        mime=message.mime,
                        msg_type=str(message.msg_type),
                        sent_to=self.channel_id,
                        provenance="mtproto_ingested",
                        time=source_time if isinstance(source_time, datetime.datetime) else utc_now_naive(),
                    )
                    current.inserted_count += 1
                    current.existing_streak = 0
                    outcome = "inserted"
            current.updated_at = utc_now
            current.save()
            scan.__data__.update(current.__data__)
            return outcome

    @bind_database
    def finish_scan(self, scan: MsgLogIngestionScan, *, status: str, error: Optional[str] = None, lease_owner: str) -> bool:
        local_now = datetime.datetime.now()
        utc_now = utc_now_naive()
        with self.database.atomic():
            updated = (
                MsgLogIngestionScan.update(status=status, error=error, lease_owner=None, lease_expires_at=None, updated_at=utc_now)
                .where(
                    (MsgLogIngestionScan.id == scan.id)
                    & (MsgLogIngestionScan.lease_owner == lease_owner)
                    & MsgLogIngestionScan.lease_expires_at.is_null(False)
                    & ~self._expired_lease_condition(utc_now, local_now)
                )
                .execute()
            )
            current = MsgLogIngestionScan.get_by_id(scan.id)
            if updated == 1 or current.status == "complete":
                scan.__data__.update(current.__data__)
            return updated == 1

    @bind_database
    def complete_scan(self, scan: MsgLogIngestionScan, *, lease_owner: str) -> MsgLogIngestionCompletion:
        """Complete the current pass and retain its lease for a requested rescan."""
        local_now = datetime.datetime.now()
        utc_now = utc_now_naive()
        supports_for_update = bool(getattr(self.database, "for_update", False))
        transaction = self.database.atomic() if supports_for_update else self.database.atomic("IMMEDIATE")
        with transaction:
            query = MsgLogIngestionScan.select().where(MsgLogIngestionScan.id == scan.id)
            current = query.for_update().get() if supports_for_update else query.get()
            if current.lease_owner != lease_owner or self._lease_expired(current, utc_now, local_now):
                return MsgLogIngestionCompletion.LEASE_LOST
            if current.rescan_requested:
                current.cursor = current.scan_boundary
                current.existing_streak = 0
                current.scanned_count = 0
                current.inserted_count = 0
                current.existing_count = 0
                current.skipped_count = 0
                current.rescan_requested = False
                current.error = None
                current.updated_at = utc_now
                current.save()
                scan.__data__.update(current.__data__)
                return MsgLogIngestionCompletion.RESCAN
            current.status = "complete"
            current.lease_owner = None
            current.lease_expires_at = None
            current.updated_at = utc_now
            current.save()
            scan.__data__.update(current.__data__)
            return MsgLogIngestionCompletion.COMPLETE

    @bind_database
    def release_scan(self, source_chat_id: int, lease_owner: str) -> bool:
        """Make a shutdown-interrupted scan resumable without changing its cursor."""
        utc_now = utc_now_naive()
        with self.database.atomic():
            return (
                MsgLogIngestionScan.update(status="pending", error="shutdown", lease_owner=None, lease_expires_at=None, updated_at=utc_now)
                .where((MsgLogIngestionScan.source_chat_id == str(source_chat_id)) & (MsgLogIngestionScan.status != "complete") & (MsgLogIngestionScan.lease_owner == lease_owner))
                .execute()
                == 1
            )

    @observe_database_method("get_resumable_msglog_ingestion_scans")
    def get_resumable_scans(self) -> List[MsgLogIngestionScan]:
        now = datetime.datetime.now()
        utc_now = utc_now_naive()
        return list(
            MsgLogIngestionScan.select()
            .where(
                MsgLogIngestionScan.status.in_(("pending", "retryable-error"))
                | ((MsgLogIngestionScan.status == "running") & (MsgLogIngestionScan.lease_expires_at.is_null(True) | self._expired_lease_condition(utc_now, now)))
            )
            .order_by(MsgLogIngestionScan.updated_at.asc(), MsgLogIngestionScan.id.asc())
        )
