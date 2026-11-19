"""Background creation of user custom emoji against a local fake Bot API server, on real SQLite profiles."""
import email.parser
import hashlib
import io
import itertools
import json
import logging
import sqlite3
import statistics
import threading
import time
import urllib.parse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import telegram
from ehforwarderbot.exceptions import EFBOperationNotSupported
from PIL import Image
from telegram.request import HTTPXRequest

from efb_telegram_master.custom_emoji import (
    AUTHOR_EMOJI_MACHINE,
    AuthorEmojiEvent,
    AuthorEmojiState,
    CustomEmojiConfig,
    CustomEmojiStore,
    FeatureState,
    IllegalTransition,
    UserEmojiCreator,
    fallback_emoji,
)
from efb_telegram_master.db import database

from .test_custom_emoji import profile, write_legacy_tables  # noqa: F401  (fixture)

BOT = "testbot"
OWNER = 4242
SET_6 = f"etm_user_avatars_6_by_{BOT}"
SET_7 = f"etm_user_avatars_7_by_{BOT}"
WRITES = {"addStickerToSet", "createNewStickerSet"}


def legacy_rows(count, set_name=SET_6):
    return [(f"member-user:old{i}", f"old-{i}", set_name, "2026-06-23 10:00:00", "2026-06-23 10:00:00")
            for i in range(count)]


class FakeBotApi:
    """Answers the sticker methods the way the Bot API does and records every call."""

    def __init__(self, sets=None):
        self.sets: dict[str, list[str]] = {name: list(ids) for name, ids in (sets or {}).items()}
        self.calls: list[tuple[float, str, dict]] = []
        self.files: list[bytes] = []
        self.full_on_add: set[str] = set()
        self.errors: dict[str, str] = {}
        self._next_id = 9000
        self._lock = threading.Lock()
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                params, files = api.parse(self.headers.get("Content-Type", ""), body)
                status, payload = api.answer(self.path.rsplit("/", 1)[-1], params, files)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def parse(content_type, body):
        if content_type.startswith("multipart/"):
            message = email.parser.BytesParser().parsebytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
            params, files = {}, {}
            for part in message.get_payload():
                name = part.get_param("name", header="content-disposition")
                if part.get_filename():
                    files[name] = part.get_payload(decode=True)
                else:
                    params[name] = part.get_payload(decode=True).decode()
            return params, files
        return {key: values[0] for key, values in urllib.parse.parse_qs(body.decode()).items()}, {}

    def answer(self, method, params, files):
        with self._lock:
            self.calls.append((time.monotonic(), method, params))
            self.files.extend(files.values())
            if method in self.errors:
                return 400, {"ok": False, "error_code": 400, "description": self.errors[method]}
            name = params.get("name")
            if method == "getStickerSet":
                if name not in self.sets:
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: STICKERSET_INVALID"}
                return 200, {"ok": True, "result": {
                    "name": name, "title": "t", "sticker_type": "custom_emoji",
                    "stickers": [{"file_id": i, "file_unique_id": i, "type": "custom_emoji", "width": 100,
                                  "height": 100, "is_animated": False, "is_video": False, "custom_emoji_id": i}
                                 for i in self.sets[name]]}}
            if method == "createNewStickerSet":
                if name in self.sets:
                    return 400, {"ok": False, "error_code": 400,
                                 "description": "Bad Request: sticker set name is already occupied"}
                self.sets[name] = [self._new_id()]
                return 200, {"ok": True, "result": True}
            if method == "addStickerToSet":
                if name not in self.sets:
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: STICKERSET_INVALID"}
                if name in self.full_on_add or len(self.sets[name]) >= 200:
                    return 400, {"ok": False, "error_code": 400, "description": "Bad Request: STICKERS_TOO_MUCH"}
                self.sets[name].append(self._new_id())
                return 200, {"ok": True, "result": True}
            return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    def _new_id(self):
        self._next_id += 1
        return str(self._next_id)

    def methods(self, *names):
        return [(method, params.get("name")) for _, method, params in self.calls if not names or method in names]

    def bot(self):
        return telegram.Bot("123:abc", base_url=f"http://127.0.0.1:{self.server.server_port}/bot",
                            request=HTTPXRequest(connection_pool_size=1))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api():
    server = FakeBotApi()
    yield server
    server.close()


@pytest.fixture
def make_creator(profile, api):  # noqa: F811
    directory, open_manager = profile
    creators = []

    def make(user_rows=None, config=None, **options):
        if user_rows is not None:
            write_legacy_tables(directory, user_rows=user_rows)
        store = CustomEmojiStore(open_manager(), CustomEmojiConfig.from_channel_config(
            config or {"admins": [OWNER]}))
        store.load()
        options.setdefault("write_interval", 0.0)
        creator = UserEmojiCreator(store, api.bot, BOT, "ETM user avatars", **options)
        creators.append(creator)
        return store, creator

    yield make
    for creator in creators:
        creator.stop()


def avatar():
    picture = io.BytesIO()
    Image.new("RGB", (640, 480), (200, 30, 30)).save(picture, "JPEG")
    picture.seek(0)
    return picture


def saved_rows(directory):
    with closing(sqlite3.connect(directory / "tgdata.db")) as connection:
        return {key: (emoji_id, set_name) for key, emoji_id, set_name in connection.execute(
            "SELECT cache_key, custom_emoji_id, sticker_set_name FROM useremojicache")}


def wait_until(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def settled(creator, uids):
    return lambda: all(creator.author_state(uid) in (AuthorEmojiState.READY, AuthorEmojiState.FAILED) for uid in uids)


def test_known_author_costs_no_telegram_or_database_call(make_creator, api, monkeypatch):
    _, creator = make_creator(legacy_rows(3), config={"admins": [OWNER], "user_emoji": {
        "custom_emoji_ids": {"configured": "c-1"}}})
    creator.start()
    queries = []
    original = database.obj.execute_sql
    monkeypatch.setattr(database.obj, "execute_sql", lambda *a, **k: queries.append(a) or original(*a, **k))
    loaded = []
    for uid in ("old0", "old2", "configured"):
        creator.request(uid, lambda: loaded.append(1) or avatar())
    time.sleep(0.2)
    assert (api.calls, queries, loaded) == ([], [], [])
    assert creator.author_state("old0") is AuthorEmojiState.READY


def test_new_authors_continue_the_last_saved_set_and_move_on_when_it_fills(make_creator, api, profile):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = [f"s{i}" for i in range(190)]
    store, creator = make_creator(legacy_rows(2), write_interval=0.05)
    creator.start()
    uids = [f"slave.channel_user{i}" for i in range(20)]
    for uid in uids:
        creator.request(uid, avatar)
        creator.request(uid, avatar)  # duplicate while queued
    wait_until(settled(creator, uids))

    rows = saved_rows(directory)
    for uid in uids:
        assert creator.author_state(uid) is AuthorEmojiState.READY
        assert store.lookup(uid).custom_emoji_id == rows["member-user:" + uid][0]
    assert [rows["member-user:" + uid][1] for uid in uids] == [SET_6] * 10 + [SET_7] * 10
    assert len({emoji for emoji, _ in rows.values()}) == len(rows)
    assert api.sets[SET_6][190:] == [rows["member-user:" + uid][0] for uid in uids[:10]]
    assert api.sets[SET_7] == [rows["member-user:" + uid][0] for uid in uids[10:]]

    reads = api.methods("getStickerSet")
    assert reads.count(("getStickerSet", SET_6)) == 1 + 10  # one look-up, one read-back per new emoji
    assert reads.count(("getStickerSet", SET_7)) == 1 + 10
    assert {name for _, name in reads} == {SET_6, SET_7}
    assert api.methods(*WRITES) == [("addStickerToSet", SET_6)] * 10 + [("createNewStickerSet", SET_7)] + [
        ("addStickerToSet", SET_7)] * 9
    write_times = [at for at, method, _ in api.calls if method in WRITES]
    assert min(b - a for a, b in itertools.pairwise(write_times)) > 0.04  # server-side arrival jitter


def test_sticker_matches_earlier_releases(make_creator, api):
    _, creator = make_creator(legacy_rows(1))
    creator.start()
    uid = "slave.channel_alice"
    creator.request(uid, avatar)
    wait_until(settled(creator, [uid]))

    (_, method, params), = [call for call in api.calls if call[1] in WRITES]
    (sticker,) = json.loads(params["stickers"])
    assert (method, params["name"], params["sticker_type"], params["title"]) == (
        "createNewStickerSet", SET_6, "custom_emoji", "ETM user avatars")
    assert params["user_id"] == str(OWNER)
    assert sticker["emoji_list"] == [fallback_emoji(uid)]
    assert sticker["keywords"] == ["etm-" + hashlib.sha1(uid.encode()).hexdigest()[:16]]
    assert sticker["format"] == "static"
    image = Image.open(io.BytesIO(api.files[0]))
    assert (image.format, image.size) == ("PNG", (100, 100))


def test_set_reported_full_moves_to_the_next_set(make_creator, api, profile):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = [f"s{i}" for i in range(150)]
    api.full_on_add.add(SET_6)
    _, creator = make_creator(legacy_rows(1))
    creator.start()
    creator.request("bob", avatar)
    wait_until(settled(creator, ["bob"]))

    assert saved_rows(directory)["member-user:bob"] == (api.sets[SET_7][0], SET_7)
    assert api.methods() == [("getStickerSet", SET_6), ("addStickerToSet", SET_6), ("getStickerSet", SET_7),
                             ("createNewStickerSet", SET_7), ("getStickerSet", SET_7)]


def test_failed_look_up_of_the_next_set_does_not_skip_it(make_creator, api, profile):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = [f"s{i}" for i in range(199)]
    _, creator = make_creator(legacy_rows(1))
    creator.start()
    creator.request("ann", avatar)
    wait_until(settled(creator, ["ann"]))
    api.errors["getStickerSet"] = "Bad Request: temporarily unavailable"
    creator.request("ben", avatar)
    wait_until(settled(creator, ["ben"]))
    del api.errors["getStickerSet"]
    creator.request("cid", avatar)
    wait_until(settled(creator, ["cid"]))

    rows = saved_rows(directory)
    assert creator.author_state("ben") is AuthorEmojiState.FAILED
    assert (rows["member-user:ann"][1], rows["member-user:cid"][1]) == (SET_6, SET_7)


def test_failed_author_waits_for_its_retry_time(make_creator, api):
    now = [1000.0]
    _, creator = make_creator(legacy_rows(1), retry_base=60.0, clock=lambda: now[0])
    creator.start()
    attempts = []

    def no_avatar():
        attempts.append(now[0])
        raise EFBOperationNotSupported()

    creator.request("nobody", no_avatar)
    wait_until(settled(creator, ["nobody"]))
    assert creator.author_state("nobody") is AuthorEmojiState.FAILED
    for _ in range(5):
        creator.request("nobody", no_avatar)
    now[0] += 59.0
    creator.request("nobody", no_avatar)
    time.sleep(0.1)
    assert attempts == [1000.0]

    now[0] += 1.0
    creator.request("nobody", no_avatar)
    wait_until(lambda: len(attempts) == 2 and creator.author_state("nobody") is AuthorEmojiState.FAILED)
    now[0] += 60.0  # the second failure doubles the wait
    creator.request("nobody", no_avatar)
    time.sleep(0.1)
    assert len(attempts) == 2
    assert api.methods(*WRITES) == []


def test_telegram_error_fails_only_that_author(make_creator, api):
    now = [0.0]
    _, creator = make_creator(legacy_rows(1), clock=lambda: now[0])
    creator.start()
    api.errors["addStickerToSet"] = "Bad Request: STICKER_PNG_NOPNG"
    api.sets[SET_6] = ["s0"]
    creator.request("carol", avatar)
    wait_until(settled(creator, ["carol"]))
    creator.request("carol", avatar)
    time.sleep(0.1)

    assert creator.author_state("carol") is AuthorEmojiState.FAILED
    assert creator.feature_state is FeatureState.ENABLED
    assert api.methods(*WRITES) == [("addStickerToSet", SET_6)]


def test_permission_error_makes_the_feature_unavailable(make_creator, api, caplog):
    _, creator = make_creator(legacy_rows(1))
    creator.start()
    api.errors["createNewStickerSet"] = "Bad Request: PEER_ID_INVALID"
    with caplog.at_level(logging.WARNING, logger="efb_telegram_master.custom_emoji"):
        creator.request("dave", avatar)
        wait_until(settled(creator, ["dave"]))
        calls = len(api.calls)
        loaded = []
        for uid in ("erin", "frank", "dave"):
            creator.request(uid, lambda: loaded.append(1) or avatar())
        time.sleep(0.1)

    assert creator.feature_state is FeatureState.UNAVAILABLE
    assert "peer_id_invalid" in creator.unavailable_reason.lower()
    assert [record.levelname for record in caplog.records if record.name == "efb_telegram_master.custom_emoji"] == [
        "WARNING"]
    assert (len(api.calls), loaded) == (calls, [])
    assert creator.author_state("erin") is AuthorEmojiState.ABSENT


def test_full_queue_drops_without_waiting(make_creator, api):
    _, creator = make_creator(legacy_rows(1), queue_size=3)  # worker not started: nothing leaves the queue
    for uid in ("a", "b", "c"):
        creator.request(uid, avatar)
    durations = []
    for i in range(200):
        started = time.perf_counter()
        creator.request(f"late{i}", avatar)
        durations.append(time.perf_counter() - started)

    assert statistics.median(durations) < 0.001
    assert [creator.author_state(uid) for uid in ("a", "b", "c", "late0")] == [AuthorEmojiState.QUEUED] * 3 + [
        AuthorEmojiState.ABSENT]
    assert api.calls == []


def test_stop_returns_within_its_bound_with_jobs_queued(make_creator, api):
    _, creator = make_creator(legacy_rows(1), write_interval=30.0, join_timeout=2.0)
    api.sets[SET_6] = ["s0"]
    creator.start()
    uids = [f"u{i}" for i in range(5)]
    for uid in uids:
        creator.request(uid, avatar)
    wait_until(lambda: creator.author_state("u0") is AuthorEmojiState.READY)
    started = time.monotonic()
    creator.stop()

    assert time.monotonic() - started < 1.0
    assert not creator._thread.is_alive()
    assert creator.author_state("u2") is AuthorEmojiState.QUEUED
    creator.request("late", avatar)
    assert creator.author_state("late") is AuthorEmojiState.ABSENT


def test_disabled_feature_starts_no_worker(make_creator, api):
    _, creator = make_creator(legacy_rows(1), config={"admins": [OWNER], "user_emoji": {"enabled": False}})
    creator.start()
    creator.request("gina", avatar)

    assert creator.feature_state is FeatureState.DISABLED_BY_CONFIG
    assert creator._thread is None
    assert creator.author_state("gina") is AuthorEmojiState.ABSENT


def test_illegal_author_transition_names_the_author():
    with pytest.raises(IllegalTransition, match="'slave.channel_hal'.*'requested'.*'ready'"):
        AUTHOR_EMOJI_MACHINE.next_state("slave.channel_hal", AuthorEmojiState.READY, AuthorEmojiEvent.REQUESTED)
