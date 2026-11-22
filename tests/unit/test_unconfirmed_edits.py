"""An edit whose result is unknown is tried once more, not held; a hold keeps Telegram's error text."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from telegram import Chat, Message
from telegram.error import BadRequest, TimedOut

from efb_telegram_master.db_runtime import postgresql_connection
from efb_telegram_master.outbound import QueueRequest, create_postgresql_queue_tables
from efb_telegram_master.queued_log import encode_aggregation
from tests.unit.test_delivery_uncertainty import (
    accepted_then_lost_response,
    setup_manager,
)

CHAT = -1001


def edit_message_text(chat_id, message_id, text, **kwargs):
    return None


def send_message(chat_id, text, **kwargs):
    return None


OPERATIONS = {function.__name__: function for function in (edit_message_text, send_message)}


def edited(text="edited"):
    return Message(100, datetime.now(timezone.utc), Chat(CHAT, "supergroup"), text=text)


def _edit(queue, text, *, context=None):
    row_id, waiter = queue.enqueue_many([QueueRequest("edit_message_text", (), {
        "chat_id": CHAT, "message_id": 100, "text": text, "_slave_id": "slave",
        "_required_sender_bot_id": "__main__",
    }, context)], OPERATIONS.__getitem__)
    return row_id, waiter


def _run(manager, passes=4):
    scheduler = manager._outbound_scheduler
    for _ in range(passes):
        scheduler.dispatch_once()
        scheduler.harvest_completed()


def _row(queue, row_id):
    return queue.connection.execute(
        "SELECT delivery_hold, hold_reason, reconcile_attempts FROM outbound_queue WHERE id=?", (row_id,)
    ).fetchone()


def test_a_timed_out_edit_is_tried_once_more_then_dropped(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    row_id, waiter = _edit(queue, "new text")
    manager = setup_manager(queue)
    manager.TRANSPORT_RETRY_SECONDS = 0
    attempts = []
    manager._bot = SimpleNamespace(edit_message_text=accepted_then_lost_response(attempts))
    _run(manager)
    assert [kwargs["text"] for _args, kwargs in attempts] == ["new text", "new text"]
    assert _row(queue, row_id) is None
    assert isinstance(waiter.exception(timeout=1), TimedOut)
    assert not manager._outbound_scheduler.stopping
    queue.close()


def test_a_timed_out_edit_that_goes_through_on_its_second_try_completes(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    row_id, waiter = _edit(queue, "new text")
    manager = setup_manager(queue)
    manager.TRANSPORT_RETRY_SECONDS = 0
    attempts = []
    lost = accepted_then_lost_response(attempts)
    replies = iter([lost, lambda *args, **kwargs: attempts.append((args, kwargs)) or edited("new text")])
    manager._bot = SimpleNamespace(edit_message_text=lambda *args, **kwargs: next(replies)(*args, **kwargs))
    _run(manager)
    assert len(attempts) == 2
    assert waiter.result(timeout=1).text == "new text"
    assert _row(queue, row_id) is None
    queue.close()


def test_a_timed_out_edit_with_a_newer_edit_queued_is_dropped_without_a_retry(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    old_id, old_waiter = _edit(queue, "old text")
    new_id, new_waiter = _edit(queue, "newest text")
    manager = setup_manager(queue)
    manager.TRANSPORT_RETRY_SECONDS = 0
    attempts = []
    lost = accepted_then_lost_response(attempts)

    def edit(*args, **kwargs):
        if kwargs["text"] == "old text":
            return lost(*args, **kwargs)
        attempts.append((args, kwargs))
        return edited(kwargs["text"])

    manager._bot = SimpleNamespace(edit_message_text=edit)
    _run(manager)
    assert [kwargs["text"] for _args, kwargs in attempts] == ["old text", "newest text"]
    assert _row(queue, old_id) is None and _row(queue, new_id) is None
    assert isinstance(old_waiter.exception(timeout=1), TimedOut)
    assert new_waiter.result(timeout=1).text == "newest text"
    queue.close()


def test_a_later_aggregated_container_update_does_not_drop_the_older_edit(tmp_path, make_queue):
    """The container update is rendered from the confirmed container, without the older edit's text."""
    queue = make_queue(tmp_path)
    old_id, _ = _edit(queue, "old text")
    update = encode_aggregation({"format_version": 1, "kind": "member_update", "key": ("slave", CHAT, None),
                                 "container_id": f"{CHAT}.100", "updates": [], "members": []})
    _edit(queue, "", context=update)
    assert queue.unconfirmed_edit_superseded(old_id) is None
    queue.close()


def test_a_timed_out_send_is_still_held_with_telegrams_error_text(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    row_id, _ = queue.enqueue_many([QueueRequest("send_message", (CHAT, "hello"), {"_slave_id": "slave"})],
                                   OPERATIONS.__getitem__)
    manager = setup_manager(queue)
    manager.TRANSPORT_RETRY_SECONDS = 0
    attempts = []
    manager._bot = SimpleNamespace(send_message=accepted_then_lost_response(attempts, httpx.ReadTimeout))
    _run(manager)
    assert len(attempts) == 1
    assert _row(queue, row_id) == ("uncertain:TimedOut/ReadTimeout", "response lost", 0)
    queue.close()


def test_a_rejected_row_keeps_telegrams_description_bounded(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    row_id, _ = queue.enqueue_many([QueueRequest("send_message", (CHAT, "batch"), {"_slave_id": "slave"},
                                                 encode_aggregation({"format_version": 1, "kind": "boundary",
                                                                     "key": ("slave", CHAT, None)}))],
                                   OPERATIONS.__getitem__)
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(send_message=lambda *args, **kwargs: (_ for _ in ()).throw(
        BadRequest("Bad Request: wrong file identifier/HTTP URL specified" + " x" * 200)))
    _run(manager, passes=1)
    hold, reason, _attempts = _row(queue, row_id)
    assert hold == "aggregation_failed:BadRequest"
    # The error class already names Telegram's "Bad Request: " prefix; the description follows it.
    assert reason.startswith("Wrong file identifier/http url specified x") and len(reason) == 200
    queue.close()


def test_a_queue_written_before_the_reason_was_kept_gains_the_column(tmp_path, make_queue, queue_backend):
    queue = make_queue(tmp_path)
    row_id, _ = queue.enqueue_many([QueueRequest("send_message", (CHAT, "hello"), {"_slave_id": "slave"})],
                                   OPERATIONS.__getitem__)
    queue.close()
    # Take the table back to the earlier version's columns.
    if queue_backend is None:
        with closing(sqlite3.connect(tmp_path / "outbound-queue.sqlite3")) as db, db:
            db.execute("ALTER TABLE outbound_queue DROP COLUMN hold_reason")
    else:
        with closing(postgresql_connection(queue_backend)) as db, db, db.cursor() as cursor:
            cursor.execute("ALTER TABLE outbound_queue DROP COLUMN hold_reason")
            # What database startup runs before the bot opens its queue.
            create_postgresql_queue_tables(cursor.execute)
    queue = make_queue(tmp_path)
    queue.begin_delivery_attempt(row_id, SimpleNamespace(sender_bot_id=None))
    queue.hold_uncertain_delivery(row_id, TimedOut())
    assert _row(queue, row_id) == ("uncertain:TimedOut", "Timed out", 0)
    queue.close()
