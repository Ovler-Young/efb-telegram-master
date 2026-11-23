"""Applying what the destination chat shows about a held send, without ever sending it twice."""
import asyncio
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram.error import TimedOut
from telethon.tl import types as tl

from efb_telegram_master import delivery_check
from efb_telegram_master.bot_manager import (
    QueuedDbLogContext,
    TelegramBotManager,
    _chat_receipt,
)
from efb_telegram_master.delivery_check import HeldSendCheck, Seen, Verdict
from efb_telegram_master.delivery_states import RowState, classify
from efb_telegram_master.msg_type import TGMsgType, get_msg_type
from efb_telegram_master.outbound import OutboundQueueScheduler
from tests.unit.test_clear_unsendable import (
    OPERATIONS,
    _add,
    _context,
    _copy_payload,
    _hold,
)
from tests.unit.test_database_safety import manager_factory as database_manager_fixture
from tests.unit.test_delivery_check import RecordingClient
from tests.unit.test_outbound_queue_runtime_evidence import manager_adapter
from tests.unit.test_restart_memory import message_with_group

manager_factory = database_manager_fixture
CHAT = -1000000001234   # the Bot API id of channel 1234
BOT = 901          # the auxiliary bot that made the attempts (tests.unit.test_clear_unsendable.ATTEMPT)


def now():
    return datetime.now(timezone.utc)  # noqa: UP017 - datetime.UTC needs Python 3.11


class NoSends:
    """A bot that fails the test on any call: resolving a held send never talks to the Bot API."""

    def __getattr__(self, name):
        raise AssertionError(f"Bot API call {name} while resolving a held send")


class ImmediateExecutor:
    def submit(self, fn, *args):
        raise AssertionError("nothing is submitted while resolving a held send")


def manager_for(queue, db=None):
    manager = manager_adapter()
    manager._bot = NoSends()
    manager._outbound_queue = queue
    manager._queue_operation = OPERATIONS.__getitem__
    manager.me = SimpleNamespace(id=900)
    manager.channel = SimpleNamespace(db=db)
    manager._outbound_scheduler = OutboundQueueScheduler(queue, manager, ImmediateExecutor(), 1)
    return manager


def chat_message(message_id, text, *, document=None, edited=None):
    media = None
    if document is not None:
        media = tl.MessageMediaDocument(document=tl.Document(
            id=1, access_hash=1, file_reference=b"", date=now(), mime_type="application/zip",
            size=13, dc_id=1, attributes=[tl.DocumentAttributeFilename(document)]))
    return Seen.from_message(tl.Message(
        id=message_id, peer_id=tl.PeerChannel(1234), date=now(), message=text,
        from_id=tl.PeerUser(BOT), media=media, edit_date=edited))


def held(queue, operation, kwargs, **extra):
    row_id = _add(queue, operation, kwargs, **extra)
    _hold(queue, row_id, TimedOut("response lost"))
    return row_id


def age(queue, row_id, seconds):
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET attempt_started_at=? WHERE id=?",
                                 (time.time() - seconds, row_id))


def hold_of(queue, row_id):
    return queue.connection.execute("SELECT delivery_hold FROM outbound_queue WHERE id=?", (row_id,)).fetchone()[0]


def rows(queue):
    return {row[0]: tuple(bytes(v) if isinstance(v, memoryview) else v for v in row)
            for row in queue.connection.execute("SELECT * FROM outbound_queue ORDER BY id").fetchall()}


def test_found_send_is_recorded_as_delivered_and_never_sent(tmp_path, manager_factory, make_queue):
    db = manager_factory(tmp_path)
    queue = make_queue(tmp_path / "data")
    manager = manager_for(queue, db)
    context = manager._encode_queued_log_context(QueuedDbLogContext(message_with_group(), None))
    row_id = held(queue, "send_document", {"chat_id": CHAT, "document": b"zip", "caption": "c"}, context=context)

    assert manager._resolve_held_send(row_id, Verdict.FOUND, chat_message(105, "c", document="a.zip"), BOT)

    logged = db.get_msg_log(master_msg_id=f"{CHAT}.105")
    assert logged is not None and logged.slave_message_id == "source-1"
    assert logged.sender_bot_id == str(BOT) and logged.media_type == "Document" and not logged.file_id
    assert rows(queue) == {}


def test_found_edit_is_confirmed_with_the_message_it_changed(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = held(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 50, "_required_sender_bot_id": "901", "text": "new"})

    assert manager._resolve_held_send(row_id, Verdict.FOUND, chat_message(50, "new", edited=now()), BOT)
    assert rows(queue) == {}


def test_found_message_from_another_bot_leaves_the_row_held(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = held(queue, "send_message", {"chat_id": CHAT, "text": "hi"})
    before = rows(queue)

    assert not manager._resolve_held_send(row_id, Verdict.FOUND, chat_message(105, "hi"), 902)
    assert rows(queue) == before


def test_recent_send_not_in_the_chat_is_sent_again_unchanged(tmp_path, make_queue):
    media = tmp_path / "staged.mp4"
    media.write_bytes(b"video")
    queue = make_queue(tmp_path / "data")
    manager = manager_for(queue)
    row_id = held(queue, "send_document", {"chat_id": CHAT, "document": media.as_uri()}, cleanup=(str(media),))
    before = rows(queue)

    assert manager._resolve_held_send(row_id, Verdict.NOT_FOUND, None, BOT)

    after = rows(queue)
    names = [d[0] for d in queue.connection.execute("SELECT * FROM outbound_queue LIMIT 0").description]
    changed = {name for name, old, new in zip(names, before[row_id], after[row_id]) if old != new}
    assert changed == {"delivery_hold", "hold_reason", "unconfirmed_at"}
    assert after[row_id][names.index("hold_reason")] is None      # the error text of the hold goes with it
    assert classify(after[row_id][names.index("delivery_state")], None) is RowState.WAITING
    assert media.exists()


def test_send_sent_again_by_the_check_and_missing_again_stays_held(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = held(queue, "send_message", {"chat_id": CHAT, "text": "hi"})
    assert manager._resolve_held_send(row_id, Verdict.NOT_FOUND, None, BOT)
    _hold(queue, row_id, TimedOut("response lost"))          # the second try timed out too
    before = rows(queue)

    assert not manager._resolve_held_send(row_id, Verdict.NOT_FOUND, None, BOT)
    assert rows(queue) == before and hold_of(queue, row_id) == "uncertain:TimedOut"


@pytest.mark.parametrize("why", ["old", "media_gone"])
def test_send_not_in_the_chat_is_discarded_with_the_edit_that_needs_it(tmp_path, make_queue, why):
    staged = tmp_path / "bot-api"
    staged.mkdir()
    media = staged / "clip.mp4"
    media.write_bytes(b"video")
    queue = make_queue(tmp_path / "data")
    manager = manager_for(queue)
    member = {"origin_uid": "origin", "source_id": "1"}
    row_id = held(queue, "send_document", {"chat_id": CHAT, "document": media.as_uri(), "thumbnail": b"thumb"},
                  cleanup=(str(media),), context=_context("boundary", members=[member]))
    container_edit = _add(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 1, "_required_sender_bot_id": "901", "text": "x"},
                          context=_context("member_update", member=member), predecessor=row_id)
    sharing = _add(queue, "send_document", {"chat_id": CHAT, "document": b"other"})
    _copy_payload(queue, row_id, sharing)          # names the same thumbnail file
    behind = _add(queue, "send_message", {"chat_id": CHAT, "text": "later"}, predecessor=row_id)
    if why == "old":
        age(queue, row_id, 86400 + 60)
    else:
        media.unlink()
    kept = {k: v for k, v in rows(queue).items() if k not in (row_id, container_edit)}
    files = sorted(path.name for path in queue.media_dir.iterdir())

    assert manager._resolve_held_send(row_id, Verdict.NOT_FOUND, None, BOT)

    assert rows(queue) == kept and set(kept) == {sharing, behind}
    assert sorted(path.name for path in queue.media_dir.iterdir()) == files   # the shared thumbnail stays
    assert media.exists() == (why == "old")                                   # external files are never deleted


def test_unshared_queue_file_of_a_discarded_send_is_deleted(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = held(queue, "send_document", {"chat_id": CHAT, "document": b"zip"})
    age(queue, row_id, 86400 + 60)
    assert list(queue.media_dir.iterdir())

    assert manager._resolve_held_send(row_id, Verdict.NOT_FOUND, None, BOT)
    assert rows(queue) == {} and not list(queue.media_dir.iterdir())


@pytest.mark.parametrize("newer", ["queued", "sent"])
def test_edit_not_applied_is_dropped_when_a_newer_edit_exists(tmp_path, make_queue, newer):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = held(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 50, "_required_sender_bot_id": "901", "text": "old"})
    target = chat_message(50, "first")
    if newer == "queued":
        _add(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 50, "_required_sender_bot_id": "901", "text": "newer"})
    else:
        target = chat_message(50, "newer", edited=now())
    others = {k: v for k, v in rows(queue).items() if k != row_id}

    assert manager._resolve_held_send(row_id, Verdict.NOT_FOUND, target, BOT)
    assert rows(queue) == others


MEMBER = {"origin_uid": "origin", "source_id": "1"}


def appending_edit(queue):
    """A held edit that appends a new message to a combined message: that message's only copy."""
    return held(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 50, "_required_sender_bot_id": "901",
                                             "text": "one\ntwo"}, context=_context("aggregate", members=[MEMBER]))


def test_edit_appending_to_a_combined_message_stays_held_when_a_newer_edit_is_queued(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = appending_edit(queue)
    _add(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 50, "_required_sender_bot_id": "901", "text": "newer"})
    before = rows(queue)

    assert not manager._resolve_held_send(row_id, Verdict.NOT_FOUND, chat_message(50, "one"), BOT)
    assert rows(queue) == before


@pytest.mark.parametrize("later", ["combined_update_queued", "combined_message_edited"])
def test_later_update_of_a_combined_message_is_not_a_newer_edit(tmp_path, make_queue, later):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = appending_edit(queue)
    target = chat_message(50, "one")
    if later == "combined_update_queued":
        _add(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 50, "_required_sender_bot_id": "901",
                                          "text": "one (edited)"}, context=_context("member_update", member=MEMBER))
    else:
        target = chat_message(50, "one (edited)", edited=now())

    assert manager._resolve_held_send(row_id, Verdict.NOT_FOUND, target, BOT)
    assert hold_of(queue, row_id) is None


def test_member_update_edit_is_not_dropped_on_the_chat_edit_date(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = held(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 50, "_required_sender_bot_id": "901",
                                               "text": "one (edited)"}, context=_context("member_update", member=MEMBER))
    # A later applied append moved the chat's edit date past the attempt; it says nothing about this update.
    target = chat_message(50, "one\ntwo", edited=now())

    assert manager._resolve_held_send(row_id, Verdict.NOT_FOUND, target, BOT)
    assert row_id in rows(queue) and hold_of(queue, row_id) is None


def test_edit_not_applied_without_a_newer_edit_is_sent_again(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = held(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 50, "_required_sender_bot_id": "901", "text": "old"})
    _add(queue, "edit_message_text", {"chat_id": CHAT, "message_id": 51, "_required_sender_bot_id": "901", "text": "another message"})

    assert manager._resolve_held_send(row_id, Verdict.NOT_FOUND, chat_message(50, "first"), BOT)
    assert hold_of(queue, row_id) is None


def test_rows_in_flight_or_resolved_meanwhile_are_left_alone(tmp_path, make_queue):
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    running = held(queue, "send_message", {"chat_id": CHAT, "text": "a"})
    confirmed = held(queue, "send_message", {"chat_id": CHAT, "text": "b"})
    queue.record_telegram_completion(confirmed, b"\x01receipt", operator_confirmed=True)
    manager._outbound_scheduler.in_flight[running] = object()
    before = rows(queue)

    for row_id in (running, confirmed):
        assert not manager._resolve_held_send(row_id, Verdict.NOT_FOUND, None, BOT)
        assert not manager._resolve_held_send(row_id, Verdict.FOUND, chat_message(105, "a"), BOT)
    assert rows(queue) == before


def test_send_to_a_broadcast_channel_stays_held(tmp_path, make_queue, monkeypatch):
    monkeypatch.setattr(delivery_check, "PACE", 0.0)
    queue = make_queue(tmp_path)
    manager = manager_for(queue)
    row_id = held(queue, "send_message", {"chat_id": CHAT, "text": "hi", "_slave_id": "mod.chat"})
    age(queue, row_id, 7200)
    before = rows(queue)
    attempt = datetime.fromtimestamp(time.time() - 7180, timezone.utc)  # noqa: UP017 - datetime.UTC needs Python 3.11
    post = tl.Message(id=12, peer_id=tl.PeerChannel(1234), date=attempt, message="hi", post=True)
    resolved = []
    check = HeldSendCheck(
        api_id=1, api_hash="h", tokens={str(BOT): f"{BOT}:secret"},
        db=SimpleNamespace(logged_id_bounds=lambda *_: (10, 30), is_message_logged=lambda *_: False),
        list_rows=manager._held_rows, resolve=lambda *call: resolved.append(call),
        client_factory=lambda: RecordingClient({12: post}))

    counts = asyncio.run(check.run_pass())
    assert counts["chat_unreadable"] == 1 and not resolved
    assert rows(queue) == before


def test_check_starts_only_with_both_api_values():
    manager = manager_adapter()
    manager.logger = Mock()
    manager.channel = SimpleNamespace(db=None)
    manager.bot_pool = SimpleNamespace(bots=[SimpleNamespace(bot_id=BOT, _token=f"{BOT}:aux")])
    for config in ({"token": "900:main"}, {"token": "900:main", "api_id": 1}, {"token": "900:main", "api_hash": "h"}):
        assert manager._build_held_send_check(config) is None
    check = manager._build_held_send_check({"token": "900:main", "api_id": 1, "api_hash": "h"})
    assert isinstance(check, HeldSendCheck) and check._tokens == {None: "900:main", str(BOT): f"{BOT}:aux"}


def test_graceful_stop_ends_the_check_before_the_queue_closes():
    order = []
    check = HeldSendCheck(api_id=1, api_hash="h", tokens={}, db=None, list_rows=list, resolve=Mock())
    check.start()
    thread = check._thread
    stop = check.stop
    check.stop = lambda: (stop(), order.append("check"))
    done = threading.Event()
    done.set()
    manager = SimpleNamespace(
        logger=Mock(), _stopping=threading.Event(), _graceful_stop_lock=threading.Lock(), _graceful_stop_complete=False,
        _held_send_check=check, _metrics_httpd=None, _runtime=SimpleNamespace(_owns_loop_thread=True, shutdown=Mock()),
        stop_queued_worker=lambda: order.append("queue"), bot_pool=None, _shutdown_complete_event=done,
    )

    TelegramBotManager.graceful_stop(manager)

    assert order == ["check", "queue"] and not thread.is_alive()


@pytest.mark.parametrize(("mime", "attributes", "expected"), [
    ("image/jpeg", None, TGMsgType.Photo),
    ("video/mp4", [tl.DocumentAttributeAnimated(), tl.DocumentAttributeVideo(3, 320, 240)], TGMsgType.Animation),
    ("video/mp4", [tl.DocumentAttributeVideo(3, 320, 240)], TGMsgType.Video),
    ("image/webp", [tl.DocumentAttributeSticker("", tl.InputStickerSetEmpty()), tl.DocumentAttributeImageSize(512, 512)],
     TGMsgType.Sticker),
    ("audio/ogg", [tl.DocumentAttributeAudio(4, voice=True)], TGMsgType.Voice),
    ("audio/mpeg", [tl.DocumentAttributeAudio(4, title="t")], TGMsgType.Audio),
    ("application/zip", [tl.DocumentAttributeFilename("a.zip")], TGMsgType.Document),
])
def test_receipt_carries_the_type_the_chat_shows(mime, attributes, expected):
    if attributes is None:
        media = tl.MessageMediaPhoto(photo=tl.Photo(
            id=1, access_hash=1, file_reference=b"", date=now(), dc_id=1,
            sizes=[tl.PhotoStrippedSize("i", b""), tl.PhotoSize("x", 800, 600, 1000)]))
    else:
        media = tl.MessageMediaDocument(document=tl.Document(
            id=1, access_hash=1, file_reference=b"", date=now(), mime_type=mime, size=13,
            dc_id=1, attributes=attributes))
    seen = Seen.from_message(tl.Message(id=105, peer_id=tl.PeerChannel(1234), date=now(),
                                        message="caption", from_id=tl.PeerUser(BOT), media=media))

    receipt = _chat_receipt(seen, BOT)

    assert (receipt.chat_id, receipt.message_id, receipt.from_user.id, receipt.caption) == (CHAT, 105, BOT, "caption")
    assert get_msg_type(receipt) is expected
    assert receipt.effective_attachment is not None
