import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from efb_telegram_master.history.history_replay import HistoryReplayShutdownTimeout, HistoryReplayWorker


def test_history_replay_resume_starts_a_worker_for_queued_entries():
    history_migrations = Mock(has_pending_entries=Mock(return_value=True))
    manager = HistoryReplayWorker(Mock(), Mock(), history_migrations, Mock(), Mock())

    with patch("efb_telegram_master.history.history_replay.threading.Thread") as thread:
        assert manager.resume() is True

    thread.return_value.start.assert_called_once()


def test_history_replay_stop_rejects_starts_and_retries_after_a_blocked_call():
    started, release = threading.Event(), threading.Event()
    bot = SimpleNamespace(send_message=Mock(side_effect=lambda **_kwargs: (started.set(), release.wait())))
    worker = HistoryReplayWorker(bot, Mock(), Mock(), Mock(), Mock())
    worker.queue_entries = Mock(return_value=0)
    try:
        assert worker.start("tests.slave chat", 100, source_storage_key=(1, 2))
        assert started.wait(1)
        errors = worker.stop(0.01)
        assert len(errors) == 1
        assert isinstance(errors[0], HistoryReplayShutdownTimeout)
        assert worker.start("tests.slave chat", 100) is False
        release.set()
        assert worker.stop(1) == ()
        assert not any(thread.name == "HistoryMigrationReplay" and thread.is_alive() for thread in threading.enumerate())
    finally:
        release.set()
        worker.stop(1)


def test_history_replay_one_loop_drains_multiple_targets():
    first_started, release, second_done = threading.Event(), threading.Event(), threading.Event()
    worker = HistoryReplayWorker(Mock(), Mock(), SimpleNamespace(get_next_target=lambda: None), Mock(), Mock())
    queued: list[tuple[str, int]] = []

    def queue_entries(slave_chat_id, target_chat_id, _thread_id):
        queued.append((str(slave_chat_id), target_chat_id))
        if len(queued) == 1:
            first_started.set()
            release.wait(1)
        else:
            second_done.set()
        return 0

    worker.queue_entries = Mock(side_effect=queue_entries)
    try:
        assert worker.start("tests.slave first", 100)
        assert first_started.wait(1)
        assert worker.start("tests.slave second", 200)
        release.set()
        assert second_done.wait(1)
        assert worker.stop(1) == ()
        assert queued == [("tests.slave first", 100), ("tests.slave second", 200)]
    finally:
        release.set()
        worker.stop(1)


def test_history_replay_processes_request_enqueued_after_idle_queue_observation():
    observed_empty, second_done = threading.Event(), threading.Event()
    worker = HistoryReplayWorker(Mock(), Mock(), SimpleNamespace(get_next_target=lambda: None), Mock(), Mock())
    queued: list[int] = []
    original_wait = worker._condition.wait

    def observe_then_wait(*args, **kwargs):
        observed_empty.set()
        return original_wait(*args, **kwargs)

    worker._condition.wait = observe_then_wait
    worker.queue_entries = Mock(side_effect=lambda _slave, target, _thread: (queued.append(target), second_done.set() if target == 200 else None, 0)[2])
    try:
        assert worker.start("tests.slave first", 100)
        assert observed_empty.wait(1)
        assert worker.start("tests.slave second", 200)
        assert second_done.wait(1)
        assert queued == [100, 200]
    finally:
        worker.stop(1)
