import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot import Message
from ehforwarderbot.constants import MsgType
from ehforwarderbot.types import MessageID
from telegram import Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.channel_commands import LocaleState, TelegramCommandService
from efb_telegram_master.msglog_scan import MsgLogScanScheduler, MsgLogScanShutdownTimeout
from efb_telegram_master.slave_message import ETMMsg, SlaveMessageService
from efb_telegram_master.slave_status import SlaveStatusService
from efb_telegram_master.telegram_sync_bridge import AsyncTelegramRuntime


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
        self.second_submission = threading.Event()
        self._calls = 0
        self._calls_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="TestSharedAsyncRuntime")
        self.thread.start()
        assert self.ready.wait(1)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    def call(self, coroutine, timeout=None):
        with self._calls_lock:
            self._calls += 1
            if self._calls == 2:
                self.second_submission.set()
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


def test_msglog_scan_stop_releases_lease_and_rejects_new_workers():
    started, release = threading.Event(), threading.Event()

    class Runtime:
        def __init__(self):
            self.calls = 0

        def call(self, coroutine, timeout=None):
            self.calls += 1
            coroutine.close()
            if self.calls == 1:
                started.set()
                release.wait()

    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=Runtime()), SimpleNamespace(enabled=True, connected=True, config=SimpleNamespace(scan_ceiling=10)), ingestion, Mock(), Mock())
    try:
        assert scheduler.schedule(100) == "started"
        assert started.wait(1)
        assert scheduler.schedule(100) == "already running"
        errors = scheduler.stop(0.01)
        assert len(errors) == 1
        assert isinstance(errors[0], MsgLogScanShutdownTimeout)
        assert scheduler.schedule(200) == "stopping"
        release.set()
        assert scheduler.stop(1) == ()
        ingestion.release_scan.assert_called_once()
        assert not any(thread.name == "MsgLogIngestion-100" and thread.is_alive() for thread in threading.enumerate())
    finally:
        release.set()
        scheduler.stop(1)


def test_msglog_scan_stop_uses_one_deadline_for_all_stalled_workers(monkeypatch):
    class StalledWorker:
        def __init__(self, name):
            self.name = name
            self.join_timeouts = []

        def join(self, timeout):
            self.join_timeouts.append(timeout)
            clock[0] += timeout

        def is_alive(self):
            return True

    clock = [0.0]
    first, second = StalledWorker("MsgLogIngestion-100"), StalledWorker("MsgLogIngestion-200")
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=Mock()), SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10)), Mock(), Mock(), Mock())
    scheduler._stopping = True
    scheduler._threads = {100: first, 200: second}
    monkeypatch.setattr("efb_telegram_master.msglog_scan.time.monotonic", lambda: clock[0])

    errors = scheduler.stop(5)

    assert len(errors) == 1
    assert first.join_timeouts == [5]
    assert second.join_timeouts == [0]


def test_msglog_scan_stop_waits_for_unregistered_worker_exit():
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="MsgLogIngestion-100")
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=Mock()), SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10)), Mock(), Mock(), Mock())
    scheduler._stopping = True
    scheduler._retiring_threads = {worker}
    worker.start()
    try:
        assert len(scheduler.stop(0.01)) == 1
        assert worker.is_alive()
        release.set()
        assert scheduler.stop(1) == ()
    finally:
        release.set()
        worker.join(1)


def test_msglog_scan_stop_joins_workers_after_runtime_rejects_admission(recwarn):
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="MsgLogIngestion-100")
    async_runtime = AsyncTelegramRuntime(Mock())
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=async_runtime), SimpleNamespace(enabled=True, config=SimpleNamespace(scan_ceiling=10)), Mock(), Mock(), Mock())
    scheduler._threads = {100: worker}
    scheduler._retiring_threads = {worker}
    async_runtime.begin_delivery_shutdown()
    worker.start()
    try:
        errors = scheduler.stop(0.01)
        assert len(errors) == 1
        assert isinstance(errors[0], MsgLogScanShutdownTimeout)
        release.set()
        assert scheduler.stop(1) == ()
        assert not worker.is_alive()
        assert not [warning for warning in recwarn if "was never awaited" in str(warning.message)]
    finally:
        release.set()
        worker.join(1)


def test_msglog_scan_stopped_before_coroutine_start_does_not_connect():
    queued, release = threading.Event(), threading.Event()

    class Runtime:
        def __init__(self):
            self.calls = 0

        def call(self, coroutine, timeout=None):
            self.calls += 1
            if self.calls == 1:
                queued.set()
                release.wait()
            asyncio.run(coroutine)

    class MTProto:
        enabled = True
        connected = False
        config = SimpleNamespace(scan_ceiling=10)

        def __init__(self):
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1

    mtproto = MTProto()
    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=Runtime()), mtproto, ingestion, Mock(), Mock())
    try:
        assert scheduler.schedule(100) == "started"
        assert queued.wait(1)
        assert len(scheduler.stop(0.01)) == 1
        release.set()
        assert scheduler.stop(1) == ()
        assert mtproto.connect_calls == 0
    finally:
        release.set()
        scheduler.stop(1)


def test_msglog_scan_stop_waits_for_pre_stop_connect_admission():
    connected, release = threading.Event(), threading.Event()

    class MTProto:
        enabled = True
        connected = False
        config = SimpleNamespace(scan_ceiling=10)

        def __init__(self):
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            connected.set()
            await asyncio.to_thread(release.wait)

    mtproto = MTProto()
    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())
    runtime = SharedAsyncRuntime()
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=runtime), mtproto, ingestion, Mock(), Mock())
    stop_result = []
    stop_finished = threading.Event()
    try:
        assert scheduler.schedule(100) == "started"
        assert connected.wait(1)
        stop_thread = threading.Thread(target=lambda: (stop_result.extend(scheduler.stop(1)), stop_finished.set()))
        stop_thread.start()
        assert scheduler._stop_event.wait(1)
        assert not stop_finished.wait(0.05)
        release.set()
        assert stop_finished.wait(1)
        stop_thread.join(1)
        assert not stop_thread.is_alive()
        assert stop_result == []
        assert mtproto.connect_calls == 1
    finally:
        release.set()
        scheduler.stop(1)
        runtime.close()


def test_msglog_scan_stop_times_out_while_connect_is_active():
    connected, release = threading.Event(), threading.Event()

    class MTProto:
        enabled = True
        connected = False
        config = SimpleNamespace(scan_ceiling=10)

        async def connect(self):
            connected.set()
            await asyncio.to_thread(release.wait)

    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())
    runtime = SharedAsyncRuntime()
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=runtime), MTProto(), ingestion, Mock(), Mock())
    try:
        assert scheduler.schedule(100) == "started"
        assert connected.wait(1)
        started = time.monotonic()
        errors = scheduler.stop(0.01)
        assert time.monotonic() - started < 0.2
        assert len(errors) == 1
        assert isinstance(errors[0], MsgLogScanShutdownTimeout)
    finally:
        release.set()
        assert scheduler.stop(1) == ()
        runtime.close()


def test_msglog_scan_waiting_for_admission_does_not_block_shared_runtime():
    connected, release, second_submitted = threading.Event(), threading.Event(), threading.Event()

    class MTProto:
        enabled = True
        connected = False
        config = SimpleNamespace(scan_ceiling=10)

        def __init__(self):
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            connected.set()
            await asyncio.to_thread(release.wait)

    runtime = SharedAsyncRuntime()
    mtproto = MTProto()
    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=0)), release_scan=Mock())
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=runtime), mtproto, ingestion, Mock(), Mock())
    try:
        assert scheduler.schedule(100) == "started"
        assert connected.wait(1)
        assert scheduler.schedule(200) == "started"
        assert runtime.second_submission.wait(1)

        async def confirm_loop_progress():
            second_submitted.set()

        runtime.call(confirm_loop_progress(), timeout=0.2)
        assert second_submitted.is_set()
        started = time.monotonic()
        errors = scheduler.stop(0.01)
        assert time.monotonic() - started < 0.2
        assert len(errors) == 1
        assert isinstance(errors[0], MsgLogScanShutdownTimeout)
        assert mtproto.connect_calls == 1
    finally:
        release.set()
        assert scheduler.stop(1) == ()
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


def test_ordinary_send_writes_msglog_once_and_releases_completion(monkeypatch):
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.msglogs = SimpleNamespace(add_or_update_message_log=Mock())
    processor.chat_manager = Mock()
    processor.router = Mock(resolve_reply=Mock(return_value=None))
    processor.commands = SimpleNamespace(register_command=Mock())
    processor._release_pending_slave_message = Mock()
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

    processor.dispatch_message(message, "", None, 123, None, dedupe_key=("slave", "slave-message"))

    processor.msglogs.add_or_update_message_log.assert_called_once_with(
        etm_msg,
        sent,
        None,
        sender_bot_id="7",
    )
    processor._release_pending_slave_message.assert_called_once_with(("slave", "slave-message"))
