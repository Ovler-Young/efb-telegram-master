"""Background creation of user custom emoji against a local fake Bot API server, on real SQLite profiles."""
import email.parser
import hashlib
import io
import json
import logging
import sqlite3
import threading
import time
import types
import urllib.parse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import telegram
from ehforwarderbot.exceptions import EFBOperationNotSupported
from PIL import Image
from telegram.request import HTTPXRequest

from efb_telegram_master import custom_emoji
from efb_telegram_master.custom_emoji import (
    CustomEmojiConfig,
    CustomEmojiStore,
    FeatureState,
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
        self.lose_reply: set[str] = set()
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
            if method in self.lose_reply:  # the call takes effect, but the client sees a server error
                self.lose_reply.discard(method)
                self._apply(method, params)
                return 500, {"ok": False, "error_code": 500, "description": "Internal Server Error"}
            return self._apply(method, params)

    def _apply(self, method, params):
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
def make_creator(profile, api, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG, logger="efb_telegram_master.custom_emoji")
    directory, open_manager = profile
    creators = []

    def make(user_rows=None, config=None, **options):
        """``options`` replace the module's pacing and size limits (``write_interval`` -> ``WRITE_INTERVAL``)."""
        if user_rows is not None:
            write_legacy_tables(directory, user_rows=user_rows)
        store = CustomEmojiStore(open_manager(), CustomEmojiConfig.from_channel_config(
            config or {"admins": [OWNER]}))
        store.load()
        options.setdefault("write_interval", 0.0)
        for name, value in options.items():
            monkeypatch.setattr(custom_emoji, name.upper(), value)
        creator = UserEmojiCreator(store, api.bot, BOT, "ETM user avatars")
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


def failures(caplog, uid):
    return sum(f"No custom emoji for {uid} (" in record.getMessage() for record in caplog.records)


def settled(store, caplog, uids):
    """Every author has an emoji, or the worker gave up on it for now."""
    return lambda: all(store.lookup(uid) is not None or failures(caplog, uid) for uid in uids)


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


def test_new_authors_continue_the_last_saved_set_and_move_on_when_it_fills(make_creator, api, profile, caplog):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = [f"s{i}" for i in range(190)]
    store, creator = make_creator(legacy_rows(2))
    creator.start()
    uids = [f"slave.channel_user{i}" for i in range(20)]
    for uid in uids:
        creator.request(uid, avatar)
        creator.request(uid, avatar)  # duplicate while queued
    wait_until(settled(store, caplog, uids))

    rows = saved_rows(directory)
    for uid in uids:
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


def test_writes_wait_for_the_write_interval(make_creator, api, caplog):
    api.sets[SET_6] = ["s0"]
    store, creator = make_creator(legacy_rows(1), write_interval=30.0)
    creator.start()
    for uid in ("u0", "u1", "u2"):
        creator.request(uid, avatar)
    wait_until(lambda: store.lookup("u0") is not None)
    time.sleep(0.2)  # unpaced, the other two would be written by now; paced, they wait out the interval

    assert len(api.methods(*WRITES)) == 1
    assert store.lookup("u1") is None


def test_sticker_matches_earlier_releases(make_creator, api, caplog):
    store, creator = make_creator(legacy_rows(1))
    creator.start()
    uid = "slave.channel_alice"
    creator.request(uid, avatar)
    wait_until(settled(store, caplog, [uid]))

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


def test_set_reported_full_moves_to_the_next_set(make_creator, api, profile, caplog):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = [f"s{i}" for i in range(150)]
    api.full_on_add.add(SET_6)
    store, creator = make_creator(legacy_rows(1))
    creator.start()
    creator.request("bob", avatar)
    wait_until(settled(store, caplog, ["bob"]))

    assert saved_rows(directory)["member-user:bob"] == (api.sets[SET_7][0], SET_7)
    assert api.methods() == [("getStickerSet", SET_6), ("addStickerToSet", SET_6), ("getStickerSet", SET_7),
                             ("createNewStickerSet", SET_7), ("getStickerSet", SET_7)]


def test_failed_look_up_of_the_next_set_does_not_skip_it(make_creator, api, profile, caplog):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = [f"s{i}" for i in range(199)]
    store, creator = make_creator(legacy_rows(1))
    creator.start()
    creator.request("ann", avatar)
    wait_until(settled(store, caplog, ["ann"]))
    api.errors["getStickerSet"] = "Bad Request: temporarily unavailable"
    creator.request("ben", avatar)
    wait_until(settled(store, caplog, ["ben"]))
    del api.errors["getStickerSet"]
    creator.request("cid", avatar)
    wait_until(settled(store, caplog, ["cid"]))

    rows = saved_rows(directory)
    assert store.lookup("ben") is None
    assert (rows["member-user:ann"][1], rows["member-user:cid"][1]) == (SET_6, SET_7)


def test_lost_reply_to_set_creation_does_not_block_later_authors(make_creator, api, profile, caplog):  # noqa: F811
    directory, _ = profile
    api.lose_reply.add("createNewStickerSet")
    store, creator = make_creator(legacy_rows(1))
    creator.start()
    creator.request("ivy", avatar)
    wait_until(settled(store, caplog, ["ivy"]))
    creator.request("jon", avatar)
    wait_until(settled(store, caplog, ["jon"]))

    assert store.lookup("ivy") is None
    assert saved_rows(directory)["member-user:jon"] == (api.sets[SET_6][1], SET_6)
    assert api.methods() == [("getStickerSet", SET_6), ("createNewStickerSet", SET_6), ("getStickerSet", SET_6),
                             ("addStickerToSet", SET_6), ("getStickerSet", SET_6)]


def test_failed_author_waits_for_its_retry_time(make_creator, api, caplog, monkeypatch):
    clock = types.SimpleNamespace(now=1000.0, monotonic=lambda: clock.now)
    monkeypatch.setattr(custom_emoji, "time", clock)
    store, creator = make_creator(legacy_rows(1), retry_base=300.0)
    creator.start()
    attempts = []

    def no_avatar():
        attempts.append(1)
        raise EFBOperationNotSupported()

    creator.request("nobody", no_avatar)
    wait_until(settled(store, caplog, ["nobody"]))
    for _ in range(5):
        creator.request("nobody", no_avatar)
    time.sleep(0.1)  # a wrongly accepted request would be attempted by now
    assert len(attempts) == 1

    clock.now += 301
    creator.request("nobody", no_avatar)
    wait_until(lambda: len(attempts) == 2 and failures(caplog, "nobody") == 2)
    clock.now += 301  # the second failure doubles the wait
    creator.request("nobody", no_avatar)
    time.sleep(0.1)
    assert len(attempts) == 2
    assert api.methods(*WRITES) == []


def test_failed_read_back_after_the_upload_is_retried_without_uploading_again(
        make_creator, api, profile, caplog):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = ["s0"]
    answer = api.answer
    read_back_failed = []

    def failing_read_back(method, params, files):
        if method == "getStickerSet" and (api.methods("addStickerToSet") and not read_back_failed):
            read_back_failed.append(1)
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: temporarily unavailable"}
        return answer(method, params, files)

    api.answer = failing_read_back
    store, creator = make_creator(legacy_rows(1), retry_base=0.1)
    creator.start()
    creator.request("kim", avatar)
    wait_until(settled(store, caplog, ["kim"]))
    assert store.lookup("kim") is None
    api.errors["addStickerToSet"] = "Bad Request: unexpected failure"  # another author's write leaves the set state unclear
    creator.request("lee", avatar)
    wait_until(settled(store, caplog, ["lee"]))
    del api.errors["addStickerToSet"]
    time.sleep(0.15)
    creator.request("kim", avatar)
    wait_until(lambda: store.lookup("kim") is not None)

    assert saved_rows(directory)["member-user:kim"] == (api.sets[SET_6][-1], SET_6)
    assert api.methods(*WRITES) == [("addStickerToSet", SET_6)] * 2  # kim's, and lee's that failed
    assert len(api.files) == 2


def test_retried_read_back_saves_this_authors_sticker_not_a_later_one(
        make_creator, api, profile, caplog):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = ["s0"]
    answer = api.answer
    read_back_failed = []

    def failing_first_read_back(method, params, files):
        if method == "getStickerSet" and api.methods("addStickerToSet") and not read_back_failed:
            read_back_failed.append(1)
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: temporarily unavailable"}
        return answer(method, params, files)

    api.answer = failing_first_read_back
    store, creator = make_creator(legacy_rows(1), retry_base=0.1)
    creator.start()
    creator.request("kim", avatar)
    wait_until(settled(store, caplog, ["kim"]))
    creator.request("lee", avatar)
    wait_until(lambda: store.lookup("lee") is not None)
    time.sleep(0.15)
    creator.request("kim", avatar)
    wait_until(lambda: store.lookup("kim") is not None)

    rows = saved_rows(directory)
    kim, lee = (rows["member-user:" + uid][0] for uid in ("kim", "lee"))
    assert api.sets[SET_6][1:] == [kim, lee]
    assert len(api.files) == 2


def test_telegram_error_fails_only_that_author(make_creator, api, caplog):
    store, creator = make_creator(legacy_rows(1))
    creator.start()
    api.errors["addStickerToSet"] = "Bad Request: STICKER_PNG_NOPNG"
    api.sets[SET_6] = ["s0"]
    creator.request("carol", avatar)
    wait_until(settled(store, caplog, ["carol"]))
    creator.request("carol", avatar)
    time.sleep(0.1)

    assert store.lookup("carol") is None
    assert creator.feature_state is FeatureState.ENABLED
    assert api.methods(*WRITES) == [("addStickerToSet", SET_6)]


def test_permission_error_makes_the_feature_unavailable(make_creator, api, caplog):
    store, creator = make_creator(legacy_rows(1))
    creator.start()
    api.errors["createNewStickerSet"] = "Bad Request: PEER_ID_INVALID"
    with caplog.at_level(logging.WARNING, logger="efb_telegram_master.custom_emoji"):
        creator.request("dave", avatar)
        wait_until(lambda: creator.feature_state is FeatureState.UNAVAILABLE)
        calls = len(api.calls)
        loaded = []
        for uid in ("erin", "frank", "dave"):
            creator.request(uid, lambda: loaded.append(1) or avatar())
        time.sleep(0.1)

    assert creator.feature_state is FeatureState.UNAVAILABLE
    (warning,) = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert "peer_id_invalid" in warning.getMessage().lower()
    assert (len(api.calls), loaded) == (calls, [])
    assert store.lookup("erin") is None


def test_full_queue_drops_without_waiting(make_creator, api):
    store, creator = make_creator(legacy_rows(1), queue_size=3)  # worker not started: nothing leaves the queue
    for uid in ("a", "b", "c"):
        creator.request(uid, avatar)
    for i in range(200):
        creator.request(f"late{i}", avatar)

    assert api.calls == []

    creator.start()
    wait_until(lambda: all(store.lookup(uid) is not None for uid in ("a", "b", "c")))
    time.sleep(0.1)
    assert store.lookup("late0") is None
    assert len(api.methods(*WRITES)) == 3


def test_stop_returns_within_its_bound_with_jobs_queued(make_creator, api):
    store, creator = make_creator(legacy_rows(1), write_interval=30.0)
    api.sets[SET_6] = ["s0"]
    creator.start()
    uids = [f"u{i}" for i in range(5)]
    for uid in uids:
        creator.request(uid, avatar)
    wait_until(lambda: store.lookup("u0") is not None)
    creator.stop()

    assert not creator._thread.is_alive()  # stop interrupts the 30 s wait instead of leaving the worker behind


def test_upload_in_flight_when_stopping_is_still_saved(make_creator, api, profile):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = ["s0"]
    answer = api.answer
    uploading = threading.Event()

    def slow_answer(method, params, files):
        if method == "addStickerToSet":
            uploading.set()
            time.sleep(0.3)
        return answer(method, params, files)

    api.answer = slow_answer
    _, creator = make_creator(legacy_rows(1))
    creator.start()
    creator.request("zed", avatar)
    assert uploading.wait(10)
    creator.stop()

    assert saved_rows(directory)["member-user:zed"] == ("9001", SET_6)


def test_retried_read_back_of_a_filled_set_does_not_change_the_current_set(
        make_creator, api, profile, caplog):  # noqa: F811
    directory, _ = profile
    api.sets[SET_6] = [f"s{i}" for i in range(198)]
    answer = api.answer
    read_back_failed = []

    def failing_first_read_back(method, params, files):
        if method == "getStickerSet" and api.methods("addStickerToSet") and not read_back_failed:
            read_back_failed.append(1)
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: temporarily unavailable"}
        return answer(method, params, files)

    api.answer = failing_first_read_back
    store, creator = make_creator(legacy_rows(1), retry_base=0.1)
    creator.start()
    for uid in ("kim", "lee", "max"):  # kim and lee fill set 6, max starts set 7
        creator.request(uid, avatar)
        wait_until(settled(store, caplog, [uid]))
    time.sleep(0.15)
    creator.request("kim", avatar)
    wait_until(lambda: store.lookup("kim") is not None)
    creator.request("zed", avatar)
    wait_until(lambda: store.lookup("zed") is not None)

    rows = saved_rows(directory)
    assert [rows["member-user:" + uid][1] for uid in ("kim", "lee", "max", "zed")] == [SET_6, SET_6, SET_7, SET_7]
    assert len(api.sets[SET_7]) == 2
