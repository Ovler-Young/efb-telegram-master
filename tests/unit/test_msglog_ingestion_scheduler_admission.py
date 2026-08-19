import threading
from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master.msglog_scan import MsgLogScanScheduler


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
