import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from efb_telegram_master.msglog_ingestion import MsgLogIngestionService
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
        self.associations = {10: "tests.slave"}
        self.persisted = []
        self.claims = []

    def get_or_create_msglog_ingestion_scan(self, source_chat_id, scan_boundary):
        assert source_chat_id == 100
        assert scan_boundary == self.scan.scan_boundary
        return self.scan

    def claim_msglog_ingestion_scan(self, source_chat_id, lease_owner, lease_seconds):
        self.claims.append((source_chat_id, lease_owner, lease_seconds))
        return self.scan if self.scan.status != "complete" else None

    def get_topic_assoc_slave_uid(self, source_chat_id, topic_id):
        assert source_chat_id == 100
        return self.associations.get(topic_id)

    def persist_msglog_ingestion_item(self, scan, *, source_message_id, classification,
                                      slave_uid=None, message=None, lease_owner):
        self.persisted.append((source_message_id, classification, slave_uid, message))
        scan.cursor = source_message_id - 1
        if classification == "eligible":
            if source_message_id in {503, 502}:
                scan.existing_streak += 1
                return "existing"
            scan.existing_streak = 0
            return "inserted"
        return "skipped"

    def finish_msglog_ingestion_scan(self, scan, *, status, error=None, lease_owner):
        scan.status = status
        scan.error = error


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
    mtproto = FakeMTProto({
        205: topic_message(205),
        104: topic_message(104, topic_root=True),
        4: ordinary_reply,
    })

    asyncio.run(MsgLogIngestionService(db, mtproto).run(100, lease_owner="worker-a"))

    assert [ids for _, ids in mtproto.calls] == [
        list(range(205, 105, -1)), list(range(105, 5, -1)), list(range(5, 0, -1)),
    ]
    accepted = [entry for entry in db.persisted if entry[1] == "eligible"]
    assert [(entry[0], entry[2]) for entry in accepted] == [
        (205, "tests.slave"), (104, "tests.slave"),
    ]
    ordinary_reply_entry = next(entry for entry in db.persisted if entry[0] == 4)
    assert ordinary_reply_entry[1:] == ("not-topic", None, None)
    assert db.scan.status == "complete"


def test_ingestion_skips_are_neutral_and_existing_streak_completes_at_500():
    db = FakeDatabase(scan_boundary=503)
    db.persist_msglog_ingestion_item = lambda scan, **kwargs: _five_hundred_rule(db, scan, **kwargs)
    mtproto = FakeMTProto(
        {message_id: topic_message(message_id) for message_id in range(1, 504)},
        scan_ceiling=503,
    )
    mtproto.messages[502] = SimpleNamespace(id=502, message="service", action=object(), reply_to=None)

    asyncio.run(MsgLogIngestionService(db, mtproto).run(100, lease_owner="worker-a"))

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

    asyncio.run(MsgLogIngestionService(db, mtproto).run(100, lease_owner="worker-a"))

    stored = db.persisted[0][3]
    assert stored.media_type == "Document"
    assert stored.msg_type == "File"
    assert stored.mime == "video/mp4"
    assert stored.time == source_time


def test_ingestion_marks_transient_mtproto_failure_for_retry_without_advancing_cursor():
    db = FakeDatabase(scan_boundary=5)
    mtproto = FakeMTProto({}, error=MTProtoRetryableError("temporary"), scan_ceiling=5)

    asyncio.run(MsgLogIngestionService(db, mtproto).run(100, lease_owner="worker-a"))

    assert db.scan.status == "retryable-error"
    assert db.scan.error == "temporary"
    assert db.scan.cursor == 5
