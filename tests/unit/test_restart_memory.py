"""Restart regressions with real message objects and large unrelated state."""

import io
import json
import os
import pickle
import subprocess
import sys
import textwrap
import tracemalloc
from pathlib import Path
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



def _queue_subprocess(script: str, *arguments: object) -> dict:
    environment = dict(os.environ)
    project_root = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = project_root + os.pathsep + environment.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *(str(argument) for argument in arguments)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(result.stdout)


def _outbound_import_rss() -> int:
    return _queue_subprocess(
        """
        import json, resource
        from efb_telegram_master.outbound import OutboundQueue
        print(json.dumps({"rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
        """
    )["rss_kib"]


def test_startup_cleanup_does_not_materialize_a_nonmatching_128_mib_payload(tmp_path):
    size = 128 * 1024 * 1024
    baseline_rss = _outbound_import_rss()
    _queue_subprocess(
        """
        import sqlite3, sys
        from pathlib import Path
        from efb_telegram_master.outbound import OutboundQueue
        root = sys.argv[1]
        queue = OutboundQueue(root)
        queue.close()
        connection = sqlite3.connect(str(Path(root) / OutboundQueue.filename))
        connection.execute(
            "INSERT INTO outbound_queue(priority, telegram_chat_id, operation, payload, created_at) "
            "VALUES(0, 1, 'send_message', zeroblob(?), 0)", (int(sys.argv[2]),)
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        print("{}")
        """,
        tmp_path,
        size,
    )
    report = _queue_subprocess(
        """
        import json, resource, sys
        from efb_telegram_master.outbound import OutboundQueue
        queue = OutboundQueue(sys.argv[1])
        print(json.dumps({"rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
        queue.close()
        """,
        tmp_path,
    )
    assert report["rss_kib"] - baseline_rss < 32 * 1024, {"baseline_rss": baseline_rss, **report}


def test_recovery_streams_an_exact_381320453_byte_v1_payload_and_survives_restart(tmp_path):
    total_size = 381_320_453
    baseline_rss = _outbound_import_rss()
    prepared = _queue_subprocess(
        """
        import io, json, pickle, sqlite3, sys
        from pathlib import Path
        from efb_telegram_master.outbound import OutboundQueue
        root, total = sys.argv[1], int(sys.argv[2])
        sample_content = b"x" * 1024
        body = pickle.dumps(((1, io.BytesIO(sample_content)), {}), protocol=5)
        marker = b"B" + len(sample_content).to_bytes(4, "little") + sample_content
        offset = body.index(marker)
        content_length = total - 1 - offset - 5 - (len(body) - offset - len(marker))
        assert content_length > 0
        head = b"\x01" + body[:offset] + b"B" + content_length.to_bytes(4, "little")
        # Keep the source pickle valid; recovery removes FRAME before unpickling.
        head = head[:4] + (total - 1 - 11).to_bytes(8, "little") + head[12:]
        tail = body[offset + len(marker):]
        assert len(head) + content_length + len(tail) == total
        queue = OutboundQueue(root)
        queue.close()
        connection = sqlite3.connect(str(Path(root) / OutboundQueue.filename))
        connection.execute(
            "INSERT INTO outbound_queue(priority, telegram_chat_id, operation, payload, created_at) "
            "VALUES(0, 1, 'send_document', zeroblob(?), 0)", (total,)
        )
        row_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute(
            "UPDATE outbound_queue SET payload = CAST(? || zeroblob(?) || ? AS BLOB) WHERE id = ?",
            (head, content_length, tail, row_id),
        )
        stored_size = connection.execute(
            "SELECT length(CAST(payload AS BLOB)) FROM outbound_queue WHERE id = ?", (row_id,)
        ).fetchone()[0]
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        print(json.dumps({"row_id": row_id, "stored_size": stored_size, "media_size": content_length}))
        """,
        tmp_path,
        total_size,
    )
    assert prepared["stored_size"] == total_size

    recovered = _queue_subprocess(
        """
        import json, resource, sys
        import efb_telegram_master.outbound as outbound
        from efb_telegram_master.outbound import OutboundQueue
        requests = []
        stages = {}
        rss = lambda: resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        read_blob = outbound._SQLiteBlobReader.read
        def measured_read(self, size=-1):
            value = read_blob(self, size)
            requests.append(len(value))
            return value
        outbound._SQLiteBlobReader.read = measured_read
        queue = OutboundQueue(sys.argv[1])
        stages["opened"] = rss()
        stream = queue._stream_legacy_pickle
        def measured_stream(source, **kwargs):
            stages["before_stream"] = rss()
            value = stream(source, **kwargs)
            stages["after_stream"] = rss()
            return value
        queue._stream_legacy_pickle = measured_stream
        load = outbound._LegacyRecoveryUnpickler.load
        def measured_load(self):
            stages["before_unpickle"] = rss()
            value = load(self)
            stages["after_unpickle"] = rss()
            return value
        outbound._LegacyRecoveryUnpickler.load = measured_load
        queue.connection.set_trace_callback(
            lambda sql: stages.setdefault("before_update", rss())
            if sql.startswith("UPDATE outbound_queue SET payload") else None
        )
        row = queue.recover_legacy_media_payload(int(sys.argv[2]))
        stages["after_update"] = rss()
        args, _kwargs = queue.decode_payload(row.payload)
        media = args[1]
        media.seek(0)
        first = media.read(1)
        media.seek(-1, 2)
        last = media.read(1)
        size = media.seek(0, 2)
        media.close()
        print(json.dumps({
            "payload_version": row.payload[0], "payload_size": len(row.payload),
            "media_size": size, "first": first.hex(), "last": last.hex(),
            "rss_kib": rss(), "stages": stages,
            "helper_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
            "max_blob_read": max(requests),
        }))
        queue.close()
        """,
        tmp_path,
        prepared["row_id"],
    )
    assert recovered["payload_version"] == 2
    assert recovered["payload_size"] < 4096
    assert recovered["media_size"] == prepared["media_size"]
    assert recovered["first"] == recovered["last"] == "00"
    assert recovered["max_blob_read"] <= 1024 * 1024
    report = {"baseline_rss": baseline_rss, **recovered}
    assert recovered["rss_kib"] - baseline_rss < 32 * 1024, report
    assert recovered["helper_rss_kib"] - baseline_rss < 32 * 1024, report
    assert all(value - baseline_rss < 32 * 1024 for value in recovered["stages"].values()), report

    restarted = _queue_subprocess(
        """
        import asyncio, json, resource, sys
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        import httpx
        from telegram import Bot
        from telegram.request import BaseRequest
        from efb_telegram_master.outbound import (
            OutboundQueue, OutboundQueueScheduler, SenderSelection, SenderSelectionResult,
        )

        class UploadSink(BaseRequest):
            @property
            def read_timeout(self):
                return None
            async def initialize(self):
                pass
            async def shutdown(self):
                pass
            async def do_request(self, url, method, request_data=None, **kwargs):
                request = httpx.Request(method, url, data=request_data.json_parameters,
                                        files=request_data.multipart_data)
                self.uploaded = 0
                self.largest_chunk = 0
                for chunk in request.stream:
                    self.uploaded += len(chunk)
                    self.largest_chunk = max(self.largest_chunk, len(chunk))
                return 200, json.dumps({"ok": True, "result": {
                    "message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"},
                }}).encode()

        sink = UploadSink()
        bot = Bot("123:test", request=sink)
        class Adapter:
            def select_sender(self, row, now):
                return SenderSelectionResult(selection=SenderSelection(bot, None))
            def acquire_sender_limits(self, selection, chat_id):
                return True
            def execute_queued_call(self, row, args, kwargs, selection):
                args = OutboundQueue.streaming_uploads(args)
                kwargs = OutboundQueue.streaming_uploads(kwargs)
                return asyncio.run(bot.send_document(*args, **kwargs))
            def record_queued_success(self, *args):
                return SimpleNamespace(kind="success")

        queue = OutboundQueue(sys.argv[1])
        with ThreadPoolExecutor(max_workers=1) as executor:
            scheduler = OutboundQueueScheduler(queue, Adapter(), executor, worker_count=1)
            scheduler.dispatch_once()
            submitted = scheduler.in_flight[int(sys.argv[2])]
            assert submitted.future.result(timeout=30).message_id == 1
            scheduler.harvest_completed()
            assert not scheduler.stopping
            assert queue.heads() == []
            assert list(queue.media_dir.iterdir()) == []
            assert all(stream.closed for stream in submitted.closeables)
        print(json.dumps({"uploaded": sink.uploaded, "largest_chunk": sink.largest_chunk,
                          "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}))
        queue.close()
        """,
        tmp_path,
        prepared["row_id"],
    )
    assert prepared["media_size"] < restarted["uploaded"] < prepared["media_size"] + 4096
    assert restarted["largest_chunk"] <= 64 * 1024
    assert restarted["rss_kib"] - baseline_rss < 32 * 1024, {"baseline_rss": baseline_rss, **restarted}


def test_oversized_legacy_opaque_bytes_are_retained_without_sidecar_leaks(tmp_path):
    opaque_size = 32 * 1024 * 1024
    baseline_rss = _outbound_import_rss()
    prepared = _queue_subprocess(
        """
        import io, json, pickle, sqlite3, sys
        from pathlib import Path
        from efb_telegram_master.outbound import OutboundQueue
        root, opaque_size = sys.argv[1], int(sys.argv[2])
        queue = OutboundQueue(root)
        payload = b"\\x01" + pickle.dumps(
            ((1, io.BytesIO(b"small media")), {"opaque": b"x" * opaque_size}), protocol=5,
        )
        with queue.connection:
            cursor = queue.connection.execute(
                "INSERT INTO outbound_queue(priority,telegram_chat_id,operation,payload,created_at) "
                "VALUES(0,1,'send_document',?,0)", (payload,),
            )
        print(json.dumps({"row_id": cursor.lastrowid, "stored_size": len(payload)}))
        queue.close()
        """,
        tmp_path, opaque_size,
    )
    report = _queue_subprocess(
        """
        import json, resource, sys
        import efb_telegram_master.outbound as outbound
        from efb_telegram_master.outbound import OutboundQueue, OversizedQueuedPayloadError
        requested = []
        read_blob = outbound._SQLiteBlobReader.read
        def measured_read(self, size=-1):
            value = read_blob(self, size)
            requested.append(len(value))
            return value
        outbound._SQLiteBlobReader.read = measured_read
        queue = OutboundQueue(sys.argv[1])
        queue.MAX_REPLAY_BYTES = 1024 * 1024
        try:
            queue.recover_legacy_media_payload(int(sys.argv[2]))
        except OversizedQueuedPayloadError as error:
            retained = "Row retained" in str(error)
        else:
            retained = False
        with outbound._SQLiteBlobReader(queue.path, int(sys.argv[2])) as blob:
            version = blob.read(1)[0]
        print(json.dumps({
            "retained": retained, "version": version,
            "sidecars": [path.name for path in queue.media_dir.iterdir()],
            "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "max_blob_read": max(requested),
        }))
        queue.close()
        """,
        tmp_path, prepared["row_id"],
    )
    assert report["retained"] and report["version"] == 1
    assert report["sidecars"] == []
    assert report["max_blob_read"] <= 64 * 1024
    assert report["rss_kib"] - baseline_rss < 16 * 1024, {"baseline_rss": baseline_rss, **report}


def test_startup_cleanup_retains_sidecars_for_oversized_matching_v2_payload(tmp_path):
    size = 128 * 1024 * 1024
    baseline_rss = _outbound_import_rss()
    _queue_subprocess(
        """
        import pickle, sqlite3, sys
        from pathlib import Path
        from efb_telegram_master.outbound import OutboundQueue, _StoredMediaSnapshot
        root = Path(sys.argv[1])
        queue = OutboundQueue(root)
        (queue.media_dir / "media-live.bin").write_bytes(b"live")
        (queue.media_dir / "media-orphan.bin").write_bytes(b"orphan")
        payload = b"\\x02" + pickle.dumps(((), {
            "media": _StoredMediaSnapshot("media-live.bin", None),
            "opaque": b"x" * int(sys.argv[2]),
        }), protocol=5)
        with queue.connection:
            queue.connection.execute(
                "INSERT INTO outbound_queue(priority,telegram_chat_id,operation,payload,created_at) "
                "VALUES(0,1,'send_document',?,0)", (payload,),
            )
        queue.close()
        print("{}")
        """,
        tmp_path, size,
    )
    report = _queue_subprocess(
        """
        import json, resource, sys
        from efb_telegram_master.outbound import OutboundQueue
        queue = OutboundQueue(sys.argv[1])
        print(json.dumps({
            "rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "sidecars": sorted(path.name for path in queue.media_dir.iterdir()),
        }))
        queue.close()
        """,
        tmp_path,
    )
    assert report["sidecars"] == ["media-live.bin", "media-orphan.bin"]
    assert report["rss_kib"] - baseline_rss < 32 * 1024, {"baseline_rss": baseline_rss, **report}


def test_interrupted_legacy_stream_rolls_back_and_reclaims_crash_sidecar(tmp_path):
    queue = OutboundQueue(tmp_path)
    payload = b"\x01" + pickle.dumps(((1, io.BytesIO(b"legacy media")), {}), protocol=5)
    with queue.connection:
        cursor = queue.connection.execute(
            "INSERT INTO outbound_queue(priority,telegram_chat_id,operation,payload,created_at) "
            "VALUES(0,1,'send_document',?,0)", (payload,),
        )
    row_id = cursor.lastrowid
    queue.close()
    result = subprocess.run([
        sys.executable, "-c", textwrap.dedent("""
            import os, sys
            from efb_telegram_master.outbound import OutboundQueue
            queue = OutboundQueue(sys.argv[1])
            store = queue._store_media_stream
            def interrupted(*args, **kwargs):
                store(*args, **kwargs)
                os._exit(17)
            queue._store_media_stream = interrupted
            queue.recover_legacy_media_payload(int(sys.argv[2]))
        """), str(tmp_path), str(row_id),
    ], check=False)
    assert result.returncode == 17
    assert len(list((tmp_path / "outbound-media").iterdir())) == 1
    queue = OutboundQueue(tmp_path)
    try:
        assert list(queue.media_dir.iterdir()) == []
        assert queue.load_queued(row_id).payload == payload
        row = queue.recover_legacy_media_payload(row_id)
        args, kwargs = queue.decode_payload(row.payload)
        try:
            assert args[1].read() == b"legacy media"
        finally:
            queue.close_payload_resources(queue.payload_closeables(args, kwargs))
    finally:
        queue.close()
