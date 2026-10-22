"""The read-only queue report must run against a live-shaped queue without writing."""
import io
import sqlite3

from efb_telegram_master.outbound import OutboundQueue, QueueRequest
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


def test_report_prints_depth_age_and_sources_per_destination(tmp_path):
    queue = OutboundQueue(tmp_path)
    for index in range(3):
        queue.enqueue_many([_request(-1001, f"a{index}", thread=11, slave="slave group A")],
                           lambda _operation: send_message)
    queue.enqueue_many([_request(-2002, "b", slave="__history__:slave group B")],
                       lambda _operation: send_message)
    queue.close()

    with sqlite3.connect(tmp_path / TG_FILENAME) as db:
        db.executescript("""
            CREATE TABLE chatassoc (id INTEGER PRIMARY KEY, master_uid TEXT, slave_uid TEXT);
            CREATE TABLE slavechatinfo (id INTEGER PRIMARY KEY, slave_channel_id TEXT,
                slave_chat_uid TEXT, slave_chat_name TEXT, slave_chat_type TEXT);
            CREATE TABLE topicassoc (id INTEGER PRIMARY KEY, topic_chat_id TEXT,
                message_thread_id TEXT, slave_uid TEXT);
            CREATE TABLE historymigrationentry (id INTEGER PRIMARY KEY, slave_chat_id TEXT,
                target_chat_id TEXT, message_thread_id TEXT, source_master_msg_id TEXT,
                formatted_text TEXT, media_type TEXT, source_time TEXT, received_time TEXT,
                position INTEGER, created_at TEXT, generation TEXT);
            INSERT INTO chatassoc (master_uid, slave_uid) VALUES ('blueset.telegram -1001', 'slave group A');
            -- Every relink adds a chatassoc row: depth must not multiply by them.
            INSERT INTO chatassoc (master_uid, slave_uid) VALUES ('blueset.telegram -1001', 'slave group A');
            INSERT INTO chatassoc (master_uid, slave_uid) VALUES ('blueset.telegram -1001', 'slave group A');
            INSERT INTO slavechatinfo (slave_channel_id, slave_chat_uid, slave_chat_name, slave_chat_type)
                VALUES ('tests', 'slave group A', 'A group', 'group');
        """)

    out = io.StringIO()
    assert report(tmp_path / QUEUE_FILENAME, out=out) == 0
    printed = out.getvalue()
    assert "-1001 | 11 | 3" in printed          # depth per (chat, topic)
    assert "-1001 | 11 | 9" not in printed       # repeated chatassoc rows must not fan out
    assert "A group" in printed                  # bound slave chat name
    assert "implied_per_min" in printed          # lane drain rate vs the bot-chat limit
    assert "__history__:slave group B" not in printed
    assert "history | slave group B | 1" in printed  # backfill rows are separable
    # Read-only: the queue is untouched and can still be opened exclusively.
    reopened = OutboundQueue(tmp_path)
    reopened.close()


def test_data_dir_defaults_to_the_directory_holding_the_queue(tmp_path, monkeypatch):
    (tmp_path / QUEUE_FILENAME).write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    assert resolve_data_dir(None) == tmp_path


def test_report_measures_rows_that_still_carry_a_topic_in_their_request(tmp_path):
    def send_photo(photo, chat_id, message_thread_id=None):
        return photo, chat_id, message_thread_id

    queue = OutboundQueue(tmp_path)
    for _ in range(2):
        queue.enqueue_many([QueueRequest("send_photo", (b"image",), {
            "chat_id": -1001, "message_thread_id": 42,
        })], lambda _operation: send_photo)
    queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "main"})],
                       lambda _operation: send_message)
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET thread_id = NULL")
    queue.close()

    out = io.StringIO()
    assert report(tmp_path / QUEUE_FILENAME, out=out) == 0
    printed = out.getvalue()
    assert "rows with no topic: 3" in printed
    assert "recoverable from the request: 2" in printed   # the two photo sends name topic 42
    assert "genuinely the main chat: 1" in printed


def test_the_report_runs_as_a_plain_script_without_the_package(tmp_path, monkeypatch):
    """A host with EFB installed elsewhere can still run this one file."""
    import subprocess
    import sys
    from efb_telegram_master.queue_report import __file__ as module_path

    queue = OutboundQueue(tmp_path)
    queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "x"})],
                       lambda _operation: send_message)
    queue.close()
    result = subprocess.run(
        [sys.executable, module_path, "--queue", str(tmp_path / QUEUE_FILENAME)],
        capture_output=True, text=True, cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": ""},
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


def test_lane_listing_shows_why_a_row_is_not_sendable(tmp_path):
    queue = OutboundQueue(tmp_path)
    first, _ = queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "a"})],
                                  lambda _operation: send_message)
    second, _ = queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "b"})],
                                   lambda _operation: send_message)
    with queue.connection:
        queue.connection.execute(
            "UPDATE outbound_queue SET delivery_hold='uncertain:TimedOut/ReadTimeout' WHERE id = ?", (first,))
    queue.close()

    out = io.StringIO()
    assert report(tmp_path / QUEUE_FILENAME, out=out, lane_target=(-1001, 0)) == 0
    printed = out.getvalue()
    assert "uncertain:TimedOut/ReadTimeout" in printed
    assert f"{second} | 0 | send_message | - |" in printed      # the unheld row is sendable
    assert "sendable" in printed


def test_a_negative_chat_id_is_accepted_as_a_lane(tmp_path, capsys):
    from efb_telegram_master.queue_report import main

    queue = OutboundQueue(tmp_path)
    queue.enqueue_many([QueueRequest("send_message", (), {"chat_id": -1001, "text": "a"})],
                       lambda _operation: send_message)
    queue.close()
    assert main(["--data-dir", str(tmp_path), "--lane", "-1001:0"]) == 0
    printed = capsys.readouterr().out
    assert "lane -1001:0 in send order" in printed
