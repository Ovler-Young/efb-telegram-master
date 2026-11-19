"""Custom emoji for slave users: configuration, saved ids, and the in-memory lookup.

Earlier releases generated one custom emoji per slave user from the user's avatar
and saved its id in the ``useremojicache`` table (and, before that, in
``topiciconcache``). This module reads those rows once at startup into a dict, so
that sending a message needs only a dict lookup, never database or network work.
"""

import datetime
import hashlib
import logging
import re
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .db import DatabaseManager

logger = logging.getLogger(__name__)

DEFAULT_STICKER_SET_NAME = "etm_user_avatars"
MAX_STICKER_SET_NAME_LENGTH = 64

# One of these is stored as the sticker's emoji and shown as the fallback text.
FALLBACK_EMOJIS = ("😀", "🙂", "😄", "😊", "😎", "🤖", "💬", "📨", "📌", "⭐")

USER_KEY_PREFIX = "member-user:"
USER_AVATAR_KEY_PREFIX = "member:"
PLACEHOLDER_KEY_PREFIX = "member-placeholder:"

SOURCE_CONFIG = "config"
SOURCE_USER_CACHE = "useremojicache"
SOURCE_TOPIC_CACHE = "topiciconcache"
SOURCE_AVATAR_HASH = "avatar-hash"

USER_EMOJI_TABLE = "useremojicache"
TOPIC_ICON_TABLE = "topiciconcache"

_HASH_SUFFIX = re.compile(r"^(.+):[0-9A-Fa-f]{64}$")
_BARE_HASH = re.compile(r"^[0-9A-Fa-f]{64}$")

# Key formats written by earlier releases, as counted in the startup log.
KEY_BARE_HASH = "bare-hash"
KEY_MEMBER_HASH = "member:uid:hash"
KEY_USER = "member-user:uid"
KEY_USER_HASH = "member-user:uid:hash"
KEY_PLACEHOLDER = "member-placeholder"
KEY_UNKNOWN = "unknown"


def fallback_emoji(uid: str) -> str:
    """The emoji character stored with a user's sticker, chosen from the user id."""
    return FALLBACK_EMOJIS[hashlib.sha1(uid.encode("utf-8")).digest()[0] % len(FALLBACK_EMOJIS)]


def user_emoji_set_name(base_name: str, index: int, bot_username: str | None) -> str:
    """Sticker set name ``<base>[_<index>]_by_<bot username>`` (at most 64 characters).

    The index is omitted for the first set. A ``_by_<bot username>`` already
    present on ``base_name`` is not repeated.
    """
    suffix = f"_by_{bot_username}" if bot_username else ""
    if suffix and base_name.lower().endswith(suffix.lower()):
        base_name = base_name[:-len(suffix)]
    base_name = base_name.rstrip("_") or "etm"
    index_suffix = "" if index <= 1 else f"_{index}"
    max_base_len = MAX_STICKER_SET_NAME_LENGTH - len(index_suffix) - len(suffix)
    return f"{base_name[:max_base_len].rstrip('_')}{index_suffix}{suffix}"


def _id_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, str) and item}


@dataclass(frozen=True)
class CustomEmojiConfig:
    """The ``user_emoji`` section, the legacy ``topic_icons.custom_emoji_ids`` and per-bot opt-ins."""

    enabled: bool = True
    sticker_set_name: str = DEFAULT_STICKER_SET_NAME
    owner_user_id: int | None = None
    custom_emoji_ids: Mapping[str, str] = field(default_factory=dict)
    topic_icon_custom_emoji_ids: Mapping[str, str] = field(default_factory=dict)
    auxiliary_tokens: frozenset[str] = frozenset()
    """Tokens of the ``auxiliary_bots`` entries with ``custom_emoji: true``."""

    @classmethod
    def from_channel_config(cls, config: Mapping[str, Any]) -> 'CustomEmojiConfig':
        section = config.get("user_emoji")
        if not isinstance(section, Mapping):
            section = {}
        name = section.get("sticker_set_name", DEFAULT_STICKER_SET_NAME)
        if not isinstance(name, str) or not name:
            name = DEFAULT_STICKER_SET_NAME
        owner = section.get("owner_user_id")
        if not isinstance(owner, int) or isinstance(owner, bool):
            admins = config.get("admins")
            owner = admins[0] if isinstance(admins, list) and admins and isinstance(admins[0], int) else None
        topic_icons = config.get("topic_icons")
        auxiliary = config.get("auxiliary_bots")
        return cls(
            enabled=bool(section.get("enabled", True)),
            sticker_set_name=name,
            owner_user_id=owner,
            custom_emoji_ids=_id_map(section.get("custom_emoji_ids")),
            topic_icon_custom_emoji_ids=_id_map(
                topic_icons.get("custom_emoji_ids") if isinstance(topic_icons, Mapping) else None),
            auxiliary_tokens=frozenset(
                entry["token"] for entry in auxiliary if isinstance(entry, Mapping)
                and entry.get("custom_emoji") is True and isinstance(entry.get("token"), str)
            ) if isinstance(auxiliary, list) else frozenset(),
        )


@dataclass(frozen=True)
class UserEmoji:
    custom_emoji_id: str
    fallback: str
    source: str


def _timestamp(value: Any) -> float:
    """Seconds for a stored timestamp, whether the column returned a datetime or text; 0 if unreadable."""
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def classify_key(key: str) -> tuple[str, str | None, bool]:
    """Return ``(format, user uid, uid is saved without an avatar hash)`` for a cache key."""
    if key.startswith(USER_KEY_PREFIX):
        rest = key[len(USER_KEY_PREFIX):]
        hashed = _HASH_SUFFIX.match(rest)
        if hashed:
            return KEY_USER_HASH, hashed.group(1), False
        return (KEY_USER, rest, True) if rest else (KEY_UNKNOWN, None, False)
    if key.startswith(USER_AVATAR_KEY_PREFIX):
        hashed = _HASH_SUFFIX.match(key[len(USER_AVATAR_KEY_PREFIX):])
        return (KEY_MEMBER_HASH, hashed.group(1), False) if hashed else (KEY_UNKNOWN, None, False)
    if key.startswith(PLACEHOLDER_KEY_PREFIX):
        return KEY_PLACEHOLDER, None, False
    if _BARE_HASH.match(key):
        return KEY_BARE_HASH, None, False
    return KEY_UNKNOWN, None, False


class CustomEmojiStore:
    """Slave user id -> custom emoji, held in memory.

    ``lookup`` only reads a dict, so it is safe on the send path and while a
    background thread calls ``record``.
    """

    def __init__(self, db: 'DatabaseManager', config: CustomEmojiConfig):
        self._db = db
        self.config = config
        self._index: dict[str, tuple[str, str]] = {}  # uid -> (custom emoji id, source)
        self._topic_icons_by_hash: dict[str, str] = {}
        self._write_lock = threading.Lock()

    def load(self) -> None:
        """Read both legacy tables once and build the index. Call before the store is shared."""
        rows = self._db.read_custom_emoji_rows()
        user_rows = rows.get(USER_EMOJI_TABLE, [])
        topic_rows = rows.get(TOPIC_ICON_TABLE, [])
        counts: dict[str, Counter] = {USER_EMOJI_TABLE: Counter(), TOPIC_ICON_TABLE: Counter()}
        # uid -> (rank, -timestamp, custom emoji id, source); the smallest rank and newest row win.
        best: dict[str, tuple[int, float, str, str]] = {}
        topic_icons: dict[str, str] = {}
        for table, table_rows, direct_rank, direct_source in (
            (USER_EMOJI_TABLE, user_rows, 1, SOURCE_USER_CACHE),
            (TOPIC_ICON_TABLE, topic_rows, 2, SOURCE_TOPIC_CACHE),
        ):
            for key, emoji_id, updated_at in table_rows:
                key_format, uid, direct = classify_key(key)
                counts[table][key_format] += 1
                if key_format == KEY_BARE_HASH and table == TOPIC_ICON_TABLE and emoji_id:
                    topic_icons[key.lower()] = emoji_id
                if uid is None or not emoji_id:
                    continue
                current = best.get(uid)
                if direct:
                    candidate = (direct_rank, 0.0, emoji_id, direct_source)
                elif current is not None and current[0] < 3:
                    continue  # a saved row for the user itself wins; skip reading the timestamp
                else:
                    candidate = (3, -_timestamp(updated_at), emoji_id, SOURCE_AVATAR_HASH)
                if current is None or candidate[:2] < current[:2]:
                    best[uid] = candidate
        index = {uid: (emoji_id, source) for uid, (_, _, emoji_id, source) in best.items()}
        for uid, emoji_id in self.config.custom_emoji_ids.items():
            index[uid] = (emoji_id, SOURCE_CONFIG)
        self._index = index
        self._topic_icons_by_hash = topic_icons
        logger.info(
            "Custom emoji cache loaded: %s users (%s from configuration), %s",
            len(index), len(self.config.custom_emoji_ids),
            "; ".join(f"{table}: {len(table_rows)} rows ({self._format_counts(counts[table])})"
                      for table, table_rows in ((USER_EMOJI_TABLE, user_rows), (TOPIC_ICON_TABLE, topic_rows))),
        )

    @staticmethod
    def _format_counts(counts: Mapping[str, int]) -> str:
        return ", ".join(f"{name}={count}" for name, count in sorted(counts.items())) or "none"

    def lookup(self, uid: str) -> UserEmoji | None:
        entry = self._index.get(uid)
        return UserEmoji(entry[0], fallback_emoji(uid), entry[1]) if entry else None

    def topic_icon_for_avatar_hash(self, avatar_hash: str) -> str | None:
        """Custom emoji id saved by an earlier release for an avatar image hash, if any."""
        return self._topic_icons_by_hash.get(avatar_hash.lower())

    def record(self, uid: str, custom_emoji_id: str, sticker_set_name: str) -> UserEmoji:
        """Save a new emoji for ``uid`` as ``member-user:<uid>`` and make it available to ``lookup``.

        A row that already exists for that key is kept as it is, and its id is the one used.
        """
        with self._write_lock:
            stored_id = self._db.add_user_emoji_cache(USER_KEY_PREFIX + uid, custom_emoji_id, sticker_set_name)
            if stored_id != custom_emoji_id:
                logger.warning("Custom emoji row for user %s already exists; keeping the saved one.", uid)
            current = self._index.get(uid)
            if current is None or current[1] != SOURCE_CONFIG:
                self._index[uid] = (stored_id, SOURCE_USER_CACHE)
            return self.lookup(uid)  # type: ignore[return-value]
