import copy
import datetime
import html
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from telegram import Chat, Message
from telegram.error import BadRequest, NetworkError, RetryAfter

from efb_telegram_master.aggregate import make_source_member
from efb_telegram_master.bot_manager import TelegramBotManager
from efb_telegram_master.db import DatabaseManager, MsgLog, database
from efb_telegram_master.live_aggregate import LiveTextAggregation
from efb_telegram_master.outbound import OutboundQueue, OutboundQueueScheduler, QueueRequest, SenderSelection, SenderSelectionResult
from efb_telegram_master.queued_log import decode_aggregation
from efb_telegram_master.utils import ExperimentalFlagsManager
from tests.unit.test_live_aggregate import source


class Transport:
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.error = None

    def send_message(self, chat_id, text, **kwargs):
        self.calls.append(("send_message", chat_id, text, kwargs))
        if self.error:
            raise self.error
        message_id = len(self.messages) + 1
        return self._save(chat_id, message_id, text, kwargs.get("message_thread_id"))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None):
        self.calls.append(("edit_message_text", chat_id, text, {"message_id": message_id}))
        if self.error:
            raise self.error
        return self._save(chat_id, message_id, text, self.messages[message_id].message_thread_id)

    def _save(self, chat_id, message_id, text, topic):
        message = Message(message_id, datetime.datetime.now(datetime.timezone.utc), Chat(chat_id, "supergroup"),
                          text=html.unescape(re.sub(r"<[^>]+>", "", text)), message_thread_id=topic)
        self.messages[message_id] = message
        return message


class Manager(TelegramBotManager):
    def select_sender(self, row, now):
        sender_id = None if row.required_sender_bot_id == "__main__" else row.required_sender_bot_id
        return SenderSelectionResult(SenderSelection(self.transport, sender_id if row.required_sender_bot_id else self.sender_id))

    def acquire_sender_limits(self, selection, chat_id):
        return True

    def record_queued_success(self, row, result, selection):
        pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr("efb_telegram_master.db.utils.get_data_path", lambda _: tmp_path)
    db = DatabaseManager(SimpleNamespace(channel_id="tests.aggregate.runtime", config={}))
    manager = Manager.__new__(Manager)
    manager.channel = SimpleNamespace(db=db, config={"admins": [1]})
    manager.channel.flag = ExperimentalFlagsManager(manager.channel)
    manager.logger = logging.getLogger("tests.aggregate.runtime")
    manager.transport = Transport()
    manager._bot = manager.transport
    manager.sender_id = None
    manager.bot_pool = None
    manager._bot_chat_state_lock = threading.Lock()
    manager._bot_chat_disabled_until = {}
    manager._queued_db_log_context_lock = threading.Lock()
    manager._queued_completion_callbacks = {}
    manager._queued_db_log_contexts = {}
    manager._outbound_queue = OutboundQueue(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1)
    manager._outbound_scheduler = OutboundQueueScheduler(manager._outbound_queue, manager, executor, 1)
    manager.live_aggregation = LiveTextAggregation(manager)
    try:
        yield manager
    finally:
        executor.shutdown(wait=True)
        manager._outbound_queue.close()
        db.stop_worker()
        database.initialize(original_database)


def append(manager, uid, at, text="body", topic=None):
    member = make_source_member(source(uid, text), received_time=datetime.datetime.fromtimestamp(at))
    key = (member["origin_uid"], -100, str(topic) if topic is not None else None)
    assert manager.live_aggregation.append(key, member)
    return key, member


def complete(manager):
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    assert scheduler.in_flight, scheduler.failure
    for submitted in scheduler.in_flight.values():
        submitted.future.result(timeout=2)
    scheduler.harvest_completed()
    assert scheduler.failure is None


def test_durable_append_owner_switch_and_parsed_capacity(runtime):
    manager = runtime
    append(manager, "one", 1, "&" * 2000)
    complete(manager)
    append(manager, "two", 5, "next")
    complete(manager)
    assert [call[0] for call in manager.transport.calls] == ["send_message", "edit_message_text"]
    first = MsgLog.get()
    assert [child["source_id"] for child in first.aggregate["children"]] == ["one", "two"]
    assert "..." not in manager.transport.calls[0][2]
    assert len(manager.transport.calls[0][2]) > 4096  # HTML escaping fits parsed Telegram text.
    manager.sender_id = "700"
    append(manager, "three", 9, "new owner")
    complete(manager)
    rows = list(MsgLog.select().order_by(MsgLog.master_msg_id))
    assert [child["source_id"] for child in rows[1].aggregate["children"]] == ["three"]
    assert rows[0].sender_bot_id is None and rows[1].sender_bot_id == "700"
    # A redelivery is neither another source nor another API request.
    append(manager, "three", 9.1)
    manager._outbound_scheduler.dispatch_once()
    assert len(manager.transport.calls) == 3


def test_in_flight_arrival_receipt_failure_and_restart(runtime, monkeypatch):
    manager = runtime
    append(manager, "one", 1)
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    first = next(iter(scheduler.in_flight.values()))
    first.future.result(timeout=2)
    append(manager, "two", 1.1)
    finalizer = manager.channel.db.finalize_aggregate_message
    monkeypatch.setattr(manager.channel.db, "finalize_aggregate_message", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    scheduler.harvest_completed()
    assert manager._outbound_queue.sent_pending()
    scheduler.dispatch_once()
    assert len(manager.transport.calls) == 1
    queue_path = manager._outbound_queue.path.parent
    manager._outbound_queue.close()
    manager._outbound_queue = OutboundQueue(queue_path)
    executor = scheduler.executor
    manager._outbound_scheduler = OutboundQueueScheduler(manager._outbound_queue, manager, executor, 1)
    manager.live_aggregation = LiveTextAggregation(manager)
    # Flag remains disabled; persisted tasks still recover and run.
    monkeypatch.setattr(manager.channel.db, "finalize_aggregate_message", finalizer)
    manager._outbound_queue.connection.execute("UPDATE outbound_queue SET reconcile_after=0")
    manager._outbound_queue.connection.commit()
    complete(manager)
    assert [call[0] for call in manager.transport.calls] == ["send_message", "edit_message_text"]
    assert [child["source_id"] for child in MsgLog.get().aggregate["children"]] == ["one", "two"]
    # Completed containers are not reopened on another restart.
    manager.live_aggregation = LiveTextAggregation(manager)
    append(manager, "three", 5)
    complete(manager)
    assert MsgLog.select().count() == 2


def test_rolling_count_fixed_deadline_and_topic_boundary_order(runtime):
    manager = runtime
    with patch("efb_telegram_master.outbound.time.time", return_value=100):
        key, _ = append(manager, "one", 100, topic=3)
        complete(manager)
    with patch("efb_telegram_master.outbound.time.time", return_value=100.1):
        append(manager, "two", 100.1, topic=3)
        complete(manager)
    with patch("efb_telegram_master.outbound.time.time", return_value=100.15):
        # Another topic does not close this topic's confirmed container.
        with manager.live_aggregation.independent((key[0], -100, "4")):
            request = manager.live_aggregation.bind_independent_requests([QueueRequest("send_message", (), {"chat_id": -100, "text": "other topic"})])
            identifier, _ = manager._outbound_queue.enqueue_many(request, manager._queue_operation)
            manager.live_aggregation.note_independent_enqueued(identifier)
        complete(manager)
    with patch("efb_telegram_master.outbound.time.time", return_value=100.2):
        append(manager, "three", 100.2, topic=3)
        append(manager, "four", 100.5, topic=3)
        manager._outbound_scheduler.dispatch_once()
        assert not manager._outbound_scheduler.in_flight
        pending = manager._outbound_queue.aggregation_rows()[0]
        assert decode_aggregation(pending.log_context)["due"] == 103.2
        # This topic's boundary waits for the delayed batch, even at blocking priority.
        with manager.live_aggregation.independent(key):
            request = manager.live_aggregation.bind_independent_requests([QueueRequest("send_message", (), {"chat_id": -100, "text": "media", "_send_mode": "blocking"})])
            identifier, _ = manager._outbound_queue.enqueue_many(request, manager._queue_operation)
            manager.live_aggregation.note_independent_enqueued(identifier)
        append(manager, "five", 100.6, topic=3)
        manager._outbound_scheduler.dispatch_once()
        assert not manager._outbound_scheduler.in_flight
    with patch("efb_telegram_master.outbound.time.time", return_value=103.2):
        complete(manager)
        assert manager.transport.calls[-1][0] == "edit_message_text"
        complete(manager)
        assert manager.transport.calls[-1][2] == "media"
    with patch("efb_telegram_master.outbound.time.time", return_value=103.6):
        complete(manager)
    assert [child["source_id"] for child in MsgLog.get_by_id("-100.1").aggregate["children"]] == ["one", "two", "three", "four"]
    assert manager.transport.calls[-1][0] == "send_message"
    assert MsgLog.get_by_id("-100.4").aggregate["children"][0]["source_id"] == "five"


def test_uncertain_attempt_blocks_successor(runtime):
    manager = runtime
    append(manager, "one", 1)
    manager.transport.error = NetworkError("response lost")
    manager._outbound_scheduler.dispatch_once()
    submitted = next(iter(manager._outbound_scheduler.in_flight.values()))
    with pytest.raises(NetworkError):
        submitted.future.result(timeout=2)
    append(manager, "two", 1.1)
    manager._outbound_scheduler.harvest_completed()
    manager.sender_id = "700"
    manager._outbound_scheduler.dispatch_once()
    assert len(manager.transport.calls) == 1
    frozen = manager._outbound_queue.aggregation_rows()[0]
    assert frozen.required_sender_bot_id == "__main__"
    assert [member["source_id"] for member in decode_aggregation(frozen.log_context)["aggregate"]["children"]] == ["one"]


def test_fixed_owner_update_and_unchanged_render_metadata(runtime):
    manager = runtime
    key, member = append(manager, "one", 1)
    complete(manager)
    manager.sender_id = "700"
    revised = copy.deepcopy(member)
    revised["source_revision"] = 2
    manager.live_aggregation.queue_container_update("-100.1", [revised], key=key)
    manager.transport.error = BadRequest("Message is not modified")
    complete(manager)
    assert MsgLog.get().aggregate["children"][0]["source_revision"] == 2
    assert MsgLog.get().aggregate["revision"] == 2
    assert MsgLog.get().sender_bot_id is None


def test_known_rejection_releases_unclaimed_batch_for_next_available(runtime):
    manager = runtime
    append(manager, "one", 1)
    manager.transport.error = RetryAfter(0)
    manager._outbound_scheduler.dispatch_once()
    submitted = next(iter(manager._outbound_scheduler.in_flight.values()))
    with pytest.raises(RetryAfter):
        submitted.future.result(timeout=2)
    manager._outbound_scheduler.harvest_completed()
    row = manager._outbound_queue.aggregation_rows()[0]
    assert decode_aggregation(row.log_context)["kind"] == "logical"
    manager.sender_id = "700"
    manager.transport.error = None
    complete(manager)
    assert MsgLog.get().sender_bot_id == "700"
    assert MsgLog.get().aggregate["children"][0]["source_id"] == "one"


def test_ordinary_ingestion_snapshots_before_formatting_and_preserves_prefix(runtime):
    from efb_telegram_master.slave_message import SlaveMessageProcessor
    from unittest.mock import Mock
    manager = runtime
    manager.channel.flag.config["text_aggregation"] = True
    processor = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    processor.bot = manager
    processor.flag = manager.channel.flag
    processor.db = manager.channel.db
    processor.chat_manager = SimpleNamespace(update_chat_obj=lambda chat: chat, get_or_enrol_member=lambda chat, author: author)
    processor.logger = manager.logger
    processor._pending_slave_messages = set()
    processor._pending_slave_messages_lock = threading.Lock()
    processor.get_slave_msg_dest = lambda msg: ("Source Group Alice:", (-100, 7))
    processor.is_silent = lambda msg: False
    processor.dispatch_message = Mock()
    msg = source("ingested", "literal <body>")
    msg.chat.members.append(msg.author)
    processor.send_message(msg)
    complete(manager)
    assert msg.text == "literal <body>"
    row = MsgLog.get()
    assert row.master_message_thread_id == "7"
    assert row.text == "Source Group Alice:\nliteral <body>"
    assert row.aggregate["children"][0]["author_name"] == "Alice"
    assert not processor._pending_slave_messages
    processor.dispatch_message.assert_not_called()


def test_pending_capacity_boundary_and_idle_close_preserve_members(runtime):
    manager = runtime
    manager.channel.flag.config["text_aggregation_max_members"] = 1
    manager.channel.flag.config["text_aggregation_idle_seconds"] = 10
    append(manager, "one", 1)
    append(manager, "two", 2)
    complete(manager)
    complete(manager)
    manager.channel.flag.config["text_aggregation_max_members"] = 200
    append(manager, "three", 20)
    complete(manager)
    assert [call[0] for call in manager.transport.calls] == ["send_message"] * 3
    assert [row.aggregate["children"][0]["source_id"] for row in MsgLog.select().order_by(MsgLog.master_msg_id)] == ["one", "two", "three"]
