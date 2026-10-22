"""The read-only queue report must run against a live-shaped queue without writing."""
import io
import sqlite3

from efb_telegram_master.outbound import OutboundQueue, QueueRequest
from efb_telegram_master.queue_report import QUEUE_FILENAME, TG_FILENAME, report, resolve_data_dir


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
    assert report(tmp_path, out) == 0
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
