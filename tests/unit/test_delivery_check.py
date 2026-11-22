"""Deciding held sends from the chat: the matcher, the id range lookup and the check loop."""
import asyncio
import datetime
import time
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
from efb_telegram_master.outbound import OutboundQueue
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


def seen(message_id, text="hello", *, date=ATTEMPT + 30, author=BOT, kind="text", edited=False,
         forwarded=False, file_name="", thread=None):
    normal = " ".join(text.split())
    return Seen(id=message_id, date=date, author=author, kind=kind, file_name=file_name,
                digest=dc._digest(normal), length=len(normal), thread=thread, forwarded=forwarded,
                edited=edited, message=None)


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


def test_message_edited_since_matches_on_other_keys_for_one_cluster_only():
    edited = seen(105, "text changed later", edited=True)
    assert verdicts(run([keys(1)], [edited])) == {1: Verdict.FOUND}
    # two different clusters compete for the same edited message: neither may claim it
    assert set(verdicts(run([keys(1), keys(2, "another text")], [edited])).values()) == {Verdict.AMBIGUOUS}


def test_rows_in_different_clusters_never_share_one_message():
    # captionless photos with different file names are separate clusters, but only one photo was delivered
    rows = [keys(i, "", kind="photo", file_name=f"{name}.jpg") for i, name in enumerate("abc", 1)]
    assert set(verdicts(run(rows, [seen(105, "", kind="photo")])).values()) == {Verdict.AMBIGUOUS}
    found = run(rows[:2], [seen(105, "", kind="photo"), seen(106, "", kind="photo")])
    assert verdicts(found) == {1: Verdict.FOUND, 2: Verdict.FOUND}
    assert {found[1].message.id, found[2].message.id} == {105, 106}


def test_edited_message_matched_on_text_by_one_cluster_is_not_claimed_by_another():
    matched = seen(105, "hello", edited=True)
    assert verdicts(run([keys(1), keys(2, "another text")], [matched])) == {1: Verdict.FOUND, 2: Verdict.AMBIGUOUS}


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
    edit = row_keys(held("edit_message_caption", caption="c", message_id=7), BOT)
    assert (edit.kind, edit.target) == ("edit", 7)


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
        12: real_message(12, "delivered text", attempt + 20),
        14: real_message(14, "near text, different", attempt + 400),
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


def test_flood_wait_is_waited_out_and_the_read_retried(monkeypatch):
    monkeypatch.setattr(dc, "PACE", 0.0)
    monkeypatch.setattr(dc, "FLOOD_EXTRA", 0.0)
    client = RecordingClient({7: real_message(7, "x", ATTEMPT)})
    original = client.__call__
    pending = [errors.FloodWaitError(request=None, capture=0)]

    class Flooded(RecordingClient):
        async def __call__(self, request):
            if pending:
                raise pending.pop()
            return await original(request)

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
