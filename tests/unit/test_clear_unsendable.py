"""Clearing held sends that can never be delivered, on the queue as the bot wrote it."""
import sqlite3
from contextlib import closing

import pytest
from telegram.error import BadRequest, TimedOut

from efb_telegram_master.clear_unsendable import clear
from efb_telegram_master.db_runtime import postgresql_connection
from efb_telegram_master.delivery_states import Event, RowState, next_state, ready_sql
from efb_telegram_master.outbound import (
    _POSTGRESQL_OWNER_LOCK,
    QueueRequest,
    SenderSelection,
)
from efb_telegram_master.queued_log import encode_aggregation
from tests.unit.test_outbound import ROW_SPELLINGS, _write_row

ATTEMPT = SenderSelection(sender=None, sender_bot_id="901")


def send_document(chat_id, document, **kwargs):
    return None


def send_message(chat_id, text, **kwargs):
    return None


def edit_message_text(chat_id, message_id, text, **kwargs):
    return None


OPERATIONS = {function.__name__: function for function in (send_document, send_message, edit_message_text)}


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


def _rows(directory, database, where="TRUE"):
    """Stored rows by id, read without opening the queue (which would update some)."""
    sql = f"SELECT * FROM outbound_queue AS q WHERE {where} ORDER BY id"
    if database is None:
        with closing(sqlite3.connect((directory / "outbound-queue.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(sql).fetchall()
    else:
        with closing(postgresql_connection(database)) as db, db.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    return {row[0]: tuple(bytes(value) if isinstance(value, memoryview) else value for value in row) for row in rows}


def test_clear_removes_only_rows_that_can_never_be_sent(tmp_path, make_queue, queue_backend):
    staged = tmp_path / "bot-api"
    staged.mkdir()
    gone, present = staged / "gone.mp4", staged / "present.mp4"
    gone.write_bytes(b"gif")
    present.write_bytes(b"gif")
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
    for row_id, error in ((gone_media, TimedOut()), (rejected, BadRequest("Message is too long")),
                          (edit, TimedOut()), (source, TimedOut()), (kept_media, TimedOut())):
        _hold(queue, row_id, error)
    _copy_payload(queue, source, rejected)  # A removed row and a kept row name one file.
    gone.unlink()
    queue.close()
    before = _rows(data, queue_backend)
    files = set(queue.media_dir.iterdir())

    expected = {"missing_media": 1, "rejected_batch": 1, "needs_removed_row": 1}
    report = clear(data, database=queue_backend)
    assert report["removed"] == expected and report["released"] == 4 and report["media_files_removed"] == 1
    assert not report["applied"]
    assert _rows(data, queue_backend) == before and set(queue.media_dir.iterdir()) == files

    report = clear(data, apply=True, database=queue_backend)
    assert report["removed"] == expected and report["kept"] == 6 and report["media_files_removed"] == 1
    # Only the removed row's own file went; the file a kept row still names stays.
    assert len(files - set(queue.media_dir.iterdir())) == 1 and present.exists()
    removed = {gone_media, rejected, update}
    assert _rows(data, queue_backend) == {row_id: row for row_id, row in before.items() if row_id not in removed}
    assert set(_rows(data, queue_backend, ready_sql("q"))) == {append, behind, kept_text}


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
