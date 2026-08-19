import asyncio
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from peewee import SqliteDatabase

from efb_telegram_master.models import MsgLog, MsgLogIngestionScan, database
from efb_telegram_master.msglog_scan import MsgLogScanScheduler
from efb_telegram_master.mtproto import MTProtoRetryableError
from efb_telegram_master.persistence.msglog_ingestion_repository import MsgLogIngestionRepository


class SharedAsyncRuntime:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="TestSharedAsyncRuntime")
        self.thread.start()
        assert self.ready.wait(1)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    def call(self, coroutine, timeout=None):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(1)
        self.loop.close()


def ingested_topic_message():
    return SimpleNamespace(
        id=1,
        message="message 1",
        date=None,
        reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=10, reply_to_msg_id=None),
        action=None,
        media=None,
    )


@contextmanager
def msglog_scan_database(tmp_path):
    original_database = database.obj
    test_db = SqliteDatabase(tmp_path / "msglog.db")
    database.initialize(test_db)
    test_db.connect()
    try:
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
        yield
    finally:
        test_db.close()
        database.initialize(original_database)


def test_association_reschedule_queues_a_successor_after_active_lease_expires(tmp_path):
    first_fetch_started = threading.Event()
    allow_expired_worker_to_exit = threading.Event()
    fetches = 0
    active_fetches = 0
    max_active_fetches = 0

    class Associations:
        def get_topic_assoc_slave_uid(self, source_chat_id, topic_id):
            assert (source_chat_id, topic_id) == (100, 10)
            return "tests.slave target"

    class MTProto:
        enabled = True
        config = SimpleNamespace(scan_ceiling=1, scan_concurrency=2)

        async def connect(self):
            return None

        async def get_input_channel(self, source_chat_id):
            return source_chat_id

        async def get_channel_messages(self, _channel, message_ids):
            nonlocal active_fetches, fetches, max_active_fetches
            assert message_ids == [1]
            fetches += 1
            active_fetches += 1
            max_active_fetches = max(max_active_fetches, active_fetches)
            try:
                if fetches == 1:
                    first_fetch_started.set()
                    await asyncio.to_thread(allow_expired_worker_to_exit.wait)
                return [ingested_topic_message()]
            finally:
                active_fetches -= 1

    with msglog_scan_database(tmp_path):
        ingestion = MsgLogIngestionRepository("tests.master")
        runtime = SharedAsyncRuntime()
        scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=runtime), MTProto(), ingestion, Associations(), Mock())
        try:
            scan = ingestion.get_or_create_scan(100, 1)
            assert scheduler.schedule(100) == "started"
            assert first_fetch_started.wait(1)
            assert scheduler.schedule_for_association(100) == "queued"
            MsgLogIngestionScan.update(
                status="running",
                lease_expires_at=datetime.now() - timedelta(seconds=1),
            ).where(MsgLogIngestionScan.id == scan.id).execute()

            allow_expired_worker_to_exit.set()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if MsgLogIngestionScan.get_by_id(scan.id).status == "complete":
                    break
                time.sleep(0.01)
            recovered = MsgLogIngestionScan.get_by_id(scan.id)
            row = MsgLog.get_by_id("100.1")
        finally:
            allow_expired_worker_to_exit.set()
            assert scheduler.stop(1) == ()
            runtime.close()

    assert (recovered.status, recovered.rescan_requested, recovered.lease_owner) == ("complete", False, None)
    assert (row.provenance, row.slave_origin_uid) == ("mtproto_ingested", "tests.slave target")
    assert fetches == 3
    assert max_active_fetches == 1


@pytest.mark.parametrize(
    ("terminal_state", "prior_process"),
    [("expired", False), ("retryable-error", False), ("expired", True)],
    ids=["external-expiry", "external-retryable-error", "prior-process-expiry"],
)
def test_association_reschedule_recovers_untracked_active_lease(tmp_path, terminal_state, prior_process):
    rejected_claim = threading.Event()
    fetches = 0

    class Associations:
        def get_topic_assoc_slave_uid(self, source_chat_id, topic_id):
            assert (source_chat_id, topic_id) == (100, 10)
            return "tests.slave target"

        def get_topic_slaves(self, source_chat_id):
            assert source_chat_id == 100
            return [("tests.slave", 10)]

    class MTProto:
        enabled = True
        config = SimpleNamespace(scan_ceiling=1)

        async def connect(self):
            return None

        async def get_input_channel(self, source_chat_id):
            return source_chat_id

        async def get_channel_messages(self, _channel, message_ids):
            nonlocal fetches
            assert message_ids == [1]
            fetches += 1
            return [ingested_topic_message()]

    class TrackingRepository(MsgLogIngestionRepository):
        def claim_scan(self, source_chat_id, lease_owner, lease_seconds):
            claimed = super().claim_scan(source_chat_id, lease_owner, lease_seconds)
            if lease_owner != "other-process" and claimed is None:
                rejected_claim.set()
            return claimed

    with msglog_scan_database(tmp_path):
        ingestion = TrackingRepository("tests.master")
        runtime = SharedAsyncRuntime()
        scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=runtime), MTProto(), ingestion, Associations(), Mock())
        try:
            scan = ingestion.get_or_create_scan(100, 1)
            assert ingestion.claim_scan(100, "other-process", 1) is not None
            MsgLogIngestionScan.update(lease_expires_at=datetime.now() + timedelta(milliseconds=100)).where(MsgLogIngestionScan.id == scan.id).execute()
            if prior_process:
                scheduler.resume()
                assert not rejected_claim.is_set()

            assert scheduler.schedule_for_association(100) == "queued"
            assert rejected_claim.wait(1)
            if terminal_state == "retryable-error":
                MsgLogIngestionScan.update(status="retryable-error", lease_owner=None, lease_expires_at=None).where(MsgLogIngestionScan.id == scan.id).execute()

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if MsgLogIngestionScan.get_by_id(scan.id).status == "complete":
                    break
                time.sleep(0.01)
            recovered = MsgLogIngestionScan.get_by_id(scan.id)
            row = MsgLog.get_by_id("100.1")
        finally:
            assert scheduler.stop(1) == ()
            runtime.close()

    assert (recovered.status, recovered.rescan_requested, recovered.lease_owner) == ("complete", False, None)
    assert (row.provenance, row.slave_origin_uid) == ("mtproto_ingested", "tests.slave target")
    assert fetches == 2


def test_association_reschedule_reports_queued_when_resume_has_pending_source():
    admission_started = threading.Event()
    allow_admission = threading.Event()

    class Runtime:
        def __init__(self):
            self.calls = 0

        def call(self, coroutine, timeout=None):
            try:
                if self.calls == 0:
                    self.calls += 1
                    admission_started.set()
                    allow_admission.wait()
                return None
            finally:
                coroutine.close()

    runtime = Runtime()
    ingestion = SimpleNamespace(
        get_resumable_scans=Mock(return_value=[SimpleNamespace(source_chat_id="100")]),
        get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)),
        request_association_rescan=Mock(return_value="pending"),
    )
    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=runtime),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=1, scan_concurrency=1)),
        ingestion,
        SimpleNamespace(get_topic_slaves=Mock(return_value=[("tests.slave", 10)])),
        Mock(),
    )
    try:
        assert scheduler.schedule(200) == "started"
        assert admission_started.wait(1)

        scheduler.resume()
        assert scheduler.schedule(100) == "already running"
        assert scheduler.schedule_for_association(100) == "queued"
    finally:
        allow_admission.set()
        assert scheduler.stop(1) == ()


def test_association_reschedule_queues_a_successor_after_retryable_error(tmp_path):
    first_fetch_started = threading.Event()
    allow_retryable_error = threading.Event()
    fetches = 0

    class Associations:
        def get_topic_assoc_slave_uid(self, source_chat_id, topic_id):
            assert (source_chat_id, topic_id) == (100, 10)
            return "tests.slave target"

    class MTProto:
        enabled = True
        config = SimpleNamespace(scan_ceiling=1)

        async def connect(self):
            return None

        async def get_input_channel(self, source_chat_id):
            return source_chat_id

        async def get_channel_messages(self, _channel, message_ids):
            nonlocal fetches
            assert message_ids == [1]
            fetches += 1
            if fetches == 1:
                first_fetch_started.set()
                await asyncio.to_thread(allow_retryable_error.wait)
                raise MTProtoRetryableError("temporary")
            return [ingested_topic_message()]

    with msglog_scan_database(tmp_path):
        ingestion = MsgLogIngestionRepository("tests.master")
        runtime = SharedAsyncRuntime()
        scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=runtime), MTProto(), ingestion, Associations(), Mock())
        try:
            scan = ingestion.get_or_create_scan(100, 1)
            assert scheduler.schedule(100) == "started"
            assert first_fetch_started.wait(1)
            assert scheduler.schedule_for_association(100) == "queued"
            allow_retryable_error.set()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if MsgLogIngestionScan.get_by_id(scan.id).status == "complete":
                    break
                time.sleep(0.01)
            recovered = MsgLogIngestionScan.get_by_id(scan.id)
            row = MsgLog.get_by_id("100.1")
        finally:
            allow_retryable_error.set()
            assert scheduler.stop(1) == ()
            runtime.close()

    assert (recovered.status, recovered.rescan_requested, recovered.lease_owner) == ("complete", False, None)
    assert (row.provenance, row.slave_origin_uid) == ("mtproto_ingested", "tests.slave target")
    assert fetches == 3
