import asyncio
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master.bot_manager import TelegramBotManager, TopicRecoveryQueueContext
from efb_telegram_master.chat_binding import ChatBindingManager
from efb_telegram_master.outbound import OutboundQueue, OutboundQueueScheduler, QueueRequest, SenderSelection, SenderSelectionResult
from efb_telegram_master.topic_history_recovery import TopicHistoryRecovery, TopicRecoveryRequest


def completed_future(result):
    waiter = Future()
    waiter.set_result(result)
    return waiter


def test_topic_recovery_models_create_required_durable_columns():
    from peewee import SqliteDatabase
    from efb_telegram_master.db import TopicRecoveryEntry, TopicRecoveryScan

    database = SqliteDatabase(":memory:")
    with database.bind_ctx([TopicRecoveryScan, TopicRecoveryEntry]):
        database.create_tables([TopicRecoveryScan, TopicRecoveryEntry])
        scan_columns = {column.name for column in database.get_columns("topicrecoveryscan")}
        entry_columns = {column.name for column in database.get_columns("topicrecoveryentry")}

    assert {"scan_boundary", "cursor", "status", "error"}.issubset(scan_columns)
    assert {"classification", "target_message_id", "idempotency_key", "delivery_queue_id"}.issubset(entry_columns)


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
        previous = self.entries.get((kwargs["scan_id"], kwargs["source_message_id"]))
        if "delivery_queue_id" not in kwargs and previous is not None:
            kwargs["delivery_queue_id"] = getattr(previous, "delivery_queue_id", None)
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
        self.loops = []

    async def get_input_channel(self, chat_id):
        self.loops.append(asyncio.get_running_loop())
        return chat_id

    async def get_channel_messages(self, channel, ids):
        self.loops.append(asyncio.get_running_loop())
        self.calls.append((channel, ids))
        return [
            SimpleNamespace(id=message_id, reply_to=SimpleNamespace(
                forum_topic=True, reply_to_top_id=7,
            ))
            for message_id in ids
        ]


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.loop = None

    def call(self, coroutine):
        self.calls.append(coroutine)
        loop = asyncio.new_event_loop()
        self.loop = loop
        try:
            return loop.run_until_complete(coroutine)
        finally:
            loop.close()


def test_recovery_requests_ascending_batches_and_records_target_receipts():
    database = FakeDatabase()
    mtproto = FakeMTProto()
    bot = SimpleNamespace(enqueue_history_operation=Mock(
        return_value=completed_future(SimpleNamespace(message_id=900))
    ))
    runtime = FakeRuntime()
    recovery = TopicHistoryRecovery(database, bot, mtproto, runtime)

    recovery.recover(TopicRecoveryRequest(10, 7, 20, 8, "tests.mocks.slave.chat", 205))

    assert [ids for _channel, ids in mtproto.calls] == [list(range(1, 101)), list(range(101, 201)), list(range(201, 206))]
    assert all(len(ids) <= 100 for _channel, ids in mtproto.calls)
    assert len(bot.enqueue_history_operation.call_args_list) == 205
    assert database.entries[(1, 205)].status == "accepted"
    assert database.entries[(1, 205)].target_message_id == 900
    assert len(database.msglog_receipts) == 205
    assert len(runtime.calls) == 1
    assert mtproto.loops and all(loop is runtime.loop for loop in mtproto.loops)


def test_recovery_copy_uses_a_versioned_durable_completion_context():
    database = FakeDatabase()
    database.scan.scan_boundary = 1
    bot = SimpleNamespace(enqueue_history_operation=Mock(
        return_value=completed_future(SimpleNamespace(message_id=900))
    ))

    TopicHistoryRecovery(database, bot, FakeMTProto(), FakeRuntime()).recover(
        TopicRecoveryRequest(10, 7, 20, 8, "tests.mocks.slave.chat", 1)
    )

    context = TelegramBotManager._decode_topic_recovery_log_context(
        bot.enqueue_history_operation.call_args.kwargs["log_context"]
    )
    assert context == (1, 10, 1, 20, "tests.mocks.slave.chat", "", "1:1")


@pytest.mark.asyncio
async def test_recovery_awaits_queue_completion_without_blocking_the_runtime_loop():
    database = FakeDatabase()
    database.scan.scan_boundary = 1
    waiter: Future = Future()

    class LoopScheduledBot:
        def enqueue_history_operation(self, **_kwargs):
            asyncio.get_running_loop().call_soon(
                waiter.set_result, SimpleNamespace(message_id=900)
            )
            return waiter

    recovery = TopicHistoryRecovery(database, LoopScheduledBot(), FakeMTProto(), FakeRuntime())

    await asyncio.wait_for(
        recovery._recover(TopicRecoveryRequest(10, 7, 20, 8, "tests.mocks.slave.chat", 1)),
        timeout=0.5,
    )

    assert database.entries[(1, 1)].status == "accepted"


def test_recovery_caps_scan_boundary_before_creating_scan_state():
    class CeilingDatabase(FakeDatabase):
        def __init__(self):
            super().__init__()
            self.requested_boundary = None

        def get_or_create_topic_recovery_scan(self, **kwargs):
            self.requested_boundary = kwargs["scan_boundary"]
            self.scan.scan_boundary = kwargs["scan_boundary"]
            return self.scan

    database = CeilingDatabase()
    mtproto = FakeMTProto()
    mtproto.config = SimpleNamespace(scan_ceiling=2)
    bot = SimpleNamespace(enqueue_history_operation=Mock(
        return_value=completed_future(SimpleNamespace(message_id=900))
    ))

    TopicHistoryRecovery(database, bot, mtproto, FakeRuntime()).recover(
        TopicRecoveryRequest(10, 7, 20, 8, "tests.mocks.slave.chat", 5)
    )

    assert database.requested_boundary == 2
    assert mtproto.calls == [(10, [1, 2])]


def test_recovery_receipt_reconciles_after_restart_without_copying_twice(tmp_path):
    database = SimpleNamespace(attempts=0, entries=[], msglogs=[])

    def reconcile_topic_recovery_delivery(**kwargs):
        database.attempts += 1
        if database.attempts == 1:
            raise RuntimeError("application database unavailable")
        database.entries.append(kwargs["delivery_queue_id"])
        database.msglogs.append((kwargs["source_message_id"], kwargs["target_message_id"]))

    database.reconcile_topic_recovery_delivery = reconcile_topic_recovery_delivery
    manager = object.__new__(TelegramBotManager)
    manager.channel = SimpleNamespace(db=database)
    manager.logger = Mock()

    class Adapter:
        def __init__(self):
            self.calls = []

        def select_sender(self, row, now):
            return SenderSelectionResult(selection=SenderSelection(object(), None))

        def acquire_sender_limits(self, selection, telegram_chat_id):
            return True

        def execute_queued_call(self, row, args, kwargs, selection):
            self.calls.append(row.id)
            return SimpleNamespace(message_id=900)

        def encode_queued_completion_receipt(self, result, selection):
            return TelegramBotManager.encode_queued_completion_receipt(result, selection)

        def reconcile_queued_delivery(self, row):
            return TelegramBotManager.reconcile_queued_delivery(manager, row)

        def record_queued_success(self, row, result, selection):
            return None

        def record_queued_failure(self, row, error, selection):
            raise AssertionError(error)

    context = TelegramBotManager.encode_topic_recovery_log_context(
        TopicRecoveryQueueContext(1, 10, 1, 20, "tests.mocks.slave.chat", "text", "1:1")
    )

    def copy_message(*, chat_id, from_chat_id, message_id, message_thread_id):
        return None

    queue = OutboundQueue(tmp_path)
    row_id, _waiter = queue.enqueue_many(
        [QueueRequest("copy_message", (), {
            "chat_id": 20, "from_chat_id": 10, "message_id": 1, "message_thread_id": 8,
        }, log_context=context)],
        lambda _operation: copy_message,
    )
    first_adapter = Adapter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, first_adapter, executor, worker_count=1)
        scheduler.dispatch_once()
        scheduler.in_flight[row_id].future.result(timeout=1)
        scheduler.harvest_completed()

    assert first_adapter.calls == [row_id]
    assert [row.id for row in queue.sent_pending()] == [row_id]
    queue.close()

    restarted = OutboundQueue(tmp_path)
    second_adapter = Adapter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        OutboundQueueScheduler(restarted, second_adapter, executor, worker_count=1).dispatch_once()

    assert second_adapter.calls == []
    assert len(database.entries) == 1
    assert database.msglogs == [(1, 900)]
    assert database.attempts == 2
    assert restarted.sent_pending() == []


def test_recovery_restart_after_queue_commit_reuses_one_copy_row_before_dispatch(tmp_path):
    database = FakeDatabase()
    database.scan.scan_boundary = 1

    def copy_message(*, chat_id, from_chat_id, message_id, message_thread_id, disable_notification):
        return chat_id, from_chat_id, message_id, message_thread_id, disable_notification

    class QueueingBot:
        def __init__(self, queue, interrupt_after_commit):
            self.queue = queue
            self.interrupt_after_commit = interrupt_after_commit
            self.enqueued = threading.Event()

        def enqueue_history_operation(self, *, operation, args, kwargs, log_context, queue_id, **_ignored):
            row_id, waiter = self.queue.enqueue_many(
                [QueueRequest(operation, args, dict(kwargs), log_context, queue_id)],
                lambda _operation: copy_message,
            )
            self.enqueued.set()
            if self.interrupt_after_commit:
                raise RuntimeError("simulated restart before dispatcher submission")
            return waiter

    def reconcile_topic_recovery_delivery(**kwargs):
        database.save_topic_recovery_entry(
            scan_id=kwargs["scan_id"], source_message_id=kwargs["source_message_id"],
            classification="accepted", status="accepted", idempotency_key=kwargs["idempotency_key"],
            target_message_id=kwargs["target_message_id"], delivery_queue_id=kwargs["delivery_queue_id"],
        )
        database.add_topic_recovery_msg_log(
            source_chat_id=kwargs["source_chat_id"], source_message_id=kwargs["source_message_id"],
            target_chat_id=kwargs["target_chat_id"], target_message_id=kwargs["target_message_id"],
            slave_chat_id=kwargs["slave_chat_id"], text=kwargs["text"],
        )

    database.reconcile_topic_recovery_delivery = reconcile_topic_recovery_delivery

    first_queue = OutboundQueue(tmp_path)
    first_bot = QueueingBot(first_queue, interrupt_after_commit=True)
    request = TopicRecoveryRequest(10, 7, 20, 8, "tests.mocks.slave.chat", 1)
    TopicHistoryRecovery(database, first_bot, FakeMTProto(), FakeRuntime()).recover(request)

    entry = database.entries[(1, 1)]
    assert entry.status == "error"
    assert entry.delivery_queue_id == "topic-recovery:1:1"
    assert len(first_queue.heads()) == 1
    first_queue.close()

    restarted_queue = OutboundQueue(tmp_path)
    restarted_bot = QueueingBot(restarted_queue, interrupt_after_commit=False)
    recovery_thread = threading.Thread(
        target=TopicHistoryRecovery(database, restarted_bot, FakeMTProto(), FakeRuntime()).recover,
        args=(request,),
    )
    recovery_thread.start()
    assert restarted_bot.enqueued.wait(timeout=1)
    rows = restarted_queue.heads()
    assert len(rows) == 1
    assert rows[0].queue_id == "topic-recovery:1:1"

    class Adapter:
        def __init__(self):
            self.calls = []

        def select_sender(self, row, now):
            return SenderSelectionResult(selection=SenderSelection(object(), None))

        def acquire_sender_limits(self, selection, telegram_chat_id):
            return True

        def execute_queued_call(self, row, args, kwargs, selection):
            self.calls.append(row.id)
            return SimpleNamespace(message_id=900)

        def encode_queued_completion_receipt(self, result, selection):
            return TelegramBotManager.encode_queued_completion_receipt(result, selection)

        def reconcile_queued_delivery(self, row):
            context = TelegramBotManager._decode_topic_recovery_log_context(row.log_context)
            database.reconcile_topic_recovery_delivery(
                scan_id=context.scan_id, source_chat_id=context.source_chat_id,
                source_message_id=context.source_message_id, target_chat_id=context.target_chat_id,
                target_message_id=900, slave_chat_id=context.slave_chat_id, text=context.text,
                idempotency_key=context.idempotency_key, delivery_queue_id=row.queue_id,
            )
            return True

        def record_queued_success(self, row, result, selection):
            return None

        def record_queued_failure(self, row, error, selection):
            raise AssertionError(error)

    adapter = Adapter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(restarted_queue, adapter, executor, worker_count=1)
        scheduler.dispatch_once()
        row_id = next(iter(scheduler.in_flight))
        scheduler.in_flight[row_id].future.result(timeout=1)
        scheduler.harvest_completed()
    recovery_thread.join(timeout=1)

    assert not recovery_thread.is_alive()
    assert adapter.calls == [row_id]
    assert restarted_queue.heads() == []
    assert len(database.msglog_receipts) == 1
    restarted_queue.close()


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
    TopicHistoryRecovery(database, bot, FilteredMTProto(), FakeRuntime()).recover(
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

    TopicHistoryRecovery(database, bot, mtproto, FakeRuntime()).recover(
        TopicRecoveryRequest(10, 7, 20, 8, "tests.mocks.slave.chat", 2)
    )

    assert mtproto.calls == [(10, [2])]
    bot.enqueue_history_operation.assert_not_called()


def test_startup_resumes_partial_scan_from_cursor_without_duplicate_transfer():
    database = FakeDatabase()
    database.scan.cursor = 1
    database.scan.scan_boundary = 2
    database.scan.source_chat_id = "10"
    database.scan.source_thread_id = "7"
    database.scan.target_chat_id = "20"
    database.scan.target_thread_id = "8"
    database.scan.slave_chat_id = "tests.mocks.slave.chat"
    database.entries[(1, 2)] = SimpleNamespace(status="accepted")
    mtproto = FakeMTProto()
    bot = SimpleNamespace(enqueue_history_operation=Mock())
    binding = SimpleNamespace(db=database, logger=Mock())

    def recover_topic_history(**kwargs):
        TopicHistoryRecovery(database, bot, mtproto, FakeRuntime()).recover(TopicRecoveryRequest(**kwargs))

    binding.recover_topic_history = recover_topic_history
    database.get_incomplete_topic_recovery_scans = Mock(return_value=[database.scan])

    ChatBindingManager.resume_pending_topic_recoveries(binding)

    assert mtproto.calls == [(10, [2])]
    bot.enqueue_history_operation.assert_not_called()
