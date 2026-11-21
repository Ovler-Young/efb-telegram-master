"""The read-only queue report must run against a live-shaped queue without writing."""
import io
import pickle
import sqlite3

import pytest

from efb_telegram_master.db_runtime import postgresql_connection
from efb_telegram_master.outbound import QueueRequest
from efb_telegram_master.queue_report import (
    QUEUE_FILENAME, TG_FILENAME, queued_topic, report, resolve_data_dir,
)


def send_message(chat_id, text, message_thread_id=None):
    return chat_id, text


def _request(chat_id, text, *, thread=None, slave=None):
    kwargs = {"chat_id": chat_id, "text": text}
    if thread is not None:
        kwargs["message_thread_id"] = thread
    if slave is not None:
        kwargs["_slave_id"] = slave
    return QueueRequest("send_message", (), kwargs)


@pytest.fixture
def make_queue(make_queue, queue_backend, monkeypatch):
    """The report never takes a password on its command line: libpq reads this one."""
    if queue_backend is not None:
        monkeypatch.setenv("PGPASSWORD", queue_backend["password"])
    return make_queue


def _dsn(backend):
    """The libpq connection string for the test schema; the password is left to PGPASSWORD."""
    from psycopg2.extensions import make_dsn

    return make_dsn(dbname=backend["database"], host=backend["host"], port=backend["port"],
                    user=backend["user"], options=backend["options"])


def _report(directory, backend, **kwargs):
    """Run the report on the queue backend under test and return what it printed."""
    out = io.StringIO()
    if backend is None:
        assert report(directory / QUEUE_FILENAME, out=out, **kwargs) == 0
    else:
        assert report(directory / QUEUE_FILENAME, out=out, dsn=_dsn(backend), **kwargs) == 0
    return out.getvalue()


def _tg_tables(directory, backend, ddl, inserts):
    """The tgdata tables next to the queue: a SQLite file, or the same PostgreSQL schema."""
    if backend is None:
        with sqlite3.connect(directory / TG_FILENAME) as db:
            db.executescript(ddl + inserts)
        return
    connection = postgresql_connection(backend)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(ddl.replace("INTEGER PRIMARY KEY", "SERIAL PRIMARY KEY") + inserts)
    finally:
        connection.close()


def test_report_prints_depth_age_and_sources_per_destination(tmp_path, make_queue, queue_backend):
    queue = make_queue(tmp_path)
    for index in range(3):
        queue.enqueue_many([_request(-1001, f"a{index}", thread=11, slave="slave group A")],
                           lambda _operation: send_message)
    queue.enqueue_many([_request(-2002, "b", slave="__history__:slave group B")],
                       lambda _operation: send_message)
    queue.close()

    _tg_tables(tmp_path, queue_backend, """
        CREATE TABLE chatassoc (id INTEGER PRIMARY KEY, master_uid TEXT, slave_uid TEXT);
        CREATE TABLE slavechatinfo (id INTEGER PRIMARY KEY, slave_channel_id TEXT,
            slave_chat_uid TEXT, slave_chat_name TEXT, slave_chat_type TEXT,
            slave_channel_emoji TEXT);
        CREATE TABLE topicassoc (id INTEGER PRIMARY KEY, topic_chat_id TEXT,
            message_thread_id TEXT, slave_uid TEXT);
        CREATE TABLE historymigrationentry (id INTEGER PRIMARY KEY, slave_chat_id TEXT,
            target_chat_id TEXT, message_thread_id TEXT, source_master_msg_id TEXT,
            formatted_text TEXT, media_type TEXT, source_time TEXT, received_time TEXT,
            position INTEGER, created_at TEXT, generation TEXT);
    """, """
        INSERT INTO chatassoc (master_uid, slave_uid) VALUES ('blueset.telegram -1001', 'slave group A');
        -- Every relink adds a chatassoc row: depth must not multiply by them.
        INSERT INTO chatassoc (master_uid, slave_uid) VALUES ('blueset.telegram -1001', 'slave group A');
        INSERT INTO chatassoc (master_uid, slave_uid) VALUES ('blueset.telegram -1001', 'slave group A');
        INSERT INTO slavechatinfo (slave_channel_id, slave_channel_emoji, slave_chat_uid, slave_chat_name,
            slave_chat_type) VALUES ('tests', 'T', 'slave group A', 'A group', 'group');
        INSERT INTO topicassoc (topic_chat_id, message_thread_id, slave_uid) VALUES ('-1001', '11', 'slave group A');
        INSERT INTO topicassoc (topic_chat_id, message_thread_id, slave_uid) VALUES ('-1001', '9', 'slave group A');
    """)

    printed = _report(tmp_path, queue_backend)
    assert "skipped:" not in printed             # every section ran, on the tgdata tables too
    assert "-1001 | 9 | slave group A\n   -1001 | 11 | slave group A" in printed  # topics sort as numbers
    assert "-1001 | 11 | 3" in printed          # depth per (chat, topic)
    assert "-1001 | 11 | 9" not in printed       # repeated chatassoc rows must not fan out
    assert "A group" in printed                  # bound slave chat name
    assert "retry_in_s" in printed                # why a head is not dispatched yet
    assert "__history__:slave group B" not in printed
    assert "history | slave group B | 1" in printed  # backfill rows are separable
    # Read-only: the queue is untouched and can still be opened exclusively.
    reopened = make_queue(tmp_path)
    reopened.close()


def test_data_dir_defaults_to_the_directory_holding_the_queue(tmp_path, monkeypatch):
    (tmp_path / QUEUE_FILENAME).write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    assert resolve_data_dir(None) == tmp_path


def test_report_measures_rows_that_still_carry_a_topic_in_their_request(tmp_path, make_queue, queue_backend):
    def send_photo(photo, chat_id, message_thread_id=None):
        return photo, chat_id, message_thread_id

    queue = make_queue(tmp_path)
    for _ in range(2):
        queue.enqueue_many([QueueRequest("send_photo", (b"image",), {
            "chat_id": -1001, "message_thread_id": 42,
        })], lambda _operation: send_photo)
    queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "main"})],
                       lambda _operation: send_message)
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET thread_id = NULL")
    queue.close()

    printed = _report(tmp_path, queue_backend)
    assert "rows with no topic: 3" in printed
    assert "recoverable from the request: 2" in printed   # the two photo sends name topic 42
    assert "genuinely the main chat: 1" in printed


def test_the_report_runs_as_a_plain_script_without_the_package(tmp_path, make_queue, queue_backend):
    """A host with EFB installed elsewhere can still run this one file."""
    import os
    import subprocess
    import sys
    from efb_telegram_master.queue_report import __file__ as module_path

    queue = make_queue(tmp_path)
    queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "x"})],
                       lambda _operation: send_message)
    queue.close()
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": ""}
    if queue_backend is None:
        target = ["--queue", str(tmp_path / QUEUE_FILENAME)]
    else:
        target = ["--dsn", _dsn(queue_backend)]
        environment["PGPASSWORD"] = os.environ["PGPASSWORD"]
    result = subprocess.run(
        [sys.executable, module_path, *target], capture_output=True, text=True, cwd=tmp_path,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "per destination: depth, holds, age" in result.stdout
    assert "-1001 | 0 | 1" in result.stdout


def test_queued_topic_reads_the_payload_and_the_aggregation_context():
    from efb_telegram_master.outbound import OutboundQueue
    from efb_telegram_master.queued_log import encode_aggregation

    payload = OutboundQueue.encode_payload((), {"chat_id": -1001, "message_thread_id": 42})
    assert queued_topic(payload, None) == 42
    api_payload = OutboundQueue.encode_payload((), {"chat_id": -1001, "api_kwargs": {"message_thread_id": "7"}})
    assert queued_topic(api_payload, None) == 7
    context = encode_aggregation({"key": ("slave", -1001, "13"), "kind": "logical"})
    assert queued_topic(b"", context) == 13
    assert queued_topic(b"", None) is None


def test_lane_listing_shows_why_a_row_is_not_sendable(tmp_path, make_queue, queue_backend):
    queue = make_queue(tmp_path)
    first, _ = queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "a"})],
                                  lambda _operation: send_message)
    second, _ = queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "b"})],
                                   lambda _operation: send_message)
    with queue.connection:
        queue.connection.execute(
            "UPDATE outbound_queue SET delivery_hold='uncertain:TimedOut/ReadTimeout' WHERE id = ?", (first,))
    queue.close()

    printed = _report(tmp_path, queue_backend, lane_target=(-1001, 0))
    assert "uncertain:TimedOut/ReadTimeout" in printed
    assert f"{second} | 0 | send_message | - |" in printed      # the unheld row is sendable
    assert "sendable" in printed


def test_a_negative_chat_id_is_accepted_as_a_lane(tmp_path, capsys, make_queue, queue_backend):
    from efb_telegram_master.queue_report import main

    queue = make_queue(tmp_path)
    queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "a"})],
                       lambda _operation: send_message)
    queue.close()
    target = ["--data-dir", str(tmp_path)] if queue_backend is None else ["--dsn", _dsn(queue_backend)]
    assert main([*target, "--lane", "-1001:0"]) == 0
    printed = capsys.readouterr().out
    assert "lane -1001:0 in send order" in printed


def test_row_detail_shows_a_durable_receipt_and_whether_msglog_has_it(tmp_path, make_queue, queue_backend):
    from efb_telegram_master.outbound import QueuedCall
    from efb_telegram_master.queued_log import encode_aggregation
    from types import SimpleNamespace

    queue = make_queue(tmp_path)
    context = encode_aggregation(dict(
        format_version=1, kind="aggregate", key=("slave", -1001, None), container_id=None,
        generation=1, aggregate={"children": [{"a": 1}, {"b": 2}], "revision": 3}, members=[],
    ))
    row_id, _ = queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "x"},
                                                 log_context=context)],
                                   lambda _operation: send_message)
    receipt = b"\x01" + pickle.dumps((
        SimpleNamespace(message_id=77, chat=SimpleNamespace(id=-1001), date=None), "aux-1",
    ), protocol=5)
    with queue.connection:
        queue.connection.execute(
            "UPDATE outbound_queue SET delivery_state='sent_pending', completion_receipt=?, "
            "reconcile_attempts=7, reconcile_after=0 WHERE id=?", (receipt, row_id))
    queue.close()

    _tg_tables(tmp_path, queue_backend, """
        CREATE TABLE msglog (id INTEGER PRIMARY KEY, master_msg_id TEXT, slave_message_id TEXT,
            sender_bot_id TEXT);
    """, """
        INSERT INTO msglog (master_msg_id, slave_message_id, sender_bot_id)
            VALUES ('-1001.77', 'source.7', 'aux-1');
    """)
    printed = _report(tmp_path, queue_backend, row_id=row_id)
    assert "delivery_state = sent_pending" in printed
    assert "reconcile_attempts = 7" in printed
    assert "aggregate_children=2" in printed
    assert "message_id=77" in printed and "chat_id=-1001" in printed
    assert "-1001.77" in printed and "source.7" in printed   # MsgLog already has it
