import asyncio
import gc
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot import Message
from ehforwarderbot.constants import MsgType
from ehforwarderbot.types import MessageID
from telegram import Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.channel_commands import LocaleState, TelegramCommandService
from efb_telegram_master.msglog_ingestion import MsgLogIngestionService
from efb_telegram_master.msglog_scan import MsgLogScanScheduler, MsgLogScanShutdownTimeout
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
        reset_completed_scan=Mock(return_value=True),
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
        ingestion.reset_completed_scan.assert_called_once_with(100)
        ingestion.get_or_create_scan.assert_called_once_with(100, 10)
    finally:
        assert scheduler.stop(1) == ()


def test_association_during_active_scan_queues_one_reset_follow_up(monkeypatch):
    first_started, release_first, follow_up_started = threading.Event(), threading.Event(), threading.Event()
    run_owners = []
    persisted = []
    scan = SimpleNamespace(status="pending", scanned_count=0)
    associations = SimpleNamespace(slave_uid=None)

    def reset_completed_scan(_source_chat_id):
        if scan.status != "complete":
            return False
        scan.status = "pending"
        return True

    async def run(_service, _source_chat_id, *, lease_owner, stop_requested):
        run_owners.append(lease_owner)
        if len(run_owners) == 1:
            scan.status = "running"
            persisted.append(("unbound-topic", None))
            first_started.set()
            await asyncio.to_thread(release_first.wait)
            scan.status = "complete"
        else:
            assert scan.status == "pending"
            scan.status = "running"
            persisted.append(("eligible", associations.slave_uid))
            scan.status = "complete"
            follow_up_started.set()

    monkeypatch.setattr(MsgLogIngestionService, "run", run)
    ingestion = SimpleNamespace(
        get_or_create_scan=Mock(return_value=scan),
        reset_completed_scan=Mock(side_effect=reset_completed_scan),
        release_scan=Mock(),
    )
    runtime = SharedAsyncRuntime()

    async def connect():
        return None

    scheduler = MsgLogScanScheduler(
        SimpleNamespace(async_runtime=runtime),
        SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10), connect=connect),
        ingestion,
        associations,
        Mock(),
    )
    try:
        assert scheduler.schedule(100) == "started"
        assert first_started.wait(1)
        associations.slave_uid = "tests.linked-slave"
        assert scheduler.schedule_for_association(100) == "queued"
        release_first.set()
        assert follow_up_started.wait(1)
        assert persisted == [("unbound-topic", None), ("eligible", "tests.linked-slave")]
        assert ingestion.reset_completed_scan.call_count == 2
    finally:
        release_first.set()
        assert scheduler.stop(1) == ()
        runtime.close()


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
