"""Thread-liveness reporting for test-session teardown."""

from __future__ import annotations

import sys
import threading
import traceback
from collections.abc import Iterable

_KNOWN_PREFIXES = (
    ("ETM master messages worker thread", "master-message worker"),
    ("ETM-membership", "membership probe executor"),
    ("ETM-send", "outbound executor"),
    ("ETM RPC server thread", "RPC server"),
    ("pytest-poll-bot-", "integration polling fixture"),
)


def live_non_daemon_threads(threads: Iterable[threading.Thread] | None = None) -> tuple[threading.Thread, ...]:
    """Return live non-daemon threads other than the caller."""
    current = threading.current_thread()
    candidates = threading.enumerate() if threads is None else threads
    return tuple(thread for thread in candidates if thread is not current and thread.is_alive() and not thread.daemon)


def _classify(thread: threading.Thread) -> str:
    for prefix, classification in _KNOWN_PREFIXES:
        if thread.name.startswith(prefix):
            return classification
    return "unclassified"


def format_live_thread_report(threads: Iterable[threading.Thread] | None = None) -> str:
    """Render live non-daemon thread identities and Python stacks."""
    live_threads = live_non_daemon_threads(threads)
    if not live_threads:
        return ""
    frames = sys._current_frames()
    lines = ["Live non-daemon threads remained after pytest fixture teardown:"]
    for thread in live_threads:
        lines.append(f"- name={thread.name!r} ident={thread.ident!r} class={_classify(thread)}")
        frame = frames.get(thread.ident)
        if frame is None:
            lines.append("  stack unavailable")
            continue
        lines.extend(f"  {line.rstrip()}" for line in traceback.format_stack(frame))
    return "\n".join(lines)


def fail_session_for_live_threads(session, threads: Iterable[threading.Thread] | None = None) -> str:
    """Report leaked threads and mark the session failed without terminating them."""
    report = format_live_thread_report(threads)
    if not report:
        return ""
    terminal_reporter = session.config.pluginmanager.getplugin("terminalreporter")
    if terminal_reporter is not None:
        terminal_reporter.write_line(report)
    else:
        print(report)
    if session.exitstatus == 0:
        session.exitstatus = 1
    return report
