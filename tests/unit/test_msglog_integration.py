import asyncio
import gc
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot import Message
from ehforwarderbot.constants import MsgType
from ehforwarderbot.types import MessageID
from peewee import SqliteDatabase
from telegram import Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.channel_commands import LocaleState, TelegramCommandService
from efb_telegram_master.models import MsgLog, MsgLogIngestionScan, database
from efb_telegram_master.msglog_ingestion import MsgLogIngestionService
from efb_telegram_master.msglog_ingestion_repository import MsgLogIngestionRepository
from efb_telegram_master.msglog_scan import MsgLogScanScheduler
from efb_telegram_master.mtproto import MTProtoRetryableError
from efb_telegram_master.slave_message import SlaveMessageService
from efb_telegram_master.slave_status import SlaveStatusService


class FakeMessageIdentifier:
    message_id = 1


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, **_kwargs: object) -> FakeMessageIdentifier:
        self.calls.append((chat_id, text))
        return FakeMessageIdentifier()


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


class FakeAssociations:
    def __init__(self, topics: list[tuple[str, int]]) -> None:
        self.topics = topics
        self.lookups: list[int] = []

    def get_topic_slaves(self, group_id: int) -> list[tuple[str, int]]:
        self.lookups.append(group_id)
        return self.topics


class FakeScanScheduler:
    def __init__(self) -> None:
        self.scheduled: list[int] = []

    def schedule(self, group_id: int) -> str:
        self.scheduled.append(group_id)
        return "started"


@pytest.fixture
def sync_msglog_service() -> TelegramCommandService:
    return TelegramCommandService(
        "tests.master",
        None,
        "test",
        FakeAPI(),
        FakeAssociations([("tests.slave", 7)]),
        Mock(),
        Mock(),
        Mock(),
        FakeScanScheduler(),
        Mock(),
        [10],
        None,
        Mock(),
        LocaleState(),
    )


def sync_msglog_update(*, user_id=10, is_forum=True):
    message = Mock()
    message.chat = SimpleNamespace(id=100, is_forum=is_forum)
    message.from_user = SimpleNamespace(id=user_id)
    message.message_thread_id = None
    return Update(update_id=1, message=message)


def test_sync_msglog_schedules_for_admin_in_bound_forum_group(sync_msglog_service):
    service = sync_msglog_service

    service.sync_msglog(sync_msglog_update(), Mock())

    assert service.chat_associations.lookups == [100]
    assert service.msglog_scan.scheduled == [100]
    assert service.api.calls == [(100, "MsgLog sync started for this group.")]


@pytest.mark.parametrize(
    ("user_id", "is_forum", "bound_topics", "expected_reply", "topic_lookup_expected"),
    [
        (11, True, [("tests.slave", 7)], "This command is for ETM admins only.", False),
        (10, False, [("tests.slave", 7)], "This command must be used in a bound forum group.", False),
        (10, True, [], "This forum group has no bound topics.", True),
    ],
    ids=["non-admin", "non-forum", "no-bound-topics"],
)
def test_sync_msglog_rejects_unqualified_requests(sync_msglog_service, user_id, is_forum, bound_topics, expected_reply, topic_lookup_expected):
    service = sync_msglog_service
    service.chat_associations.topics = bound_topics

    service.sync_msglog(sync_msglog_update(user_id=user_id, is_forum=is_forum), Mock())

    if topic_lookup_expected:
        assert service.chat_associations.lookups == [100]
    else:
        assert service.chat_associations.lookups == []
    assert service.msglog_scan.scheduled == []
    assert service.api.calls == [(100, expected_reply)]


def test_sync_msglog_ignores_updates_without_an_effective_message(sync_msglog_service):
    service = sync_msglog_service

    service.sync_msglog(Update(update_id=1), Mock())

    assert service.chat_associations.lookups == []
    assert service.msglog_scan.scheduled == []
    assert service.api.calls == []


def test_resume_msglog_ingestions_schedules_each_bound_retryable_group():
    manager = object.__new__(MsgLogScanScheduler)
    manager.ingestion = SimpleNamespace(
        get_resumable_scans=Mock(
            return_value=[
                SimpleNamespace(source_chat_id="100"),
                SimpleNamespace(source_chat_id="200"),
            ]
        )
    )
    manager.chat_associations = SimpleNamespace(get_topic_slaves=Mock(side_effect=[[("a", 1)], [("b", 2)]]))
    manager.schedule = Mock()
    manager.logger = Mock()

    MsgLogScanScheduler.resume(manager)

    assert [call.args for call in manager.schedule.call_args_list] == [(100,), (200,)]


@pytest.mark.parametrize(
    ("scan_status", "scanned_count", "enabled", "stop_during_request", "expected", "creates_scan"),
    [
        ("pending", 0, True, False, "started", True),
        (None, 0, True, False, "unchanged", False),
        ("retryable-error", 1, True, False, "resumed", True),
        ("running", 0, True, False, "queued", True),
        ("running", 0, False, False, "unavailable", False),
        ("running", 0, True, True, "stopping", False),
    ],
    ids=["pending-start", "no-scan", "retryable-resume", "running-queue", "unavailable", "stopping"],
)
def test_association_reschedule_transitions(scan_status, scanned_count, enabled, stop_during_request, expected, creates_scan):
    class Runtime:
        def call(self, coroutine, timeout=None):
            coroutine.close()

    ingestion = SimpleNamespace(
        request_association_rescan=Mock(return_value=scan_status),
        get_or_create_scan=Mock(return_value=SimpleNamespace(status=scan_status, scanned_count=scanned_count)),
        release_scan=Mock(),
    )
    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=Runtime()),
        SimpleNamespace(enabled=enabled, config=SimpleNamespace(scan_ceiling=10)),
        ingestion,
        Mock(),
        Mock(),
    )
    if stop_during_request:
        ingestion.request_association_rescan.side_effect = lambda _source_chat_id: (setattr(scheduler, "_stopping", True), "running")[1]
    try:
        assert scheduler.schedule_for_association(100) == expected
        ingestion.request_association_rescan.assert_called_once_with(100)
        if creates_scan:
            ingestion.get_or_create_scan.assert_called_once_with(100, 10)
        else:
            ingestion.get_or_create_scan.assert_not_called()
    finally:
        assert scheduler.stop(1) == ()


@contextmanager
def msglog_scan_scheduler(ingestion, *, scan_concurrency=1):
    runtime = SharedAsyncRuntime()

    async def connect():
        return None

    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=runtime),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10, scan_concurrency=scan_concurrency), connect=connect),
        ingestion,
        Mock(),
        Mock(),
    )
    try:
        yield scheduler
    finally:
        scheduler.stop(1)
        runtime.close()


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
                return [
                    SimpleNamespace(
                        id=1,
                        message="message 1",
                        date=None,
                        reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=10, reply_to_msg_id=None),
                        action=None,
                        media=None,
                    )
                ]
            finally:
                active_fetches -= 1

    original_database = database.obj
    test_db = SqliteDatabase(tmp_path / "msglog.db")
    database.initialize(test_db)
    test_db.connect()
    ingestion = MsgLogIngestionRepository("tests.master")
    runtime = SharedAsyncRuntime()
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=runtime), MTProto(), ingestion, Associations(), Mock())
    try:
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
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
        test_db.close()
        database.initialize(original_database)

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
            return [
                SimpleNamespace(
                    id=1,
                    message="message 1",
                    date=None,
                    reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=10, reply_to_msg_id=None),
                    action=None,
                    media=None,
                )
            ]

    class TrackingRepository(MsgLogIngestionRepository):
        def claim_scan(self, source_chat_id, lease_owner, lease_seconds):
            claimed = super().claim_scan(source_chat_id, lease_owner, lease_seconds)
            if lease_owner != "other-process" and claimed is None:
                rejected_claim.set()
            return claimed

    original_database = database.obj
    test_db = SqliteDatabase(tmp_path / "msglog.db")
    database.initialize(test_db)
    test_db.connect()
    ingestion = TrackingRepository("tests.master")
    runtime = SharedAsyncRuntime()
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=runtime), MTProto(), ingestion, Associations(), Mock())
    try:
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
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
        test_db.close()
        database.initialize(original_database)

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
            return [
                SimpleNamespace(
                    id=1,
                    message="message 1",
                    date=None,
                    reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=10, reply_to_msg_id=None),
                    action=None,
                    media=None,
                )
            ]

    original_database = database.obj
    test_db = SqliteDatabase(tmp_path / "msglog.db")
    database.initialize(test_db)
    test_db.connect()
    ingestion = MsgLogIngestionRepository("tests.master")
    runtime = SharedAsyncRuntime()
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=runtime), MTProto(), ingestion, Associations(), Mock())
    try:
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
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
        test_db.close()
        database.initialize(original_database)

    assert (recovered.status, recovered.rescan_requested, recovered.lease_owner) == ("complete", False, None)
    assert (row.provenance, row.slave_origin_uid) == ("mtproto_ingested", "tests.slave target")
    assert fetches == 3


def test_msglog_scan_stop_does_not_double_release_a_gracefully_stopped_lease(monkeypatch):
    started = threading.Event()
    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())

    with msglog_scan_scheduler(ingestion) as scheduler:

        async def run(_service, source_chat_id, *, lease_owner, stop_requested):
            started.set()
            await asyncio.to_thread(scheduler._stop_event.wait)
            assert stop_requested()
            ingestion.release_scan(source_chat_id, lease_owner)
            return True

        monkeypatch.setattr(MsgLogIngestionService, "run", run)
        assert scheduler.schedule(100) == "started"
        assert started.wait(1)
        assert scheduler.schedule(100) == "already running"
        assert scheduler.stop(1) == ()
        ingestion.get_or_create_scan.assert_called_once_with(100, 10)
        assert scheduler.schedule(200) == "stopping"
        ingestion.release_scan.assert_called_once()
        assert not any(thread.name == "MsgLogIngestion-100" and thread.is_alive() for thread in threading.enumerate())


def test_msglog_scan_stop_releases_a_lease_after_an_interrupted_service(monkeypatch):
    started = threading.Event()
    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())

    with msglog_scan_scheduler(ingestion) as scheduler:

        async def run(_service, _source_chat_id, *, lease_owner, stop_requested):
            started.set()
            await asyncio.to_thread(scheduler._stop_event.wait)
            raise RuntimeError(f"interrupted {lease_owner}")

        monkeypatch.setattr(MsgLogIngestionService, "run", run)
        assert scheduler.schedule(100) == "started"
        assert started.wait(1)
        assert scheduler.stop(1) == ()
        ingestion.release_scan.assert_called_once()
        assert ingestion.release_scan.call_args.args[0] == 100


def test_msglog_scan_closes_runtime_coroutines_when_runtime_call_raises(recwarn):
    class RejectingRuntime:
        def call(self, _coroutine, timeout=None):
            raise RuntimeError("Telegram runtime is stopping.")

    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())
    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=RejectingRuntime()),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10)),
        ingestion,
        Mock(),
        Mock(),
    )

    assert scheduler.schedule(100) == "started"
    assert scheduler.stop(1) == ()
    gc.collect()

    assert not [warning for warning in recwarn if "was never awaited" in str(warning.message)]


def test_msglog_scan_caps_concurrent_workers_and_admits_pending_groups_fairly(monkeypatch):
    started, release = [], threading.Event()
    first_two_started, third_started = threading.Event(), threading.Event()

    async def run(_service, source_chat_id, *, lease_owner, stop_requested):
        assert lease_owner
        assert not stop_requested()
        started.append(source_chat_id)
        if len(started) == 2:
            first_two_started.set()
        if source_chat_id == 300:
            third_started.set()
        await asyncio.to_thread(release.wait)

    monkeypatch.setattr(MsgLogIngestionService, "run", run)
    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())
    runtime = SharedAsyncRuntime()

    async def connect():
        return None

    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=runtime),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10, scan_concurrency=2), connect=connect),
        ingestion,
        Mock(),
        Mock(),
    )
    try:
        assert scheduler.schedule(100) == "started"
        assert scheduler.schedule(200) in {"started", "queued"}
        assert first_two_started.wait(1)
        assert scheduler.schedule(300) == "queued"
        assert set(started) == {100, 200}

        release.set()
        assert third_started.wait(1)
        assert started[-1] == 300
    finally:
        release.set()
        assert scheduler.stop(1) == ()
        runtime.close()


def test_msglog_scan_shutdown_discards_pending_groups(monkeypatch):
    started, release = [], threading.Event()
    first_started = threading.Event()

    async def run(_service, source_chat_id, *, lease_owner, stop_requested):
        assert lease_owner
        started.append(source_chat_id)
        first_started.set()
        await asyncio.to_thread(release.wait)

    monkeypatch.setattr(MsgLogIngestionService, "run", run)
    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())
    with msglog_scan_scheduler(ingestion) as scheduler:
        try:
            assert scheduler.schedule(100) == "started"
            assert first_started.wait(1)
            assert scheduler.schedule(200) == "queued"
            assert len(scheduler.stop(0.01)) == 1
            release.set()
            assert scheduler.stop(1) == ()
            assert started == [100]
        finally:
            release.set()


def test_ingested_rows_are_not_remote_get_or_reaction_targets():
    row = SimpleNamespace(provenance="mtproto_ingested")
    chat = SimpleNamespace(module_id="tests.slave", uid="chat")
    channel = object.__new__(TelegramChannel)
    channel.msglogs = SimpleNamespace(get_msg_log=Mock(return_value=row))
    channel.chat_manager = Mock()

    assert TelegramChannel.get_message_by_id(channel, chat, "mtproto-ingested:100.1") is None

    processor = object.__new__(SlaveStatusService)
    processor.msglogs = SimpleNamespace(get_msg_log=Mock(return_value=row))
    processor.logger = Mock()
    processor.router = Mock()
    processor.update_reactions(SimpleNamespace(chat=chat, msg_id="mtproto-ingested:100.1", reactions={}))

    processor.logger.info.assert_called_once()


@pytest.mark.parametrize(
    ("provenance", "expected_target_msg_id"),
    [("mtproto_ingested", None), ("live", 456)],
)
def test_dispatch_reply_target_respects_provenance(provenance, expected_target_msg_id):
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.msglogs = SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(master_msg_id="123.456", provenance=provenance)))
    processor.chat_manager = Mock()
    processor.router = Mock(resolve_reply=Mock(return_value=expected_target_msg_id))
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.text_delivery = Mock(text=Mock(return_value=None))
    processor._release_pending_slave_message = Mock()
    target = Message(uid=MessageID("recovered"), chat=SimpleNamespace(module_id="tests.slave", uid="chat"))
    message = Message(uid=MessageID("reply"), chat=SimpleNamespace(module_id="tests.slave", uid="chat"), target=target, text="reply", type=MsgType.Text)

    processor.dispatch_message(message, "", None, 123, None)

    assert processor.text_delivery.text.call_args.args[6] == expected_target_msg_id
