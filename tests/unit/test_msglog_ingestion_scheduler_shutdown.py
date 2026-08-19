import asyncio
import gc
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master.history.msglog_ingestion import MsgLogIngestionService
from efb_telegram_master.history.msglog_scan import MsgLogScanScheduler


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
