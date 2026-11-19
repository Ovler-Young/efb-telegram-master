import hashlib
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from efb_telegram_master import db as db_module
from efb_telegram_master.custom_emoji import (
    CustomEmojiConfig,
    CustomEmojiStore,
    fallback_emoji,
    user_emoji_set_name,
)
from efb_telegram_master.db import DatabaseManager, database

TOPIC_DDL = (
    'CREATE TABLE "topiciconcache" ("avatar_hash" TEXT NOT NULL PRIMARY KEY, "custom_emoji_id" TEXT NOT NULL, '
    '"sticker_set_name" TEXT NOT NULL, "created_at" DATETIME NOT NULL, "updated_at" DATETIME NOT NULL)'
)
USER_DDL = TOPIC_DDL.replace("topiciconcache", "useremojicache").replace("avatar_hash", "cache_key")
H1, H2, H3, H4 = (hashlib.sha256(name.encode()).hexdigest() for name in ("png-1", "png-2", "png-3", "png-4"))

TOPIC_ROWS = [
    (H1, "t-bare", "ovler_etm_topic_icons_by_bot", "2026-06-01 10:00:00.000000", "2026-06-01 10:00:00.000000"),
    ("member:alice:" + H1, "t-alice-old", "set_a", "2026-06-02 10:00:00", "2026-06-02 10:00:00"),
    ("member:alice:" + H2, "t-alice-new", "set_a", "2026-06-03 10:00:00", "2026-06-03 10:00:00"),
    ("member-user:bob", "t-bob", "set_b", "2026-06-20 10:00:00", "2026-06-20 10:00:00"),
    ("member-user:carol", "t-carol", "set_b", "2026-06-20 11:00:00", "2026-06-20 11:00:00"),
    ("member-placeholder:set_b:7", "t-placeholder", "set_b", "2026-06-20 12:00:00", "2026-06-20 12:00:00"),
    ("something-else", "t-unknown", "set_b", "2026-06-20 12:00:00", "2026-06-20 12:00:00"),
]
USER_ROWS = [
    ("member-user:carol", "u-carol", "etm_user_avatars_by_bot", "2026-06-23 10:00:00.5", "2026-06-23 10:00:00.5"),
    ("member-user:dave:" + H3, "u-dave-old", "etm_user_avatars_by_bot", "2026-06-23 10:00:00", "2026-06-23 10:00:00"),
    ("member-user:dave:" + H4, "u-dave-new", "etm_user_avatars_by_bot", "2026-06-24 10:00:00", "2026-06-24 10:00:00"),
    ("member-user:erin:with:colons", "u-erin", "etm_user_avatars_by_bot", "2026-06-23 10:00:00", "2026-06-23 10:00:00"),
    ("member-user:alice:" + H1, "u-alice-hashed", "etm_user_avatars_by_bot", "2026-06-25 10:00:00", "2026-06-25 10:00:00"),
]


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """Open a real profile directory; the database is closed and the proxy restored afterwards."""
    directory = tmp_path / "channel"
    directory.mkdir()
    previous = database.obj
    managers = []
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _: directory)

    def open_manager():
        manager = DatabaseManager(SimpleNamespace(channel_id="test.emoji", config={"database": {}}))
        managers.append(manager)
        return manager

    yield directory, open_manager
    for manager in reversed(managers):
        manager.stop_worker()
    database.initialize(previous)


def write_legacy_tables(directory, topic_rows=None, user_rows=None):
    with closing(sqlite3.connect(directory / "tgdata.db")) as connection, connection:
        for ddl, rows, table in ((TOPIC_DDL, topic_rows, "topiciconcache"), (USER_DDL, user_rows, "useremojicache")):
            if rows is not None:
                connection.execute(ddl)
                connection.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?)", rows)


def dump_tables(directory):
    with closing(sqlite3.connect(directory / "tgdata.db")) as connection:
        return {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("topiciconcache", "useremojicache")}


def table_names(directory):
    with closing(sqlite3.connect(directory / "tgdata.db")) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_saved_rows_of_every_old_key_format_resolve_in_priority_order(profile, caplog):
    directory, open_manager = profile
    write_legacy_tables(directory, TOPIC_ROWS, USER_ROWS)
    before = dump_tables(directory)
    config = CustomEmojiConfig.from_channel_config({"user_emoji": {"custom_emoji_ids": {"bob": "configured-bob"}}})
    store = CustomEmojiStore(open_manager(), config)
    with caplog.at_level("INFO", logger="efb_telegram_master.custom_emoji"):
        store.load()

    expected = {
        "bob": ("configured-bob", "config"),  # configuration beats a saved row
        "carol": ("u-carol", "useremojicache"),  # useremojicache beats topiciconcache
        "alice": ("u-alice-hashed", "avatar-hash"),  # newest hashed row across both tables
        "dave": ("u-dave-new", "avatar-hash"),
        "erin:with:colons": ("u-erin", "useremojicache"),  # a uid with colons but no hash suffix
    }
    for uid, (emoji_id, source) in expected.items():
        found = store.lookup(uid)
        assert (found.custom_emoji_id, found.source) == (emoji_id, source), uid
        assert found.fallback == fallback_emoji(uid)
    assert store.topic_icon_for_avatar_hash(H1.upper()) == "t-bare"
    assert store.topic_icon_for_avatar_hash(H2) is None
    assert "bare-hash=1" in caplog.text and "member-placeholder=1" in caplog.text and "unknown=1" in caplog.text
    assert dump_tables(directory) == before


def test_user_row_in_topiciconcache_is_used_when_useremojicache_has_none(profile):
    directory, open_manager = profile
    write_legacy_tables(directory, TOPIC_ROWS, [])
    store = CustomEmojiStore(open_manager(), CustomEmojiConfig())
    store.load()
    assert store.lookup("bob").custom_emoji_id == "t-bob"
    assert store.lookup("alice").custom_emoji_id == "t-alice-new"


@pytest.mark.parametrize("kind", ["only-topic", "only-user", "neither"])
def test_profiles_with_one_or_no_legacy_table_load_and_get_both_tables(profile, kind):
    directory, open_manager = profile
    write_legacy_tables(directory,
                        TOPIC_ROWS if kind == "only-topic" else None, USER_ROWS if kind == "only-user" else None)
    store = CustomEmojiStore(open_manager(), CustomEmojiConfig())
    store.load()
    assert {"topiciconcache", "useremojicache"} <= table_names(directory)
    assert (store.lookup("bob") is not None) == (kind == "only-topic")
    assert (store.lookup("carol") is not None) == (kind != "neither")


def test_timestamps_in_a_timestamp_typed_column_order_hashed_rows(profile):
    directory, open_manager = profile
    ddl = USER_DDL.replace("DATETIME", "TIMESTAMP")
    with closing(sqlite3.connect(directory / "tgdata.db")) as connection, connection:
        connection.execute(ddl)
        connection.executemany("INSERT INTO useremojicache VALUES (?, ?, ?, ?, ?)", [
            ("member:zed:" + H1, "new", "s", "2026-06-24 10:00:00+00:00", "2026-06-24 10:00:00+00:00"),
            ("member:zed:" + H2, "old", "s", "2026-06-23 10:00:00+00:00", "2026-06-23 10:00:00+00:00"),
        ])
    store = CustomEmojiStore(open_manager(), CustomEmojiConfig())
    store.load()
    assert store.lookup("zed").custom_emoji_id == "new"


def test_new_emoji_is_recorded_as_a_member_user_row_and_found_by_lookup(profile):
    directory, open_manager = profile
    write_legacy_tables(directory, TOPIC_ROWS, USER_ROWS)
    before = dump_tables(directory)
    store = CustomEmojiStore(open_manager(), CustomEmojiConfig())
    store.load()

    assert store.lookup("frank") is None
    assert store.record("frank", "new-frank", "etm_user_avatars_2_by_bot").custom_emoji_id == "new-frank"
    assert store.lookup("frank").custom_emoji_id == "new-frank"
    after = dump_tables(directory)
    assert after["topiciconcache"] == before["topiciconcache"]
    assert next(row for row in after["useremojicache"] if row not in before["useremojicache"])[:3] == (
        "member-user:frank", "new-frank", "etm_user_avatars_2_by_bot")
    assert len(after["useremojicache"]) == len(before["useremojicache"]) + 1

    # An existing row is never rewritten: its id stays and is what lookup returns.
    assert store.record("carol", "different-id", "other_set").custom_emoji_id == "u-carol"
    assert dump_tables(directory)["useremojicache"] == after["useremojicache"]


def test_recorded_emoji_does_not_replace_a_configured_one(profile):
    directory, open_manager = profile
    write_legacy_tables(directory, [], [])
    config = CustomEmojiConfig.from_channel_config({"user_emoji": {"custom_emoji_ids": {"gina": "configured"}}})
    store = CustomEmojiStore(open_manager(), config)
    store.load()
    store.record("gina", "generated", "set")
    assert store.lookup("gina").custom_emoji_id == "configured"


def test_config_defaults_and_old_user_emoji_section():
    defaults = CustomEmojiConfig.from_channel_config({"admins": [42, 43]})
    assert (defaults.enabled, defaults.sticker_set_name, defaults.owner_user_id) == (True, "etm_user_avatars", 42)
    assert not defaults.custom_emoji_ids and not defaults.topic_icon_custom_emoji_ids and not defaults.auxiliary_tokens

    old = CustomEmojiConfig.from_channel_config({
        "admins": [42],
        "user_emoji": {"enabled": False, "sticker_set_name": "my_set", "owner_user_id": 7,
                       "custom_emoji_ids": {"alice": "e1", 12345: "e2", "bad": 5}},
        "topic_icons": {"sticker_set_name": "ignored", "custom_emoji_ids": {"chat": "t1"}},
        "auxiliary_bots": [{"token": "a", "custom_emoji": True}, {"token": "b"}, {"token": "c", "custom_emoji": "yes"}],
    })
    assert (old.enabled, old.sticker_set_name, old.owner_user_id) == (False, "my_set", 7)
    assert dict(old.custom_emoji_ids) == {"alice": "e1", "12345": "e2"}
    assert dict(old.topic_icon_custom_emoji_ids) == {"chat": "t1"}
    assert old.auxiliary_tokens == {"a"}


@pytest.mark.parametrize("base, index, username, expected", [
    ("etm_user_avatars", 1, "yuEFbot", "etm_user_avatars_by_yuEFbot"),
    ("etm_user_avatars", 6, "yuEFbot", "etm_user_avatars_6_by_yuEFbot"),
    ("etm_user_avatars_by_yuEFbot", 2, "yuEFbot", "etm_user_avatars_2_by_yuEFbot"),
    ("x" * 80, 12, "yuEFbot", "x" * (64 - len("_12_by_yuEFbot")) + "_12_by_yuEFbot"),
])
def test_set_name_follows_the_old_naming_rule(base, index, username, expected):
    name = user_emoji_set_name(base, index, username)
    assert name == expected and len(name) <= 64
