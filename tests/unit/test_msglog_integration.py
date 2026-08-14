import asyncio
import gc
import threading
import time
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
from efb_telegram_master.msglog_scan import MsgLogScanScheduler, MsgLogScanShutdownTimeout
from efb_telegram_master.mtproto import MTProtoRetryableError
from efb_telegram_master.slave_message import ETMMsg, SlaveMessageService
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


class FakeLinkCompletion:
    def complete(self, _update: Update, _args: list[str]) -> None:
        return None


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
        FakeLinkCompletion(),
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


def test_association_reschedule_resets_completed_scan_before_starting_worker():
    class Runtime:
        def call(self, coroutine, timeout=None):
            coroutine.close()

    ingestion = SimpleNamespace(
        request_association_rescan=Mock(return_value="pending"),
        get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)),
        release_scan=Mock(),
    )
    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=Runtime()),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10)),
        ingestion,
        Mock(),
        Mock(),
    )
    try:
        assert scheduler.schedule_for_association(100) == "started"
        ingestion.request_association_rescan.assert_called_once_with(100)
        ingestion.get_or_create_scan.assert_called_once_with(100, 10)
    finally:
        assert scheduler.stop(1) == ()


def test_association_reschedule_does_not_start_a_new_scan():
    class Runtime:
        def call(self, coroutine, timeout=None):
            coroutine.close()

    ingestion = SimpleNamespace(
        request_association_rescan=Mock(return_value=None),
        release_scan=Mock(),
    )
    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=Runtime()),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10)),
        ingestion,
        Mock(),
        Mock(),
    )
    try:
        assert scheduler.schedule_for_association(100) == "unchanged"
        ingestion.request_association_rescan.assert_called_once_with(100)
    finally:
        assert scheduler.stop(1) == ()


def test_association_reschedule_starts_an_existing_retryable_scan():
    class Runtime:
        def call(self, coroutine, timeout=None):
            coroutine.close()

    retryable_scan = SimpleNamespace(status="retryable-error", scanned_count=1)
    ingestion = SimpleNamespace(
        request_association_rescan=Mock(return_value="retryable-error"),
        get_or_create_scan=Mock(return_value=retryable_scan),
        release_scan=Mock(),
    )
    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=Runtime()),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10)),
        ingestion,
        Mock(),
        Mock(),
    )
    try:
        assert scheduler.schedule_for_association(100) == "resumed"
        ingestion.request_association_rescan.assert_called_once_with(100)
        ingestion.get_or_create_scan.assert_called_once_with(100, 10)
    finally:
        assert scheduler.stop(1) == ()


def test_association_reschedule_uses_persisted_request_when_another_worker_is_running():
    class Runtime:
        def call(self, coroutine, timeout=None):
            coroutine.close()

    ingestion = SimpleNamespace(
        request_association_rescan=Mock(return_value="running"),
        get_or_create_scan=Mock(return_value=SimpleNamespace(status="running", scanned_count=0)),
        release_scan=Mock(),
    )
    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=Runtime()),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10)),
        ingestion,
        Mock(),
        Mock(),
    )
    try:
        assert scheduler.schedule_for_association(100) == "queued"
        ingestion.request_association_rescan.assert_called_once_with(100)
    finally:
        assert scheduler.stop(1) == ()


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


def test_msglog_scan_stop_releases_lease_and_rejects_new_workers():
    started, release = threading.Event(), threading.Event()

    class Runtime:
        def __init__(self):
            self.calls = 0
            self.reject_new_calls = False

        def call(self, coroutine, timeout=None):
            self.calls += 1
            coroutine.close()
            if self.reject_new_calls:
                raise RuntimeError("Telegram runtime is stopping.")
            if self.calls == 1:
                started.set()
                release.wait()

    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=Runtime()), SimpleNamespace(enabled=True, connected=True, config=SimpleNamespace(scan_ceiling=10)), ingestion, Mock(), Mock())
    try:
        assert scheduler.schedule(100) == "started"
        assert started.wait(1)
        assert scheduler.schedule(100) == "already running"
        scheduler.runtime.async_runtime.reject_new_calls = True
        errors = scheduler.stop(0.01)
        assert len(errors) == 1
        assert isinstance(errors[0], MsgLogScanShutdownTimeout)
        assert scheduler.runtime.async_runtime.calls == 2
        ingestion.get_or_create_scan.assert_called_once_with(100, 10)
        assert scheduler.schedule(200) == "stopping"
        release.set()
        assert scheduler.stop(1) == ()
        ingestion.release_scan.assert_called_once()
        assert not any(thread.name == "MsgLogIngestion-100" and thread.is_alive() for thread in threading.enumerate())
    finally:
        release.set()
        scheduler.stop(1)


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
    runtime = SharedAsyncRuntime()

    async def connect():
        return None

    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=runtime),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10, scan_concurrency=1), connect=connect),
        ingestion,
        Mock(),
        Mock(),
    )
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
        scheduler.stop(1)
        runtime.close()


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


def test_ordinary_send_writes_msglog_once_and_completes_delivery_claim(monkeypatch):
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.msglogs = SimpleNamespace(add_or_update_message_log=Mock())
    processor.delivery_claims = Mock()
    processor.chat_manager = Mock()
    processor.router = Mock(resolve_reply=Mock(return_value=None))
    processor.commands = SimpleNamespace(register_command=Mock())
    sent = SimpleNamespace(chat=SimpleNamespace(id=123), message_id=456, sender_bot_id="7")
    processor.text_delivery = Mock(text=Mock(return_value=sent))
    etm_msg = Mock()
    monkeypatch.setattr(ETMMsg, "from_efbmsg", Mock(return_value=etm_msg))
    monkeypatch.setattr("efb_telegram_master.slave_message.get_msg_type", Mock(return_value="Text"))
    message = SimpleNamespace(
        uid="slave-message",
        target=None,
        commands=[],
        reactions={},
        text="hello",
        type=MsgType.Text,
    )

    processor.dispatch_message(message, "", None, 123, None, dedupe_key=("slave", "slave-message"), claim_token="claim-token")

    processor.msglogs.add_or_update_message_log.assert_called_once_with(
        etm_msg,
        sent,
        None,
        sender_bot_id="7",
    )
    processor.delivery_claims.complete.assert_called_once_with("slave", "slave-message", "claim-token")
