from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram import Update

from efb_telegram_master.channel_commands import TelegramCommandService
from efb_telegram_master.channel_locale import LocaleState
from efb_telegram_master.msglog_scan import MsgLogScanScheduler


class FakeMessageIdentifier:
    message_id = 1


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, **_kwargs: object) -> FakeMessageIdentifier:
        self.calls.append((chat_id, text))
        return FakeMessageIdentifier()


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
