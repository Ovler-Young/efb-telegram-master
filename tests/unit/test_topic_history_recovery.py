from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master.topic_history_recovery import TopicHistoryRecovery, TopicRecoveryRequest


def test_topic_recovery_models_create_required_durable_columns():
    from peewee import SqliteDatabase
    from efb_telegram_master.db import TopicRecoveryEntry, TopicRecoveryScan

    database = SqliteDatabase(":memory:")
    with database.bind_ctx([TopicRecoveryScan, TopicRecoveryEntry]):
        database.create_tables([TopicRecoveryScan, TopicRecoveryEntry])
        scan_columns = {column.name for column in database.get_columns("topicrecoveryscan")}
        entry_columns = {column.name for column in database.get_columns("topicrecoveryentry")}

    assert {"scan_boundary", "cursor", "status", "error"}.issubset(scan_columns)
    assert {"classification", "target_message_id", "idempotency_key"}.issubset(entry_columns)


class FakeDatabase:
    def __init__(self):
        self.scan = SimpleNamespace(id=1, cursor=0, scan_boundary=205)
        self.entries = {}
        self.advances = []
        self.msglog_receipts = []

    def get_or_create_topic_recovery_scan(self, **_kwargs):
        return self.scan

    def get_topic_recovery_entry(self, scan_id, source_message_id):
        return self.entries.get((scan_id, source_message_id))

    def save_topic_recovery_entry(self, **kwargs):
        entry = SimpleNamespace(**kwargs)
        self.entries[(kwargs["scan_id"], kwargs["source_message_id"])] = entry
        return entry

    def advance_topic_recovery_scan(self, scan, cursor, **kwargs):
        scan.cursor = cursor
        self.advances.append((cursor, kwargs))

    def get_msg_log(self, **_kwargs):
        return None

    def add_topic_recovery_msg_log(self, **kwargs):
        self.msglog_receipts.append(kwargs)


class FakeMTProto:
    def __init__(self):
        self.calls = []

    async def get_input_channel(self, chat_id):
        return chat_id

    async def get_channel_messages(self, channel, ids):
        self.calls.append((channel, ids))
        return [
            SimpleNamespace(id=message_id, reply_to=SimpleNamespace(
                forum_topic=True, reply_to_top_id=7,
            ))
            for message_id in ids
        ]


def test_recovery_requests_ascending_batches_and_records_target_receipts():
    database = FakeDatabase()
    mtproto = FakeMTProto()
    bot = SimpleNamespace(enqueue_history_operation=Mock(
        return_value=SimpleNamespace(result=lambda: SimpleNamespace(message_id=900))
    ))
    recovery = TopicHistoryRecovery(database, bot, mtproto)

    recovery.recover(TopicRecoveryRequest(10, 7, 20, 8, "tests.mocks.slave.chat", 205))

    assert [ids for _channel, ids in mtproto.calls] == [list(range(1, 101)), list(range(101, 201)), list(range(201, 206))]
    assert all(len(ids) <= 100 for _channel, ids in mtproto.calls)
    assert len(bot.enqueue_history_operation.call_args_list) == 205
    assert database.entries[(1, 205)].status == "accepted"
    assert database.entries[(1, 205)].target_message_id == 900
    assert len(database.msglog_receipts) == 205


def test_recovery_rejects_non_topic_deleted_service_protected_and_cross_topic_messages():
    database = FakeDatabase()
    database.scan.scan_boundary = 5

    class FilteredMTProto(FakeMTProto):
        async def get_channel_messages(self, channel, ids):
            class MessageEmpty:
                id = 1

            return [
                MessageEmpty(),
                SimpleNamespace(id=2, action="service"),
                SimpleNamespace(id=3, noforwards=True),
                SimpleNamespace(id=4, reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=1)),
                SimpleNamespace(id=5, reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=8)),
            ]

    bot = SimpleNamespace(enqueue_history_operation=Mock())
    TopicHistoryRecovery(database, bot, FilteredMTProto()).recover(
        TopicRecoveryRequest(10, 7, 20, 8, "tests.mocks.slave.chat", 5)
    )

    assert [database.entries[(1, index)].classification for index in range(1, 6)] == [
        "deleted", "service", "protected", "general-topic", "cross-topic",
    ]
    bot.enqueue_history_operation.assert_not_called()
    assert TopicHistoryRecovery._classify(
        SimpleNamespace(id=6, forwards_restricted=True), 7
    ) == "unforwardable"


def test_recovery_resumes_without_repeating_accepted_transfers():
    database = FakeDatabase()
    database.scan.cursor = 1
    database.scan.scan_boundary = 2
    database.entries[(1, 2)] = SimpleNamespace(status="accepted")
    mtproto = FakeMTProto()
    bot = SimpleNamespace(enqueue_history_operation=Mock())

    TopicHistoryRecovery(database, bot, mtproto).recover(
        TopicRecoveryRequest(10, 7, 20, 8, "tests.mocks.slave.chat", 2)
    )

    assert mtproto.calls == [(10, [2])]
    bot.enqueue_history_operation.assert_not_called()
