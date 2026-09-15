"""Restart regressions with real message objects and large unrelated state."""

import io
import pickle
import tracemalloc
from concurrent.futures import Future, ThreadPoolExecutor
import threading
import time

import pytest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot import MsgType
from ehforwarderbot.chat import GroupChat, ChatMember

from efb_telegram_master.bot_manager import QueuedDbLogContext, TelegramBotManager
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.outbound import OutboundQueue, OutboundQueueScheduler, QueueRequest
from tests.unit.test_outbound import DurableAdapter, send_message


def message_with_group(extra_bytes=0):
    chat = GroupChat(module_id="source", uid="group", with_self=False)
    author = ChatMember(chat, uid="author", name="Author")
    chat.members.append(author)
    other = ChatMember(chat, uid="other", vendor_specific={"unrelated": b"x" * extra_bytes})
    chat.members.append(other)
    return ETMMsg(
        uid="source-1", text="message", chat=chat, author=author,
        type=MsgType.Text, type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="destination"),
    )


def test_durable_log_does_not_copy_group_cache_files_or_reply_graph():
    message = message_with_group(4 * 1024 * 1024)
    target = message_with_group(4 * 1024 * 1024)
    target.uid = "quoted-message"
    target.file_id = "remote-file-must-not-be-fetched"
    message.target = target
    message.file = io.BytesIO(b"f" * (4 * 1024 * 1024))
    message.vendor_specific = {"cache": b"v" * (4 * 1024 * 1024)}
    encoded = TelegramBotManager._encode_queued_log_context(QueuedDbLogContext(message, (123, 456)))
    assert len(encoded) < 16 * 1024, f"log-only context expanded to {len(encoded)} bytes"
    with patch.object(ETMMsg, "_load_file", side_effect=AssertionError("logging must not load files")):
        stored, old_id = TelegramBotManager._decode_queued_log_context(encoded)
    assert (stored.uid, stored.text, old_id) == ("source-1", "message", (123, 456))
    assert stored.author.uid == "author" and stored.author.chat.uid == "group"
    assert stored.target.uid == "quoted-message"
    assert stored.chat.members == []
    assert message.file.getbuffer().nbytes == 4 * 1024 * 1024


def test_legacy_log_recovery_does_not_download_or_open_media():
    message = message_with_group()
    message.file_id = "legacy-file"
    message.target = message_with_group()
    message.target.file_id = "legacy-quoted-file"
    legacy = b"\x01" + pickle.dumps((message, None), protocol=5)
    with patch.object(ETMMsg, "_load_file", side_effect=AssertionError("legacy replay attempted media I/O")):
        stored, old_id = TelegramBotManager._decode_queued_log_context(legacy)
    assert stored.uid == message.uid and old_id is None
    assert stored.target.uid == message.target.uid


def test_reconciliation_streams_large_contexts_instead_of_fetchall(tmp_path):
    queue = OutboundQueue(tmp_path)
    try:
        with queue.connection:
            queue.connection.executemany(
                "INSERT INTO outbound_queue(priority, telegram_chat_id, operation, payload, created_at, "
                "log_context, delivery_state, completion_receipt) VALUES(0,1,'send_message',X'',0,?,'sent_pending',X'01')",
                ((b"c" * (1024 * 1024),) for _ in range(32)),
            )
        tracemalloc.start()
        try:
            count = 0
            for row in queue.iter_sent_pending(due_before=1, limit=32):
                assert len(row.log_context) == 1024 * 1024
                count += 1
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert count == 32
        assert peak < 5 * 1024 * 1024, f"replay batch allocated {peak} bytes"
    finally:
        queue.close()


@pytest.mark.parametrize("state,field", [
    ("queued", "payload"), ("queued", "log_context"), ("queued", "completion_receipt"),
    ("sent_pending", "log_context"), ("sent_pending", "completion_receipt"),
])
def test_oversized_legacy_record_is_retained_without_loading_it(tmp_path, state, field):
    queue = OutboundQueue(tmp_path)
    queue.MAX_REPLAY_BYTES = 1024 * 1024
    with queue.connection:
        queue.connection.execute(
            "INSERT INTO outbound_queue(priority, telegram_chat_id, operation, payload, created_at, "
            "log_context, delivery_state, completion_receipt) VALUES(0,1,'send_message',X'',0,X'01',?,X'01')",
            (state,),
        )
        queue.connection.execute(f"UPDATE outbound_queue SET {field} = zeroblob(?)", (8 * 1024 * 1024,))
    adapter = DurableAdapter(reconcile=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
            tracemalloc.start()
            try:
                scheduler.dispatch_once()
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            if state == "queued":
                assert not scheduler.stopping
                assert scheduler.failure is None
                assert scheduler.quarantined_rows
                assert "Row retained" in next(iter(scheduler.quarantined_rows.values()))
            else:
                assert scheduler.stopping
                assert "Row retained" in str(scheduler.failure)
            assert peak < 1024 * 1024
            assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 1
            assert adapter.calls == [] and adapter.reconciled == []
    finally:
        queue.close()


@pytest.mark.parametrize("same_destination", [False, True], ids=["other-destination", "same-destination"])
def test_oversized_legacy_row_does_not_block_newer_runnable_rows(tmp_path, same_destination):
    queue = OutboundQueue(tmp_path)
    queue.MAX_REPLAY_BYTES = 1024 * 1024
    with queue.connection:
        queue.connection.execute(
            "INSERT INTO outbound_queue(priority, telegram_chat_id, operation, payload, created_at) "
            "VALUES(0,1,'send_message',zeroblob(?),0)",
            (8 * 1024 * 1024,),
        )
    normal_chat = 1 if same_destination else 2
    normal_id, _waiter = queue.enqueue_many(
        [QueueRequest("send_message", (), {"chat_id": normal_chat, "text": "ok"})],
        lambda _name: send_message,
    )
    adapter = DurableAdapter(reconcile=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
            scheduler.dispatch_once()
            assert scheduler.quarantined_rows
            scheduler.dispatch_once()
            assert not scheduler.stopping
            assert normal_id in scheduler.in_flight
            scheduler.in_flight[normal_id].future.result(timeout=1)
            scheduler.harvest_completed()
        assert adapter.calls == [(normal_id, normal_chat, "send_message")]
    finally:
        queue.close()


def test_quarantined_row_is_rechecked_when_replay_budget_changes(tmp_path):
    queue = OutboundQueue(tmp_path)
    payload = b"\x01" + pickle.dumps(((), {"chat_id": 1, "text": "x" * (2 * 1024 * 1024)}), protocol=5)
    with queue.connection:
        queue.connection.execute(
            "INSERT INTO outbound_queue(priority, telegram_chat_id, operation, payload, created_at) "
            "VALUES(0,1,'send_message',?,0)",
            (payload,),
        )
    queue.MAX_REPLAY_BYTES = 1024 * 1024
    adapter = DurableAdapter(reconcile=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
            scheduler.dispatch_once()
            assert scheduler.quarantined_rows
            assert not scheduler.in_flight

            queue.MAX_REPLAY_BYTES = 4 * 1024 * 1024
            scheduler.dispatch_once()
            assert scheduler.quarantined_rows == {}
            assert len(scheduler.in_flight) == 1
            submitted = next(iter(scheduler.in_flight.values()))
            submitted.future.result(timeout=1)
            scheduler.harvest_completed()
        assert adapter.calls and adapter.calls[0][1:] == (1, "send_message")
    finally:
        queue.close()


def test_oversized_legacy_media_is_compacted_and_sent_without_blocking_same_destination(tmp_path):
    queue = OutboundQueue(tmp_path)
    queue.MAX_REPLAY_BYTES = 1024 * 1024
    legacy_payload = b"\x01" + pickle.dumps(
        ((1, io.BytesIO(b"x" * (2 * 1024 * 1024))), {}), protocol=5
    )
    with queue.connection:
        cursor = queue.connection.execute(
            "INSERT INTO outbound_queue(priority, telegram_chat_id, operation, payload, created_at) "
            "VALUES(0,1,'send_document',?,0)",
            (legacy_payload,),
        )
        legacy_id = int(cursor.lastrowid)
    newer_id, _waiter = queue.enqueue_many(
        [QueueRequest("send_message", (), {"chat_id": 1, "text": "new"})],
        lambda name: send_message if name == "send_message" else (lambda chat_id, document: None),
    )
    adapter = DurableAdapter(reconcile=True)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
            scheduler.dispatch_once()
            assert legacy_id in scheduler.in_flight
            compact = queue.connection.execute(
                "SELECT substr(payload,1,1), length(payload) FROM outbound_queue WHERE id = ?",
                (legacy_id,),
            ).fetchone()
            assert compact[0] == b"\x02"
            assert compact[1] < 4096
            assert len(list(queue.media_dir.iterdir())) == 1
            scheduler.in_flight[legacy_id].future.result(timeout=1)
            scheduler.harvest_completed()
            scheduler.dispatch_once()
            assert newer_id in scheduler.in_flight
            scheduler.in_flight[newer_id].future.result(timeout=1)
            scheduler.harvest_completed()
        assert [call[0] for call in adapter.calls] == [legacy_id, newer_id]
        assert queue.heads() == []
        assert list(queue.media_dir.iterdir()) == []
    finally:
        queue.close()


def test_worker_count_does_not_override_replay_byte_budget(tmp_path):
    queue = OutboundQueue(tmp_path)
    queue.MAX_REPLAY_BYTES = 1024
    with queue.connection:
        for chat_id in range(1, 4):
            payload = queue.encode_payload((), {"chat_id": chat_id, "text": "x" * 700})
            queue.connection.execute(
                "INSERT INTO outbound_queue(priority, telegram_chat_id, operation, payload, created_at) "
                "VALUES(0,?,'send_message',?,0)", (chat_id, payload),
            )
    executor = Mock()
    executor.submit.side_effect = lambda *args: Future()
    adapter = DurableAdapter(reconcile=True)
    scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=8)
    try:
        scheduler.dispatch_once()
        assert len(scheduler.in_flight) == 1
        first = next(iter(scheduler.in_flight.values()))
        first.future.set_result(first.row.id)
        scheduler.harvest_completed()
        scheduler.dispatch_once()
        assert len(scheduler.in_flight) == 1
        assert next(iter(scheduler.in_flight.values())).row.id != first.row.id
    finally:
        queue.close()


@pytest.mark.parametrize("delay", [None, 60.0])
def test_worker_waits_for_events_or_deadline_without_scanning_four_times_per_second(delay):
    manager = object.__new__(TelegramBotManager)
    manager.logger = Mock()
    manager._send_worker_stop = threading.Event()
    manager._finalize_outbound_resources = Mock()
    wake = threading.Event()
    observed = []
    scheduler = SimpleNamespace(
        stopping=False, failure=None, wake_event=wake,
        next_deadline=None if delay is None else time.monotonic() + delay,
        harvest_completed=Mock(), dispatch_once=Mock(side_effect=wake.set),
        stop_and_drain=Mock(),
    )
    manager._outbound_scheduler = scheduler

    def wait(timeout):
        observed.append(timeout)
        assert wake.is_set()  # A wake during dispatch must survive until wait.
        manager._send_worker_stop.set()

    with patch.object(wake, "wait", side_effect=wait):
        manager._queued_send_worker()
    assert len(observed) == 1
    if delay is None:
        assert observed[0] is None
    else:
        assert observed[0] > 59.0
