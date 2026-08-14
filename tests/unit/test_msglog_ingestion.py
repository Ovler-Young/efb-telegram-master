import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from peewee import SqliteDatabase

from efb_telegram_master.models import MsgLog, MsgLogIngestionLeaseLostError, MsgLogIngestionScan, database
from efb_telegram_master.msglog_ingestion import MsgLogIngestionService
from efb_telegram_master.msglog_ingestion_repository import MsgLogIngestionRepository
from efb_telegram_master.mtproto import MTProtoRetryableError


@dataclass
class Scan:
    id: int = 1
    source_chat_id: str = "100"
    scan_boundary: int = 0
    cursor: int = 0
    existing_streak: int = 0
    status: str = "pending"
    error: str | None = None


class FakeDatabase:
    def __init__(self, scan_boundary=205):
        self.scan = Scan(scan_boundary=scan_boundary, cursor=scan_boundary)
        self.chat_associations = FakeChatAssociations({10: "tests.slave"})
        self.msglog_ingestion = self
        self.persisted = []
        self.claims = []

    def get_or_create_scan(self, source_chat_id, scan_boundary):
        assert source_chat_id == 100
        assert scan_boundary == self.scan.scan_boundary
        return self.scan

    def claim_scan(self, source_chat_id, lease_owner, lease_seconds):
        self.claims.append((source_chat_id, lease_owner, lease_seconds))
        return self.scan if self.scan.status != "complete" else None

    def persist_item(self, scan, *, source_message_id, classification, slave_uid=None, message=None, lease_owner):
        self.persisted.append((source_message_id, classification, slave_uid, message))
        scan.cursor = source_message_id - 1
        if classification == "eligible":
            if source_message_id in {503, 502}:
                scan.existing_streak += 1
                return "existing"
            scan.existing_streak = 0
            return "inserted"
        return "skipped"

    def finish_scan(self, scan, *, status, error=None, lease_owner):
        scan.status = status
        scan.error = error

    def complete_scan(self, scan, *, lease_owner):
        scan.status = "complete"
        return False

    def release_scan(self, source_chat_id, lease_owner):
        assert source_chat_id == 100
        self.scan.status = "pending"
        self.scan.error = "shutdown"


class FakeChatAssociations:
    def __init__(self, associations):
        self.associations = associations

    def get_topic_assoc_slave_uid(self, source_chat_id, topic_id):
        assert source_chat_id == 100
        return self.associations.get(topic_id)


class FakeMTProto:
    def __init__(self, messages, error=None, scan_ceiling=205):
        self.config = SimpleNamespace(scan_ceiling=scan_ceiling)
        self.messages = messages
        self.error = error
        self.calls = []

    async def get_input_channel(self, source_chat_id):
        return source_chat_id

    async def get_channel_messages(self, channel, message_ids):
        self.calls.append((channel, list(message_ids)))
        if self.error is not None:
            raise self.error
        return [self.messages[message_id] for message_id in message_ids if message_id in self.messages]


def topic_message(message_id, *, topic_id=10, media=None, date=None, topic_root=False):
    return SimpleNamespace(
        id=message_id,
        message=f"message {message_id}",
        date=date,
        reply_to=SimpleNamespace(
            forum_topic=True,
            reply_to_top_id=None if topic_root else topic_id,
            reply_to_msg_id=topic_id if topic_root else None,
        ),
        action=None,
        media=media,
    )


def test_ingestion_descends_in_hundred_id_batches_and_stores_mapped_messages():
    db = FakeDatabase()
    ordinary_reply = SimpleNamespace(
        id=4,
        message="ordinary reply",
        reply_to=SimpleNamespace(forum_topic=False, reply_to_top_id=None, reply_to_msg_id=10),
        action=None,
        media=None,
    )
    mtproto = FakeMTProto(
        {
            205: topic_message(205),
            104: topic_message(104, topic_root=True),
            4: ordinary_reply,
        }
    )

    asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert [ids for _, ids in mtproto.calls] == [
        list(range(205, 105, -1)),
        list(range(105, 5, -1)),
        list(range(5, 0, -1)),
    ]
    accepted = [entry for entry in db.persisted if entry[1] == "eligible"]
    assert [(entry[0], entry[2]) for entry in accepted] == [
        (205, "tests.slave"),
        (104, "tests.slave"),
    ]
    ordinary_reply_entry = next(entry for entry in db.persisted if entry[0] == 4)
    assert ordinary_reply_entry[1:] == ("not-topic", None, None)
    assert db.scan.status == "complete"


def test_association_rescan_restarts_completed_scan_and_repeats_active_lease():
    class Associations:
        def __init__(self):
            self.slave_uid = None
            self.requested_during_active_lease = False

        def get_topic_assoc_slave_uid(self, source_chat_id, topic_id):
            assert (source_chat_id, topic_id) == (100, 10)
            if self.slave_uid is not None and not self.requested_during_active_lease:
                self.requested_during_active_lease = True
                assert ingestion.request_association_rescan(100) == "running"
            return self.slave_uid

    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    ingestion = MsgLogIngestionRepository("tests.master")
    associations = Associations()
    mtproto = FakeMTProto({1: topic_message(1)}, scan_ceiling=1)
    service = MsgLogIngestionService(ingestion, associations, mtproto)
    try:
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
        asyncio.run(service.run(100, lease_owner="worker-a"))

        completed = MsgLogIngestionScan.get(MsgLogIngestionScan.source_chat_id == "100")
        assert completed.status == "complete"
        assert MsgLog.select().count() == 0

        associations.slave_uid = "tests.slave target"
        assert ingestion.request_association_rescan(100) == "pending"
        asyncio.run(service.run(100, lease_owner="worker-b"))

        scan = MsgLogIngestionScan.get_by_id(completed.id)
        row = MsgLog.get_by_id("100.1")
    finally:
        test_db.close()
        database.initialize(original_database)

    assert associations.requested_during_active_lease
    assert [ids for _channel, ids in mtproto.calls] == [[1], [1], [1]]
    assert (scan.status, scan.lease_owner, scan.rescan_requested) == ("complete", None, False)
    assert (row.provenance, row.slave_origin_uid) == ("mtproto_ingested", "tests.slave target")


def test_ingestion_skips_are_neutral_and_existing_streak_completes_at_500():
    db = FakeDatabase(scan_boundary=503)
    db.persist_item = lambda scan, **kwargs: _five_hundred_rule(db, scan, **kwargs)
    mtproto = FakeMTProto(
        {message_id: topic_message(message_id) for message_id in range(1, 504)},
        scan_ceiling=503,
    )
    mtproto.messages[502] = SimpleNamespace(id=502, message="service", action=object(), reply_to=None)

    asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert db.scan.status == "complete"
    assert db.scan.cursor == 2
    assert db.scan.existing_streak == 500


def _five_hundred_rule(db, scan, *, source_message_id, classification, **_kwargs):
    db.persisted.append((source_message_id, classification, None, None))
    scan.cursor = source_message_id - 1
    if classification == "eligible":
        scan.existing_streak += 1
        return "existing"
    return "skipped"


def test_ingestion_collapses_media_to_generic_copyable_content():
    db = FakeDatabase(scan_boundary=1)
    media = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4", attributes=[]))
    source_time = datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc)
    mtproto = FakeMTProto({1: topic_message(1, media=media, date=source_time)}, scan_ceiling=1)

    asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    stored = db.persisted[0][3]
    assert stored.media_type == "Document"
    assert stored.msg_type == "File"
    assert stored.mime == "video/mp4"
    assert stored.time == source_time


def test_ingestion_marks_transient_mtproto_failure_for_retry_without_advancing_cursor(caplog):
    db = FakeDatabase(scan_boundary=5)
    mtproto = FakeMTProto({}, error=MTProtoRetryableError("temporary"), scan_ceiling=5)

    with caplog.at_level(logging.INFO, logger="efb_telegram_master.msglog_ingestion"):
        asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert db.scan.status == "retryable-error"
    assert db.scan.error == "temporary"
    assert db.scan.cursor == 5
    assert [(record.event, getattr(record, "error_type", None)) for record in caplog.records] == [
        ("msglog_ingestion.start", None),
        ("msglog_ingestion.retry", "MTProtoRetryableError"),
    ]


def test_ingestion_stops_after_blocked_fetch_without_persisting_or_fetching_again():
    db = FakeDatabase(scan_boundary=2)
    stop_requested = False
    fetched = asyncio.Event()
    release = asyncio.Event()

    class BlockingMTProto(FakeMTProto):
        async def get_channel_messages(self, channel, message_ids):
            self.calls.append((channel, list(message_ids)))
            fetched.set()
            await release.wait()
            return [topic_message(message_ids[0])]

    mtproto = BlockingMTProto({}, scan_ceiling=2)

    async def run():
        nonlocal stop_requested
        task = asyncio.create_task(MsgLogIngestionService(db, db.chat_associations, mtproto).run(100, lease_owner="worker-a", stop_requested=lambda: stop_requested))
        await fetched.wait()
        stop_requested = True
        release.set()
        await task

    asyncio.run(run())

    assert db.persisted == []
    assert len(mtproto.calls) == 1
    assert db.scan.status == "pending"
    assert db.scan.cursor == 2


def test_ingestion_already_stopped_performs_no_database_or_mtproto_work():
    ingestion = SimpleNamespace(
        get_or_create_scan=Mock(),
        claim_scan=Mock(),
        persist_item=Mock(),
        finish_scan=Mock(),
        release_scan=Mock(),
    )
    mtproto = SimpleNamespace(config=SimpleNamespace(scan_ceiling=1), get_input_channel=Mock(), get_channel_messages=Mock())

    asyncio.run(MsgLogIngestionService(ingestion, Mock(), mtproto).run(100, lease_owner="worker-a", stop_requested=lambda: True))

    for method in (
        ingestion.get_or_create_scan,
        ingestion.claim_scan,
        ingestion.persist_item,
        ingestion.finish_scan,
        ingestion.release_scan,
        mtproto.get_input_channel,
        mtproto.get_channel_messages,
    ):
        method.assert_not_called()


def test_ingestion_logs_lease_loss_with_a_stable_event(caplog):
    db = FakeDatabase(scan_boundary=5)
    mtproto = FakeMTProto({}, scan_ceiling=5)

    async def lose_lease(_source_chat_id):
        raise MsgLogIngestionLeaseLostError()

    mtproto.get_input_channel = lose_lease
    with caplog.at_level(logging.INFO, logger="efb_telegram_master.msglog_ingestion"):
        asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert [(record.event, getattr(record, "error_type", None)) for record in caplog.records] == [
        ("msglog_ingestion.start", None),
        ("msglog_ingestion.lease_lost", None),
    ]


def test_ingestion_logs_unexpected_failure_with_error_type(caplog):
    db = FakeDatabase(scan_boundary=5)
    mtproto = FakeMTProto({}, scan_ceiling=5)

    async def fail(_source_chat_id):
        raise ValueError("failed")

    mtproto.get_input_channel = fail
    with caplog.at_level(logging.INFO, logger="efb_telegram_master.msglog_ingestion"):
        asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert db.scan.status == "error"
    assert [(record.event, getattr(record, "error_type", None)) for record in caplog.records] == [
        ("msglog_ingestion.start", None),
        ("msglog_ingestion.error", "ValueError"),
    ]
