"""Deciding held sends from the chat: the matcher, the id range lookup and the check loop."""
import asyncio
import datetime
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types

from efb_telegram_master import delivery_check as dc
from efb_telegram_master.db import MsgLog
from efb_telegram_master.delivery_check import (
    Decision,
    HeldRow,
    HeldSendCheck,
    ReadState,
    RowKeys,
    Seen,
    Span,
    Verdict,
    decide,
    eligible,
    row_keys,
)
from efb_telegram_master.outbound import OutboundQueue, QueueRequest, SenderSelection
from tests.unit.test_database_safety import (  # noqa: F401
    manager_factory,
    postgres_config,
    postgres_server_config,
)

BOT = 111
OTHER_BOT = 222
CHAT = -1001234567890
NOW = 1_800_000_000.0
ATTEMPT = NOW - 3600


def keys(row_id, text="hello", *, kind="text", attempt=ATTEMPT, file_name="", target=None, thread=None):
    return RowKeys(id=row_id, chat_id=CHAT, author=BOT, kind=kind, attempt=attempt, thread=thread,
                   text=text, digest=dc._digest(text), file_name=file_name, target=target)


def seen(message_id, text="hello", *, date=ATTEMPT + 30, author=BOT, kind="text", forwarded=False, file_name="",
         thread=None):
    normal = " ".join(text.split())
    return Seen(id=message_id, date=date, author=author, kind=kind, file_name=file_name,
                digest=dc._digest(normal), length=len(normal), thread=thread, forwarded=forwarded, message=None)


def run(rows, messages, *, logged=(), state=ReadState.CLOSED, exact=True, now=NOW):
    chat = {item.id: item for item in messages}
    spans = {row.id: Span(100, 100 + 50, state, exact) for row in rows if row.kind != "edit"}
    known = set(logged)
    return decide(rows, spans, {CHAT: chat}, lambda chat_id, message_id: message_id in known, now)


def verdicts(decisions):
    return {row_id: decision.verdict for row_id, decision in decisions.items()}


def test_identical_rows_take_one_message_each():
    decisions = run([keys(1), keys(2)], [seen(105), seen(107)])
    assert verdicts(decisions) == {1: Verdict.FOUND, 2: Verdict.FOUND}
    assert {decisions[1].message.id, decisions[2].message.id} == {105, 107}


def test_identical_rows_with_fewer_messages_are_ambiguous():
    assert set(verdicts(run([keys(1), keys(2)], [seen(105)])).values()) == {Verdict.AMBIGUOUS}


def test_message_already_logged_is_not_a_match():
    assert verdicts(run([keys(1)], [seen(105)], logged={105})) == {1: Verdict.NOT_FOUND}


@pytest.mark.parametrize("other", [
    seen(105, author=OTHER_BOT), seen(105, forwarded=True), seen(105, date=ATTEMPT + 700),
    seen(105, date=ATTEMPT - 60), seen(105, kind="photo", text="hello"), seen(105, thread=77),
])
def test_message_that_differs_in_a_key_is_neither_a_match_nor_a_near_miss(other):
    assert verdicts(run([keys(1)], [other])) == {1: Verdict.NOT_FOUND}


def test_message_with_other_text_in_the_window_is_a_near_miss():
    assert verdicts(run([keys(1)], [seen(105, "something else")])) == {1: Verdict.AMBIGUOUS}


def test_cut_caption_matches_by_prefix():
    long_caption = "x" * 60 + " tail " + "y" * 40
    assert verdicts(run([keys(1, long_caption, kind="photo")], [seen(105, long_caption[:70], kind="photo")])) == {
        1: Verdict.FOUND}


def test_animation_may_arrive_as_a_video_and_file_name_breaks_ties():
    rows = [keys(1, "", kind="animation", file_name="a.mp4")]
    found = run(rows, [seen(105, "", kind="video", file_name="b.mp4"), seen(106, "", kind="video", file_name="a.mp4")])
    assert found[1] == Decision(Verdict.FOUND, found[1].message) and found[1].message.id == 106


@pytest.mark.parametrize("state, exact, expected", [
    (ReadState.CLOSED, True, Verdict.NOT_FOUND),
    (ReadState.CLOSED, False, Verdict.AMBIGUOUS),
    (ReadState.CAPPED, True, Verdict.AMBIGUOUS),
    (ReadState.OPEN, True, Verdict.TOO_EARLY),
])
def test_nothing_found_depends_on_how_far_the_chat_was_read(state, exact, expected):
    assert verdicts(run([keys(1)], [], state=state, exact=exact)) == {1: expected}


@pytest.mark.parametrize("state", [ReadState.CAPPED, ReadState.OPEN])
def test_a_match_in_an_unclosed_range_still_counts(state):
    assert verdicts(run([keys(1)], [seen(105)], state=state)) == {1: Verdict.FOUND}


def test_recent_attempt_is_too_early_for_its_whole_cluster():
    recent = keys(2, attempt=NOW - 30)
    assert verdicts(run([keys(1, attempt=NOW - 100), recent], [seen(105)])) == {1: Verdict.TOO_EARLY, 2: Verdict.TOO_EARLY}


def test_message_edited_since_with_other_text_is_a_near_miss():
    edited = types.Message(id=105, peer_id=types.PeerChannel(1), message="text changed later",
                           from_id=types.PeerUser(BOT), date=datetime.datetime.fromtimestamp(ATTEMPT + 30, datetime.timezone.utc),  # noqa: UP017 - datetime.UTC needs Python 3.11
                           edit_date=datetime.datetime.fromtimestamp(ATTEMPT + 90, datetime.timezone.utc))  # noqa: UP017
    assert verdicts(run([keys(1)], [Seen.from_message(edited)])) == {1: Verdict.AMBIGUOUS}


@pytest.mark.parametrize("header, row_thread", [
    # outside a forum, a reply to a reply names the top of its chain, which is no topic
    (types.MessageReplyHeader(reply_to_msg_id=90, reply_to_top_id=80), None),
    (types.MessageReplyHeader(reply_to_msg_id=90, reply_to_top_id=77, forum_topic=True), 77),
    (types.MessageReplyHeader(reply_to_msg_id=77, forum_topic=True), 77),
])
def test_only_a_forum_topic_is_a_topic(header, row_thread):
    reply = types.Message(id=105, peer_id=types.PeerChannel(1), message="hello", from_id=types.PeerUser(BOT),
                          date=datetime.datetime.fromtimestamp(ATTEMPT + 30, datetime.timezone.utc),  # noqa: UP017 - datetime.UTC needs Python 3.11
                          reply_to=header)
    assert verdicts(run([keys(1, thread=row_thread)], [Seen.from_message(reply)])) == {1: Verdict.FOUND}


def test_rows_in_different_clusters_never_share_one_message():
    # captionless photos with different file names are separate clusters, but only one photo was delivered
    rows = [keys(i, "", kind="photo", file_name=f"{name}.jpg") for i, name in enumerate("abc", 1)]
    assert set(verdicts(run(rows, [seen(105, "", kind="photo")])).values()) == {Verdict.AMBIGUOUS}
    found = run(rows[:2], [seen(105, "", kind="photo"), seen(106, "", kind="photo")])
    assert verdicts(found) == {1: Verdict.FOUND, 2: Verdict.FOUND}
    assert {found[1].message.id, found[2].message.id} == {105, 106}


def test_cluster_seen_by_only_some_of_its_rows_still_holds_the_message():
    # rows 1 and 2 sent a.jpg, row 3 sent b.jpg; only row 1's read range reaches the single photo
    rows = [keys(1, "", kind="photo", file_name="a.jpg"),
            keys(3, "", kind="photo", file_name="b.jpg", attempt=ATTEMPT + 2),
            keys(2, "", kind="photo", file_name="a.jpg", attempt=ATTEMPT + 20)]
    spans = {1: Span(100, 150, ReadState.CLOSED), 3: Span(100, 150, ReadState.CLOSED),
             2: Span(107, 150, ReadState.CLOSED)}
    out = decide(rows, spans, {CHAT: {105: seen(105, "", kind="photo")}}, lambda chat, message: message == 106, NOW)
    assert verdicts(out) == {1: Verdict.AMBIGUOUS, 2: Verdict.AMBIGUOUS, 3: Verdict.AMBIGUOUS}


def test_recent_cluster_holds_back_a_message_another_cluster_could_take():
    rows = [keys(1, "", kind="photo", file_name="a.jpg", attempt=ATTEMPT),
            keys(2, "", kind="photo", file_name="b.jpg", attempt=ATTEMPT + 3)]
    out = run(rows, [seen(105, "", kind="photo", date=ATTEMPT + 5)], now=ATTEMPT + dc.WINDOW + dc.AFTER_SLACK + 1)
    assert verdicts(out) == {1: Verdict.AMBIGUOUS, 2: Verdict.TOO_EARLY}


def test_cluster_whose_span_the_chat_does_not_confirm_is_ambiguous_and_keeps_its_message():
    rows = [keys(1, "", kind="photo", file_name="a.jpg"), keys(2, "", kind="photo", file_name="b.jpg", attempt=ATTEMPT + 3)]
    spans = {1: Span(100, 150, ReadState.UNTRUSTED), 2: Span(100, 150, ReadState.CLOSED)}
    out = decide(rows, spans, {CHAT: {105: seen(105, "", kind="photo")}}, lambda chat, message: False, NOW)
    assert verdicts(out) == {1: Verdict.AMBIGUOUS, 2: Verdict.AMBIGUOUS}


def test_edit_is_applied_when_the_target_shows_its_text():
    rows = [keys(1, "first", kind="edit", target=50), keys(2, "second", kind="edit", target=50),
            keys(3, "third", kind="edit", target=50)]
    found = run(rows, [seen(50, "second")])
    assert verdicts(found) == {1: Verdict.FOUND, 2: Verdict.FOUND, 3: Verdict.NOT_FOUND}
    assert found[2].message.id == 50


def test_edit_of_an_unreadable_target_is_not_found():
    assert verdicts(run([keys(1, "x", kind="edit", target=50)], [])) == {1: Verdict.NOT_FOUND}


def payload(operation="send_message", **kwargs):
    return OutboundQueue.encode_payload((), {"chat_id": CHAT, **kwargs})


def held(operation="send_message", hold="uncertain:TimedOut", attempt=ATTEMPT, **kwargs):
    return HeldRow(id=1, chat_id=CHAT, operation=operation, payload=payload(**kwargs), slave_id="mod.chat",
                   sender_bot_id=None, attempt_started_at=attempt, hold=hold)


def test_row_keys_read_the_payload():
    parsed = row_keys(held(text="<b>Hi</b>   there", parse_mode="HTML", message_thread_id=9), BOT)
    assert (parsed.kind, parsed.thread, parsed.text) == ("text", 9, "Hi there")
    caption = row_keys(held("send_photo", caption="a  caption", filename="p.jpg"), BOT)
    assert (caption.kind, caption.text, caption.file_name) == ("photo", "a caption", "p.jpg")
    edit = row_keys(held("edit_message_text", text="t", message_id=7), BOT)
    assert (edit.kind, edit.target) == ("edit", 7)


def test_caption_edit_is_left_to_the_operator():
    # a found caption edit would be logged without the file id that only Telegram's answer carries
    assert not eligible(held("edit_message_caption", caption="c", message_id=7))


@pytest.mark.parametrize("row", [
    held("edit_message_text", text="t"),                       # no target
    held("send_media_group"),                                  # not matched
    held(attempt=None),                                        # no attempt time
    HeldRow(1, CHAT, "send_message", b"\x09junk", "m", None, ATTEMPT, "uncertain:TimedOut"),
])
def test_row_without_usable_keys_is_skipped(row):
    assert row_keys(row, BOT) is None or not eligible(row)


@pytest.mark.parametrize("hold, expected", [
    ("uncertain:TimedOut", True), ("uncertain:operator_observed_delivery", False), ("in_flight", False),
    ("aggregation_failed:BadRequest", False), ("paused_by_newer_version", False),
])
def test_only_uncertain_holds_the_bot_made_are_checked(hold, expected):
    assert eligible(held(hold=hold)) is expected


def document(attributes):
    media = types.Document(id=1, access_hash=1, file_reference=b"", date=datetime.datetime.now(datetime.timezone.utc),  # noqa: UP017 - datetime.UTC needs Python 3.11
                           mime_type="x/y", size=1, dc_id=1, attributes=attributes)
    return types.MessageMediaDocument(document=media)


@pytest.mark.parametrize("attributes, kind", [
    ([types.DocumentAttributeSticker(alt="", stickerset=types.InputStickerSetEmpty())], "sticker"),
    ([types.DocumentAttributeVideo(duration=1, w=1, h=1), types.DocumentAttributeAnimated()], "animation"),
    ([types.DocumentAttributeVideo(duration=1, w=1, h=1)], "video"),
    ([types.DocumentAttributeAudio(duration=1, voice=True)], "voice"),
    ([types.DocumentAttributeAudio(duration=1)], "audio"),
    ([types.DocumentAttributeFilename(file_name="f.bin")], "document"),
])
def test_message_kind_is_read_from_the_document(attributes, kind):
    message = types.Message(id=5, peer_id=types.PeerChannel(1), date=datetime.datetime.now(datetime.timezone.utc),  # noqa: UP017 - datetime.UTC needs Python 3.11
                            message="", media=document(attributes), from_id=types.PeerUser(BOT))
    assert Seen.from_message(message).kind == kind


# --------------------------------------------------------------------------- the id range lookup
@pytest.fixture(params=["sqlite", "postgresql"])
def manager(request, tmp_path):
    config = request.getfixturevalue("postgres_config") if request.param == "postgresql" else None
    return request.getfixturevalue("manager_factory")(tmp_path, config)


def log(manager, message_id, when, *, chat=CHAT, origin="mod.chat", alt=None):
    with manager._managed_database.connection_context():
        MsgLog.create(master_msg_id=f"{chat}.{message_id}", master_msg_id_alt=alt, slave_message_id=f"s{message_id}",
                      text="t", slave_origin_uid=origin, msg_type="Text", sent_to="x", time=when)


def test_id_bounds_are_the_nearest_logged_ids_of_that_chat_and_source(manager):
    base = datetime.datetime(2026, 5, 1, 12, 0, 0)       # local time, as the log stores it
    log(manager, 10, base - datetime.timedelta(seconds=300))
    log(manager, 12, base - datetime.timedelta(seconds=100))
    log(manager, 13, base - datetime.timedelta(seconds=50), chat=-1009876543210)   # other Telegram chat
    log(manager, 14, base - datetime.timedelta(seconds=40), origin="mod.other")  # other source chat
    log(manager, 30, base + datetime.timedelta(seconds=900))
    log(manager, 40, base + datetime.timedelta(seconds=1500))
    bounds = manager.logged_id_bounds("mod.chat", CHAT, base, base + datetime.timedelta(seconds=660))
    assert bounds == (12, 30)
    assert manager.logged_id_bounds("mod.chat", CHAT, base - datetime.timedelta(seconds=200),
                                    base + datetime.timedelta(seconds=2000)) == (10, None)
    assert manager.is_message_logged(CHAT, 12) and not manager.is_message_logged(CHAT, 13)
    log(manager, 50, base, alt=f"{CHAT}.51")
    assert manager.is_message_logged(CHAT, 51)


# --------------------------------------------------------------------------- the check loop
class RecordingClient:
    """A Telegram client double: only login, the chat lookup and message reads by id are allowed."""

    def __init__(self, messages):
        self._messages = messages
        self.calls = []
        self.started = self.disconnected = False

    async def start(self, bot_token):
        self.started = True

    async def get_input_entity(self, chat_id):
        self.calls.append("entity")
        return types.InputPeerChannel(channel_id=1, access_hash=1)

    async def __call__(self, request):
        assert isinstance(request, functions.channels.GetMessagesRequest), request
        self.calls.append("read")
        wanted = [item.id for item in request.id]
        assert len(wanted) <= 100
        return SimpleNamespace(messages=[self._messages.get(i, types.MessageEmpty(id=i, peer_id=types.PeerChannel(1))) for i in wanted])

    async def disconnect(self):
        self.disconnected = True

    def __getattr__(self, name):
        raise AssertionError(f"unexpected client use: {name}")


def real_message(message_id, text, date, author=BOT):
    return types.Message(id=message_id, peer_id=types.PeerChannel(1), message=text, from_id=types.PeerUser(author),
                         date=datetime.datetime.fromtimestamp(date, datetime.timezone.utc))  # noqa: UP017 - datetime.UTC needs Python 3.11


@pytest.fixture
def scenario(manager, monkeypatch):
    """Four held rows in one chat: delivered, not delivered, a near miss, and one that is too recent."""
    monkeypatch.setattr(dc, "PACE", 0.0)
    now = time.time()
    attempt = now - 7200
    local = datetime.datetime.fromtimestamp
    log(manager, 10, local(attempt - 60))
    log(manager, 30, local(attempt + 900))
    messages = {
        10: real_message(10, "logged before", attempt - 60),
        12: real_message(12, "delivered text", attempt + 20),
        14: real_message(14, "near text, different", attempt + 400),
        30: real_message(30, "logged after", attempt + 900),
        31: real_message(31, "after the window", attempt + 1000),
    }
    rows = [
        HeldRow(1, CHAT, "send_message", payload(text="delivered text"), "mod.chat", None, attempt, "uncertain:TimedOut"),
        HeldRow(2, CHAT, "send_photo", payload(caption="never arrived"), "mod.chat", None, attempt + 100, "uncertain:TimedOut"),
        HeldRow(3, CHAT, "send_message", payload(text="near text"), "mod.chat", None, attempt + 399, "uncertain:TimedOut"),
        HeldRow(4, CHAT, "send_message", payload(text="recent"), "mod.chat", None, now - 5, "uncertain:TimedOut"),
        HeldRow(5, CHAT, "send_message", payload(text="operator"), "mod.chat", None, attempt, "uncertain:operator_observed_delivery"),
    ]
    applied = []
    client = RecordingClient(messages)
    check = HeldSendCheck(
        api_id=1, api_hash="h", tokens={None: f"{BOT}:secret"}, db=manager, list_rows=lambda: rows,
        resolve=lambda row_id, verdict, seen_message, bot: applied.append((row_id, verdict, seen_message and seen_message.id, bot)) or True,
        client_factory=lambda: client)
    return check, client, applied


def test_pass_resolves_found_and_not_found_and_leaves_the_rest(scenario):
    check, client, applied = scenario
    counts = asyncio.run(check.run_pass())
    assert sorted(applied) == [(1, Verdict.FOUND, 12, BOT), (2, Verdict.NOT_FOUND, None, BOT)]
    assert (counts["found"], counts["not_found"], counts["ambiguous"], counts["too_early"]) == (1, 1, 1, 1)
    assert set(client.calls) == {"entity", "read"}      # a lookup and reads by id; nothing else was used
    asyncio.run(check.run_pass())
    assert len(applied) == 2            # a decided row is not decided again; the recent one is still waiting


def check_one(manager, row, messages):
    applied = []
    check = HeldSendCheck(
        api_id=1, api_hash="h", tokens={None: f"{BOT}:secret"}, db=manager, list_rows=lambda: [row],
        resolve=lambda row_id, verdict, seen_message, bot: applied.append((row_id, verdict, seen_message and seen_message.id)) or True,
        client_factory=lambda: RecordingClient(messages))
    return applied, asyncio.run(check.run_pass())


@pytest.mark.parametrize("late", [None, "upper", "lower"])
def test_logged_messages_bound_the_span_only_when_the_chat_agrees(manager, monkeypatch, late):
    """The held send was delivered as message 108. A log entry written late carries an id out of time order:
    a confirmation logs an older message now, a combined message sent late keeps its first source's time."""
    monkeypatch.setattr(dc, "PACE", 0.0)
    attempt = time.time() - 3600
    local = datetime.datetime.fromtimestamp
    log(manager, 100, local(attempt - 60))
    messages = {
        100: real_message(100, "logged before", attempt - 60),
        105: real_message(105, "an older send", attempt - 30),
        108: real_message(108, "second", attempt + 20),
        120: real_message(120, "combined, sent late", attempt + 300),
        130: real_message(130, "after the window", attempt + 1000, author=OTHER_BOT),
    }
    if late == "upper":
        log(manager, 105, local(attempt + 700))     # logged after the window, dated before the attempt
    elif late == "lower":
        log(manager, 120, local(attempt - 30))      # logged before the attempt, dated after it
    row = HeldRow(2, CHAT, "send_message", payload(text="second"), "mod.chat", None, attempt, "uncertain:TimedOut")

    applied, counts = check_one(manager, row, messages)

    if late is None:
        assert applied == [(2, Verdict.FOUND, 108)]
    else:
        assert applied == [] and counts["ambiguous"] == 1


def test_a_logged_message_dated_just_before_the_attempt_does_not_hide_a_delivery(manager, monkeypatch):
    """The host clock runs a few seconds ahead of Telegram's: the delivery is dated 2 s before the attempt, and a
    combined message sent right after it is dated 1 s before it while logged with its first source's older time."""
    monkeypatch.setattr(dc, "PACE", 0.0)
    attempt = time.time() - 3600
    local = datetime.datetime.fromtimestamp
    log(manager, 100, local(attempt - 60))
    log(manager, 120, local(attempt - 30))
    log(manager, 130, local(attempt + 900))
    messages = {
        100: real_message(100, "logged before", attempt - 60),
        108: real_message(108, "second", attempt - 2),
        120: real_message(120, "combined, sent late", attempt - 1),
        130: real_message(130, "logged after", attempt + 900),
    }
    row = HeldRow(2, CHAT, "send_message", payload(text="second"), "mod.chat", None, attempt, "uncertain:TimedOut")

    applied, counts = check_one(manager, row, messages)

    assert applied == [] and counts["ambiguous"] == 1


def test_a_dropped_read_session_is_opened_again_by_the_next_pass(manager, monkeypatch):
    monkeypatch.setattr(dc, "PACE", 0.0)
    attempt = time.time() - 3600
    log(manager, 100, datetime.datetime.fromtimestamp(attempt - 60))
    messages = {100: real_message(100, "logged before", attempt - 60), 108: real_message(108, "second", attempt + 20),
                130: real_message(130, "logged after", attempt + 1000)}
    built = []

    class Dropped(RecordingClient):
        """A client whose connection is gone: every request raises until it is replaced."""
        dropped = False

        async def __call__(self, request):
            if self.dropped:
                raise ConnectionError("Cannot send requests while disconnected")
            return await super().__call__(request)

    def factory():
        built.append(Dropped(messages))
        return built[-1]

    def held_row(row_id):
        return HeldRow(row_id, CHAT, "send_message", payload(text="second"), "mod.chat", None, attempt, "uncertain:TimedOut")

    rows = [held_row(2)]
    applied = []
    check = HeldSendCheck(
        api_id=1, api_hash="h", tokens={None: f"{BOT}:secret"}, db=manager, list_rows=lambda: rows,
        resolve=lambda row_id, verdict, seen_message, bot: applied.append((row_id, verdict, seen_message and seen_message.id)) or True,
        client_factory=factory)

    async def passes():
        first = await check.run_pass()
        built[0].dropped = True
        rows[:] = [held_row(3)]
        return first, await check.run_pass(), await check.run_pass()

    first, failed, again = asyncio.run(passes())

    assert first["found"] == 1 and failed["bot_failed"] == 1
    assert again["found"] == 1 and applied == [(2, Verdict.FOUND, 108), (3, Verdict.FOUND, 108)]
    assert len(built) == 2 and built[0].disconnected


def test_a_logged_message_the_chat_does_not_show_bounds_nothing(manager, monkeypatch):
    monkeypatch.setattr(dc, "PACE", 0.0)
    attempt = time.time() - 3600
    log(manager, 100, datetime.datetime.fromtimestamp(attempt - 60))     # deleted from the chat since
    row = HeldRow(2, CHAT, "send_message", payload(text="second"), "mod.chat", None, attempt, "uncertain:TimedOut")

    applied, counts = check_one(manager, row, {130: real_message(130, "after the window", attempt + 1000)})

    assert applied == [] and counts["ambiguous"] == 1


def test_flood_wait_is_waited_out_and_the_read_retried(monkeypatch):
    monkeypatch.setattr(dc, "PACE", 0.0)
    monkeypatch.setattr(dc, "FLOOD_EXTRA", 0.0)
    pending = [errors.FloodWaitError(request=None, capture=0)]

    class Flooded(RecordingClient):
        async def __call__(self, request):
            if pending:
                raise pending.pop()
            return await super().__call__(request)

    flooded = Flooded({7: real_message(7, "x", ATTEMPT)})
    reader = dc.BotReader(f"{BOT}:secret", 1, "h", client_factory=lambda: flooded)
    result = asyncio.run(reader.messages(CHAT, [7]))
    assert set(result) == {7} and reader.flood_waits == 1


def test_calls_stay_paced_after_a_failed_call(monkeypatch):
    monkeypatch.setattr(dc, "PACE", 1000.0)

    class Refusing(RecordingClient):
        async def get_input_entity(self, chat_id):
            raise errors.ChannelPrivateError(request=None)

    reader = dc.BotReader(f"{BOT}:secret", 1, "h", client_factory=lambda: Refusing({}))
    with pytest.raises(dc.ChatUnreadable):
        asyncio.run(reader.messages(CHAT, [7]))
    assert reader._next_call - time.monotonic() > 900


def test_thread_runs_a_pass_and_stop_disconnects(scenario, monkeypatch):
    check, client, applied = scenario
    monkeypatch.setattr(dc, "FIRST_PASS_DELAY", 0.05)
    check.start()
    deadline = time.time() + 10
    while len(applied) < 2 and time.time() < deadline:
        time.sleep(0.05)
    check.stop(timeout=5)
    assert len(applied) == 2
    assert client.disconnected and check._thread is None


def test_stop_before_the_first_pass_ends_the_thread_at_once(scenario):
    check, _client, applied = scenario
    check.start()
    started = time.time()
    check.stop(timeout=5)
    assert time.time() - started < 2 and not applied


# --------------------------------------------------------------------------- the report for the operator
def send_message(chat_id, text, **kwargs):
    return None


def send_document(chat_id, document, **kwargs):
    return None


@pytest.fixture(params=["sqlite", "postgresql"])
def channel(request, tmp_path, monkeypatch):
    """A channel data directory with a live queue, a message log and five held sends in one chat."""
    config = request.getfixturevalue("postgres_config") if request.param == "postgresql" else None
    directory = tmp_path / "channel"
    directory.mkdir()
    manager = request.getfixturevalue("manager_factory")(directory, config)
    (directory / "config.yaml").write_text(json.dumps(
        {"token": f"{BOT}:secret-token", "api_id": 7, "api_hash": "secret-hash", **({"database": config} if config else {})}))
    monkeypatch.setattr(dc, "PACE", 0.0)
    now = time.time()
    attempt = now - 7200
    local = datetime.datetime.fromtimestamp
    log(manager, 10, local(attempt - 60))
    log(manager, 30, local(attempt + 900))
    queue = OutboundQueue(directory, database=config)
    request.addfinalizer(queue.close)
    rows = [("delivered text", attempt), ("never arrived", attempt + 100), ("twin", attempt + 200),
            ("twin", attempt + 201), ("recent", now - 5)]
    ids = {}
    for text, started in rows:
        request = (QueueRequest("send_document", (), {"chat_id": CHAT, "document": b"file", "caption": text}, None)
                   if text == "never arrived" else
                   QueueRequest("send_message", (), {"chat_id": CHAT, "text": text}, None))
        row_id, _ = queue.enqueue_many([request], {"send_message": send_message, "send_document": send_document}.__getitem__)
        ids[text if text not in ids else text + " again"] = row_id
        queue.begin_delivery_attempt(row_id, SenderSelection(sender=None, sender_bot_id=None))
        queue.hold_uncertain_delivery(row_id, TimeoutError())
        with queue._lock, queue.connection:
            queue.connection.execute("UPDATE outbound_queue SET slave_id=?, attempt_started_at=? WHERE id=?",
                                     ("mod.chat", started, row_id))
    client = RecordingClient({
        10: real_message(10, "logged before", attempt - 60),
        12: real_message(12, "delivered text", attempt + 20),
        14: real_message(14, "twin", attempt + 210),     # one message for two identical rows
        30: real_message(30, "logged after", attempt + 900),
        31: real_message(31, "after the window", attempt + 1000),
    })
    monkeypatch.setattr(dc, "TelegramClient", lambda *args, **kwargs: client)
    return SimpleNamespace(directory=directory, queue=queue, ids=ids, client=client, config=config)


def _queue_state(channel):
    with channel.queue._lock:
        return channel.queue.connection.execute("SELECT * FROM outbound_queue ORDER BY id").fetchall()


def test_report_lists_only_the_rows_not_delivered_and_changes_nothing(channel, tmp_path, capsys):
    files = [channel.directory / name for name in ("outbound-queue.sqlite3", "tgdata.db")]
    before = ([hashlib.sha256(path.read_bytes()).digest() for path in files] if channel.config is None else None, _queue_state(channel))
    schema = channel.queue.connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() \
        if channel.config is None else None
    out = tmp_path / "not-delivered.txt"
    assert dc.main(["--data-dir", str(channel.directory), "--not-delivered-out", str(out)]) == 0
    assert out.read_text() == f"{channel.ids['never arrived']}\n"      # delivered, undecided and recent rows are not listed
    shown = capsys.readouterr()
    assert "found=1" in shown.out and "not_found=1" in shown.out and "ambiguous=2" in shown.out and "too_early=1" in shown.out
    for secret in ("secret-token", "secret-hash", "delivered text"):
        assert secret not in shown.out + shown.err
    assert set(channel.client.calls) == {"entity", "read"}      # the chat was only looked up and read
    after = ([hashlib.sha256(path.read_bytes()).digest() for path in files] if channel.config is None else None, _queue_state(channel))
    assert after == before        # the queue file and the log are byte-identical, the rows unchanged
    if channel.config is None:
        assert channel.queue.connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall() == schema
    # the clear step reads one row id per line
    assert frozenset(int(line) for line in out.read_text().split()) == {channel.ids["never arrived"]}


def test_report_without_a_finished_read_writes_no_file(channel, tmp_path, capsys):
    class Refusing(RecordingClient):
        async def start(self, bot_token):
            raise ConnectionError(bot_token)

    channel.client.__class__ = Refusing
    out = tmp_path / "not-delivered.txt"
    assert dc.report_not_delivered(channel.directory, out, api_id=1, api_hash="h") == 1
    assert not out.exists() and not list(tmp_path.glob("*.part"))
    assert "secret-token" not in capsys.readouterr().out


def test_report_needs_both_api_keys_in_the_config(channel, tmp_path, capsys):
    (channel.directory / "config.yaml").write_text(json.dumps({"token": f"{BOT}:secret-token"}))
    out = tmp_path / "not-delivered.txt"
    with pytest.raises(SystemExit) as stopped:
        dc.main(["--data-dir", str(channel.directory), "--not-delivered-out", str(out)])
    assert stopped.value.code == 1 and not out.exists()
    assert "secret-token" not in capsys.readouterr().err


def test_report_reads_a_queue_file_from_an_older_version(tmp_path):
    directory = tmp_path / "old"
    directory.mkdir()
    (directory / "config.yaml").write_text(json.dumps({"token": f"{BOT}:t"}))
    (directory / "tgdata.db").touch()
    with closing(sqlite3.connect(directory / "outbound-queue.sqlite3")) as db, db:
        db.execute("CREATE TABLE outbound_queue (id INTEGER PRIMARY KEY, telegram_chat_id INTEGER, operation TEXT, "
                   "payload BLOB, slave_id TEXT, delivery_state TEXT, delivery_hold TEXT)")
        db.execute("INSERT INTO outbound_queue VALUES (1, ?, 'send_message', x'00', 'mod.chat', 'queued', 'uncertain:TimedOut')",
                   (CHAT,))
    out = tmp_path / "not-delivered.txt"
    assert dc.report_not_delivered(directory, out, api_id=1, api_hash="h") == 0
    assert out.read_text() == ""       # no attempt time is recorded, so the row cannot be decided
