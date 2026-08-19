from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

from peewee import SqliteDatabase

from efb_telegram_master.models import MsgLog, MsgLogIngestionScan, database
from efb_telegram_master.persistence.msglog_ingestion_repository import MsgLogIngestionCompletion, MsgLogIngestionRepository


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

    def get_or_create_scan(self, source_chat_id, scan_boundary):
        assert source_chat_id == 100
        assert scan_boundary == self.scan.scan_boundary
        return self.scan

    def claim_scan(self, source_chat_id, lease_owner, lease_seconds):
        return self.scan if self.scan.status != "complete" else None

    def persist_item(self, scan, *, source_message_id, classification, slave_uid=None, message=None, lease_owner):
        self.persisted.append((source_message_id, classification, slave_uid, message))
        scan.cursor = source_message_id - 1
        if classification == "eligible":
            scan.existing_streak = 0
            return "inserted"
        return "skipped"

    def finish_scan(self, scan, *, status, error=None, lease_owner):
        scan.status = status
        scan.error = error

    def complete_scan(self, scan, *, lease_owner):
        scan.status = "complete"
        return MsgLogIngestionCompletion.COMPLETE

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


@contextmanager
def sqlite_ingestion_database(path=":memory:"):
    original_database = database.obj
    test_db = SqliteDatabase(path)
    database.initialize(test_db)
    test_db.connect()
    try:
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
        yield test_db, MsgLogIngestionRepository("tests.master")
    finally:
        if not test_db.is_closed():
            test_db.close()
        database.initialize(original_database)
