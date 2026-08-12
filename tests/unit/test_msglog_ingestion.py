import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from efb_telegram_master.msglog_ingestion import MsgLogIngestionService


@dataclass
class _Message:
    id: int
    reply_to: object
    message: str = "content"
    media: object = None
    action: object = None


class _Database:
    def __init__(self, outcomes=()):
        self.outcomes = iter(outcomes)
        self.persisted = []

    def get_topic_assoc_slave_uid(self, _source_chat_id, _topic_id):
        return "tests.slave.chat"

    def persist_ingested_msglog(self, source_chat_id, source_message_id, slave_uid, message):
        self.persisted.append((source_chat_id, source_message_id, slave_uid, message.text))
        return next(self.outcomes, "inserted")


class _MTProto:
    config = SimpleNamespace(scan_ceiling=3)

    async def get_input_channel(self, source_chat_id):
        return source_chat_id

    async def get_channel_messages(self, _channel, message_ids):
        return [_Message(message_id, SimpleNamespace(forum_topic=True, reply_to_top_id=9)) for message_id in message_ids]


def test_scan_persists_mapped_topic_messages_without_durable_scan_state():
    database = _Database()

    asyncio.run(MsgLogIngestionService(database, _MTProto()).run(100))

    assert database.persisted == [
        (100, 3, "tests.slave.chat", "content"),
        (100, 2, "tests.slave.chat", "content"),
        (100, 1, "tests.slave.chat", "content"),
    ]


def test_scan_stops_after_five_hundred_existing_mapped_messages():
    database = _Database(["existing"] * 500)
    mtproto = _MTProto()
    mtproto.config = SimpleNamespace(scan_ceiling=600)

    asyncio.run(MsgLogIngestionService(database, mtproto).run(100))

    assert len(database.persisted) == 500


def test_scan_stops_without_persisting_after_in_process_cancellation():
    database = _Database()
    mtproto = _MTProto()

    asyncio.run(MsgLogIngestionService(database, mtproto, should_stop=lambda: True).run(100))

    assert database.persisted == []
