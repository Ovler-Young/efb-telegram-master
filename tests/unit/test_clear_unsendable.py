"""Clearing held sends that can never be delivered, on the queue as the bot wrote it."""
import pickle
import sqlite3
from contextlib import closing

import pytest
from telegram.error import BadRequest, TimedOut

from efb_telegram_master.clear_unsendable import _header_only, clear
from efb_telegram_master.db_runtime import postgresql_connection
from efb_telegram_master.delivery_states import Event, RowState, next_state, ready_sql
from efb_telegram_master.outbound import (
    _POSTGRESQL_OWNER_LOCK,
    QueueRequest,
    SenderSelection,
)
from efb_telegram_master.queued_log import encode, encode_aggregation
from tests.unit.test_outbound import ROW_SPELLINGS, _write_row
from tests.unit.test_restart_memory import message_with_group

ATTEMPT = SenderSelection(sender=None, sender_bot_id="901")


def send_document(chat_id, document, **kwargs):
    return None


def send_message(chat_id, text, **kwargs):
    return None


def send_video(chat_id, video, **kwargs):
    return None


def edit_message_text(chat_id, message_id, text, **kwargs):
    return None


OPERATIONS = {function.__name__: function for function in (send_document, send_message, send_video, edit_message_text)}


def _context(kind, **fields):
    return encode_aggregation(dict(format_version=1, kind=kind, key=("slave", -1001, None), **fields))


def _add(queue, operation, kwargs, *, context=None, predecessor=None, cleanup=()):
    row_id, _ = queue.enqueue_many(
        [QueueRequest(operation, (), kwargs, context, cleanup_files=cleanup, predecessor_id=predecessor)],
        OPERATIONS.__getitem__,
    )
    return row_id


def _hold(queue, row_id, error):
    queue.begin_delivery_attempt(row_id, ATTEMPT)
    if isinstance(error, BadRequest):
        assert queue.hold_failed_aggregation(row_id, error)
    else:
        queue.hold_uncertain_delivery(row_id, error)


def _copy_payload(queue, source, target):
    with queue.connection:
        queue.connection.execute(
            "UPDATE outbound_queue SET payload=(SELECT payload FROM outbound_queue WHERE id=?) WHERE id=?",
            (source, target))


def _rows(directory, database, where="TRUE", columns="*"):
    """Stored rows by id, read without opening the queue (which would update some)."""
    sql = f"SELECT id, {columns} FROM outbound_queue AS q WHERE {where} ORDER BY id"
    if database is None:
        with closing(sqlite3.connect((directory / "outbound-queue.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(sql).fetchall()
    else:
        with closing(postgresql_connection(database)) as db, db.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    return {row[0]: tuple(bytes(value) if isinstance(value, memoryview) else value for value in row[1:])
            for row in rows}


def test_clear_removes_only_rows_that_can_never_be_sent(tmp_path, make_queue, queue_backend):
    staged = tmp_path / "bot-api"
    staged.mkdir()
    gone, present, captioned_file, unlisted_file, untexted_file, header_file, resend_file = (
        staged / name for name in (
            "gone.mp4", "present.mp4", "captioned.mp4", "unlisted.mp4", "untexted.mp4", "header.mp4", "resend.mp4"))
    for path in (gone, present, captioned_file, unlisted_file, untexted_file, header_file, resend_file):
        path.write_bytes(b"gif")
    data = tmp_path / "data"
    queue = make_queue(data)
    member = {"origin_uid": "origin", "source_id": "1"}

    gone_media = _add(queue, "send_document",
                      {"chat_id": -1001, "document": gone.as_uri(), "thumbnail": b"thumbnail"}, cleanup=(str(gone),))
    rejected = _add(queue, "send_message", {"chat_id": -1001, "text": "batch"},
                    context=_context("aggregate", members=[member]))
    # An edit whose container the removed batch would have created cannot be sent without it.
    update = _add(queue, "edit_message_text",
                  {"chat_id": -1001, "message_id": 5, "text": "batch", "_required_sender_bot_id": "901"},
                  context=_context("member_update", updates=[member]), predecessor=rejected)
    # Rows the bot still sends without the removed rows: an append into a new container, an edit of
    # a container already on Telegram and a second output of a member.
    append = _add(queue, "send_message", {"chat_id": -1001, "text": "next"},
                  context=_context("logical", members=[member], inherit=True), predecessor=update)
    edit = _add(queue, "edit_message_text",
                {"chat_id": -1001, "message_id": 6, "text": "edited", "_required_sender_bot_id": "901"},
                context=_context("member_update", updates=[member], container_id="slave.6"), predecessor=rejected)
    source = _add(queue, "send_document", {"chat_id": -1001, "document": b"shared file"},
                  context=_context("source", member=member), predecessor=rejected)
    behind = _add(queue, "send_message", {"chat_id": -1002, "text": "ordered after"}, predecessor=gone_media)
    kept_media = _add(queue, "send_document", {"chat_id": -1001, "document": present.as_uri()},
                      cleanup=(str(present),))
    kept_text = _add(queue, "send_message", {"chat_id": -1001, "text": "text"})
    # A gone file's caption is sent as text only when the row is listed as not delivered.
    caption = {"chat_id": -1004, "caption": "<b>Author:</b>\nmessage", "parse_mode": "HTML", "message_thread_id": 7}
    log = encode(message_with_group(), None)
    captioned = _add(queue, "send_video", {**caption, "video": captioned_file.as_uri(), "thumbnail": b"thumb",
                                           "duration": 3, "has_spoiler": True}, cleanup=(str(captioned_file),),
                     context=_context("boundary", legacy_context=log))
    unlisted = _add(queue, "send_video", {**caption, "video": unlisted_file.as_uri()}, cleanup=(str(unlisted_file),),
                    context=log)
    # A message without text has the sender's header as caption, with or without the bot's placeholder line:
    # neither is sent.
    untexted = message_with_group()
    untexted.text = ""
    placeholder = _add(queue, "send_video", {"chat_id": -1004, "video": untexted_file.as_uri(),
                                             "caption": "<b>Author:</b>\n🖼️", "parse_mode": "HTML"},
                       cleanup=(str(untexted_file),), context=encode(untexted, None))
    header = _add(queue, "send_video", {"chat_id": -1004, "video": header_file.as_uri(),
                                        "caption": "<b>Author &amp; co:</b>\n", "parse_mode": "HTML"},
                  cleanup=(str(header_file),))
    after_caption = _add(queue, "send_message", {"chat_id": -1004, "text": "after"}, predecessor=captioned)
    # A listed send that timed out and still has its file is sent again; an unlisted one (kept_media) stays held.
    resend = _add(queue, "send_document", {"chat_id": -1005, "document": resend_file.as_uri(), "caption": "again"},
                  cleanup=(str(resend_file),))
    after_resend = _add(queue, "send_message", {"chat_id": -1005, "text": "after"}, predecessor=resend)
    for row_id, error in ((gone_media, TimedOut()), (rejected, BadRequest("Message is too long")),
                          (edit, TimedOut()), (source, TimedOut()), (kept_media, TimedOut()),
                          (captioned, TimedOut()), (unlisted, TimedOut()), (placeholder, TimedOut()),
                          (header, TimedOut()), (resend, TimedOut())):
        _hold(queue, row_id, error)
    _copy_payload(queue, source, rejected)  # A removed row and a kept row name one file.
    for path in (gone, captioned_file, unlisted_file, untexted_file, header_file):
        path.unlink()
    queue.close()
    before = _rows(data, queue_backend)
    unchanged = "priority, telegram_chat_id, slave_id, created_at, log_context, predecessor_id, thread_id"
    before_unchanged = _rows(data, queue_backend, columns=unchanged)[captioned]
    stored = unchanged + ", operation, payload, required_sender_bot_id"
    before_resend = _rows(data, queue_backend, columns=stored)[resend]
    # While their predecessors are held, nothing defers the rows behind them.
    assert {after_caption, after_resend} <= set(_rows(data, queue_backend, ready_sql("q")))
    files = set(queue.media_dir.iterdir())

    # Without a list of rows not delivered, no caption is sent: every gone-file row is removed.
    report = clear(data, database=queue_backend)
    assert report["removed"] == {"missing_media": 5, "rejected_batch": 1, "needs_removed_row": 1}
    assert report["caption_sent_as_text"] == 0 and report["caption_not_sent"] == 4 and report["released"] == 5
    assert report["caption_header_only"] == 0 and report["resend"] == 0
    assert report["media_files_removed"] == 2 and not report["applied"]
    assert _rows(data, queue_backend) == before and set(queue.media_dir.iterdir()) == files

    # A listed caption is sent only when it has the sender's text; a listed rejected batch is still removed;
    # a listed timed-out member of a text batch stays held.
    listed = tmp_path / "not-delivered.txt"
    listed.write_text(f"{captioned}\n{placeholder}\n{header}\n{resend}\n{rejected}\n{source}\n")
    report = clear(data, apply=True, database=queue_backend, not_delivered=listed)
    assert report["removed"] == {"missing_media": 4, "rejected_batch": 1, "needs_removed_row": 1}
    assert report["caption_sent_as_text"] == 1 and report["caption_not_sent"] == 1
    assert report["caption_header_only"] == 2 and report["resend"] == 1 and report["resend_batch_member"] == 1
    assert report["kept"] == 10 and report["released"] == 4 and report["media_files_removed"] == 2
    # Only the files of the removed row and of the converted row went; the file a kept row still names stays.
    assert len(files - set(queue.media_dir.iterdir())) == 2 and present.exists()
    removed, converted = {gone_media, rejected, update, unlisted, placeholder, header}, {captioned}
    rows = _rows(data, queue_backend)
    assert all(rows.pop(row_id) != before[row_id] for row_id in converted | {resend})
    assert rows == {row_id: row for row_id, row in before.items() if row_id not in removed | converted | {resend}}
    # The row sent again keeps its request and is unheld; rows behind it and behind a caption wait for them.
    assert _rows(data, queue_backend, columns=stored)[resend] == before_resend
    ready = set(_rows(data, queue_backend, ready_sql("q")))
    assert ready == {append, behind, kept_text, resend, *converted}
    # The same chat, topic, text and formatting, without the video, from the bot that attempted it, in its place.
    assert _rows(data, queue_backend, columns=unchanged)[captioned] == before_unchanged
    operation, payload, sender = _rows(data, queue_backend, columns="operation, payload, required_sender_bot_id")[
        captioned]
    assert operation == "send_message" and sender == "901"
    text = {key: value for key, value in caption.items() if key != "caption"}
    assert pickle.loads(payload[1:]) == ((), {**text, "text": caption["caption"]})


@pytest.mark.parametrize(("caption", "parse_mode", "header_only"), [
    ("<b>Author:</b>\n", "HTML", True),
    ("<b>Author:</b>\n🖼️", "HTML", True),
    ("Author:\n", None, True),
    # A header longer than one short line is still the header.
    (f"{'Long name ' * 12}:\n🖼️", None, True),
    ("<b>Author:</b>\nmessage", "HTML", False),
    # A chat that shows no header: the caption is the sender's own text, even if it ends in ':'.
    ("Here is the receipt:", "HTML", False),
    ("Here is the receipt:", None, False),
    ("<b>Author</b>\n🖼️", "HTML", False),
])
def test_a_caption_is_header_only_when_it_has_the_bots_header_and_no_text_from_the_sender(
        caption, parse_mode, header_only):
    assert _header_only(caption, parse_mode) is header_only


@pytest.mark.parametrize("spelling", sorted(set(ROW_SPELLINGS) - {"absent"}))
def test_a_row_with_missing_media_is_cleared_only_from_a_state_that_accepts_discard(
        tmp_path, spelling, make_queue, queue_backend):
    staged = tmp_path / "gone.mp4"
    staged.write_bytes(b"gif")
    data = tmp_path / "data"
    queue = make_queue(data)
    row_id = _write_row(queue, data, spelling)
    media = _add(queue, "send_document", {"chat_id": -1003, "document": staged.as_uri()}, cleanup=(str(staged),))
    _copy_payload(queue, media, row_id)
    queue.delete(media)  # Also deletes the staged file the row now names.
    queue.close()
    before = _rows(data, queue_backend)

    clear(data, apply=True, database=queue_backend)
    if next_state(ROW_SPELLINGS[spelling][2], Event.DISCARD) is RowState.ABSENT:
        before.pop(row_id)
    assert _rows(data, queue_backend) == before


def test_clear_refuses_while_the_bot_owns_the_queue(tmp_path, make_queue, queue_backend):
    queue = make_queue(tmp_path)
    row_id = _add(queue, "send_message", {"chat_id": -1001, "text": "row"})
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET delivery_hold='invalid_payload' WHERE id=?", (row_id,))
    with pytest.raises(RuntimeError, match="in use"):
        clear(tmp_path, apply=True, database=queue_backend)
    queue.close()
    if queue_backend is not None:
        # A bot on another host owns a PostgreSQL queue through its session lock only.
        owner = postgresql_connection(queue_backend)
        try:
            with owner.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s::bigint + oid::bigint) FROM pg_namespace "
                               "WHERE nspname = current_schema()", (_POSTGRESQL_OWNER_LOCK,))
            with pytest.raises(RuntimeError, match="in use"):
                clear(tmp_path, apply=True, database=queue_backend)
        finally:
            owner.close()
    assert clear(tmp_path, apply=True, database=queue_backend)["removed"] == {"invalid_payload": 1}
