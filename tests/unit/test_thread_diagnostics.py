import threading
from types import SimpleNamespace

from tests.thread_diagnostics import fail_session_for_live_threads, format_live_thread_report, live_non_daemon_threads


def test_live_non_daemon_threads_excludes_the_current_thread() -> None:
    assert threading.current_thread() not in live_non_daemon_threads([threading.current_thread()])


def test_thread_report_ignores_daemon_threads() -> None:
    started = threading.Event()
    release = threading.Event()

    def wait_for_release() -> None:
        started.set()
        release.wait()

    thread = threading.Thread(target=wait_for_release, daemon=True, name="coverage-helper")
    thread.start()
    assert started.wait(1)
    try:
        assert format_live_thread_report([thread]) == ""
    finally:
        release.set()
        thread.join(1)


def test_thread_report_marks_the_session_failed_and_includes_a_stack() -> None:
    started = threading.Event()
    release = threading.Event()

    def wait_for_release() -> None:
        started.set()
        release.wait()

    thread = threading.Thread(target=wait_for_release, name="ETM master messages worker thread")
    thread.start()
    assert started.wait(1)
    output: list[str] = []
    reporter = SimpleNamespace(write_line=output.append)
    session = SimpleNamespace(config=SimpleNamespace(pluginmanager=SimpleNamespace(getplugin=lambda name: reporter if name == "terminalreporter" else None)), exitstatus=0)

    try:
        report = fail_session_for_live_threads(session, [thread])
        assert "master-message worker" in report
        assert "wait_for_release" in report
        assert output == [report]
        assert session.exitstatus == 1
    finally:
        release.set()
        thread.join(1)

    assert not thread.is_alive()
    assert format_live_thread_report([thread]) == ""
