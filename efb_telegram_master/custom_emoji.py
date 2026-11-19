"""Custom emoji for slave users: configuration, saved ids, and the in-memory lookup.

Earlier releases generated one custom emoji per slave user from the user's avatar
and saved its id in the ``useremojicache`` table (and, before that, in
``topiciconcache``). This module reads those rows once at startup into a dict, so
that sending a message needs only a dict lookup, never database or network work.
Emoji for new users are created on one background thread (``UserEmojiCreator``).
"""

import asyncio
import datetime
import hashlib
import html
import io
import logging
import re
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Coroutine, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, BinaryIO, Generic, NoReturn, TypeVar

import telegram
from ehforwarderbot.exceptions import EFBOperationNotSupported
from PIL import Image
from telegram import InputSticker, Sticker
from telegram.error import (
    BadRequest,
    Forbidden,
    InvalidToken,
    RetryAfter,
    TelegramError,
)

if TYPE_CHECKING:
    from .db import DatabaseManager

logger = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_STICKER_SET_NAME = "etm_user_avatars"
MAX_STICKER_SET_NAME_LENGTH = 64

# One of these is stored as the sticker's emoji and shown as the fallback text.
FALLBACK_EMOJIS = ("😀", "🙂", "😄", "😊", "😎", "🤖", "💬", "📨", "📌", "⭐")

USER_KEY_PREFIX = "member-user:"
USER_AVATAR_KEY_PREFIX = "member:"
PLACEHOLDER_KEY_PREFIX = "member-placeholder:"

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
    """The ``user_emoji`` section and per-bot opt-ins."""

    enabled: bool = True
    sticker_set_name: str = DEFAULT_STICKER_SET_NAME
    owner_user_id: int | None = None
    custom_emoji_ids: Mapping[str, str] = field(default_factory=dict)
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
        auxiliary = config.get("auxiliary_bots")
        return cls(
            enabled=bool(section.get("enabled", True)),
            sticker_set_name=name,
            owner_user_id=owner,
            custom_emoji_ids=_id_map(section.get("custom_emoji_ids")),
            auxiliary_tokens=frozenset(
                entry["token"] for entry in auxiliary if isinstance(entry, Mapping)
                and entry.get("custom_emoji") is True and isinstance(entry.get("token"), str)
            ) if isinstance(auxiliary, list) else frozenset(),
        )


@dataclass(frozen=True)
class UserEmoji:
    custom_emoji_id: str
    fallback: str


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
        self._index: dict[str, str] = {}  # uid -> custom emoji id
        self.sticker_set_names: frozenset[str] = frozenset()
        """Sticker set names found in the saved rows."""
        self._write_lock = threading.Lock()

    def load(self) -> None:
        """Read both legacy tables once and build the index. Call before the store is shared."""
        rows = self._db.read_custom_emoji_rows()
        user_rows = rows.get(USER_EMOJI_TABLE, [])
        topic_rows = rows.get(TOPIC_ICON_TABLE, [])
        counts: dict[str, Counter] = {USER_EMOJI_TABLE: Counter(), TOPIC_ICON_TABLE: Counter()}
        # uid -> (rank, -timestamp, custom emoji id); the smallest rank and newest row win.
        best: dict[str, tuple[int, float, str]] = {}
        set_names: set[str] = set()
        for table, table_rows, direct_rank in ((USER_EMOJI_TABLE, user_rows, 1), (TOPIC_ICON_TABLE, topic_rows, 2)):
            for key, emoji_id, updated_at, set_name in table_rows:
                key_format, uid, direct = classify_key(key)
                if set_name:
                    set_names.add(set_name)
                counts[table][key_format] += 1
                if uid is None or not emoji_id:
                    continue
                current = best.get(uid)
                if direct:
                    candidate = (direct_rank, 0.0, emoji_id)
                elif current is not None and current[0] < 3:
                    continue  # a saved row for the user itself wins; skip reading the timestamp
                else:
                    candidate = (3, -_timestamp(updated_at), emoji_id)
                if current is None or candidate[:2] < current[:2]:
                    best[uid] = candidate
        # Turned off, only the ids written in the configuration are used.
        index = {uid: emoji_id for uid, (_, _, emoji_id) in best.items()} if self.config.enabled else {}
        index.update(self.config.custom_emoji_ids)
        self._index = index
        self.sticker_set_names = frozenset(set_names)
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
        emoji_id = self._index.get(uid)
        return UserEmoji(emoji_id, fallback_emoji(uid)) if emoji_id else None

    def record(self, uid: str, custom_emoji_id: str, sticker_set_name: str) -> UserEmoji:
        """Save a new emoji for ``uid`` as ``member-user:<uid>`` and make it available to ``lookup``.

        A row that already exists for that key is kept as it is, and its id is the one used.
        """
        with self._write_lock:
            stored_id = self._db.add_user_emoji_cache(USER_KEY_PREFIX + uid, custom_emoji_id, sticker_set_name)
            if stored_id != custom_emoji_id:
                logger.warning("Custom emoji row for user %s already exists; keeping the saved one.", uid)
            if uid not in self.config.custom_emoji_ids:
                self._index[uid] = stored_id
            return self.lookup(uid)  # type: ignore[return-value]


_EMOJI_TAG = re.compile(
    r'<tg-emoji emoji-id="[^"<>]*">(' + "|".join(re.escape(emoji) for emoji in FALLBACK_EMOJIS) + r")</tg-emoji>"
)


class AuthorEmojiHeader(str):
    """A group message header that names an author whose custom emoji is known.

    As text it is the plain header, with the author's fallback emoji where the
    custom emoji goes, so every place that uses the header as text keeps working.
    An HTML send asks ``html_with_emoji`` for the header with the custom emoji tag.
    """

    emoji: UserEmoji
    offset: int

    def __new__(cls, text: str, emoji: UserEmoji, offset: int):
        header = super().__new__(cls, text)
        header.emoji = emoji
        header.offset = offset
        return header

    def html_with_emoji(self) -> str:
        """The header as HTML: text escaped, the fallback emoji replaced by its custom emoji tag."""
        end = self.offset + len(self.emoji.fallback)
        tag = f'<tg-emoji emoji-id="{html.escape(self.emoji.custom_emoji_id)}">{self.emoji.fallback}</tg-emoji>'
        return html.escape(self[:self.offset]) + tag + html.escape(self[end:])


def strip_custom_emoji_tag(content: str) -> str:
    """Replace the custom emoji tag of a message header with its fallback emoji.

    The header is the first line of a queued message, and it holds at most one
    tag, so only the first tag of the first line is replaced. Message text
    comes after the first line and is escaped when it is queued, so what the
    user wrote is never changed.
    """
    first_line, newline, rest = content.partition("\n")
    return _EMOJI_TAG.sub(r"\1", first_line, count=1) + newline + rest


# State machines for generated user emoji. Every state change goes through
# ``StateMachine.next_state``; an event the table does not allow raises
# ``IllegalTransition`` naming the machine, the identity and the event.

S = TypeVar("S", bound=Enum)
E = TypeVar("E", bound=Enum)


class IllegalTransition(RuntimeError):
    """An event the machine does not allow in the current state."""

    def __init__(self, machine: str, identity: str, state: Enum, event: Enum):
        super().__init__(f"{machine} {identity!r}: event {event.value!r} is not allowed in state {state.value!r}")
        self.machine = machine
        self.identity = identity
        self.state = state
        self.event = event


class StateMachine(Generic[S, E]):
    def __init__(self, name: str, transitions: Mapping[tuple[S, E], S]):
        self.name = name
        self.transitions = dict(transitions)

    def next_state(self, identity: str, state: S, event: E) -> S:
        try:
            return self.transitions[(state, event)]
        except KeyError:
            raise IllegalTransition(self.name, identity, state, event) from None


class AuthorEmojiState(Enum):
    ABSENT = "absent"
    QUEUED = "queued"
    CREATING = "creating"
    READY = "ready"
    """Saved: loaded from a row at startup, or created and recorded in this session."""
    FAILED = "failed"
    """Creation failed; a new request is accepted only after the author's retry time."""


class AuthorEmojiEvent(Enum):
    REQUESTED = "requested"
    STARTED = "started"
    CREATED = "created"
    FAILED = "failed"
    DISCARDED = "discarded"
    """The feature became unavailable before the queued job ran."""


AUTHOR_EMOJI_MACHINE: StateMachine[AuthorEmojiState, AuthorEmojiEvent] = StateMachine("author emoji", {
    (AuthorEmojiState.ABSENT, AuthorEmojiEvent.REQUESTED): AuthorEmojiState.QUEUED,
    (AuthorEmojiState.FAILED, AuthorEmojiEvent.REQUESTED): AuthorEmojiState.QUEUED,
    (AuthorEmojiState.QUEUED, AuthorEmojiEvent.STARTED): AuthorEmojiState.CREATING,
    (AuthorEmojiState.QUEUED, AuthorEmojiEvent.DISCARDED): AuthorEmojiState.ABSENT,
    (AuthorEmojiState.CREATING, AuthorEmojiEvent.CREATED): AuthorEmojiState.READY,
    (AuthorEmojiState.CREATING, AuthorEmojiEvent.FAILED): AuthorEmojiState.FAILED,
})


class FeatureState(Enum):
    ENABLED = "enabled"
    DISABLED_BY_CONFIG = "disabled-by-config"
    UNAVAILABLE = "unavailable"
    """Telegram refused the bot, or saving keeps failing; no more emoji are created this session."""


class FeatureEvent(Enum):
    FAILED = "failed"


FEATURE_MACHINE: StateMachine[FeatureState, FeatureEvent] = StateMachine("user emoji feature", {
    (FeatureState.ENABLED, FeatureEvent.FAILED): FeatureState.UNAVAILABLE,
})


class StickerSetState(Enum):
    UNKNOWN = "unknown"
    """The current set has not been looked up in this session, or a write to it failed for an unclear reason."""
    OPEN = "open"
    """The current set has room, or does not exist yet and is created by the next upload."""
    FULL = "full"
    ERROR = "error"


class StickerSetEvent(Enum):
    OPENED = "opened"
    ADDED = "added"
    FILLED = "filled"
    WRITE_FAILED = "write failed"
    """A write failed for a reason other than a full set; the set may exist or be gone despite the cached view."""
    BROKEN = "broken"


STICKER_SET_MACHINE: StateMachine[StickerSetState, StickerSetEvent] = StateMachine("sticker set allocator", {
    (StickerSetState.UNKNOWN, StickerSetEvent.OPENED): StickerSetState.OPEN,
    (StickerSetState.UNKNOWN, StickerSetEvent.FILLED): StickerSetState.FULL,
    (StickerSetState.UNKNOWN, StickerSetEvent.BROKEN): StickerSetState.ERROR,
    (StickerSetState.OPEN, StickerSetEvent.ADDED): StickerSetState.OPEN,
    (StickerSetState.OPEN, StickerSetEvent.FILLED): StickerSetState.FULL,
    (StickerSetState.OPEN, StickerSetEvent.WRITE_FAILED): StickerSetState.UNKNOWN,
    (StickerSetState.OPEN, StickerSetEvent.BROKEN): StickerSetState.ERROR,
    (StickerSetState.FULL, StickerSetEvent.OPENED): StickerSetState.OPEN,
    (StickerSetState.FULL, StickerSetEvent.FILLED): StickerSetState.FULL,
    (StickerSetState.FULL, StickerSetEvent.BROKEN): StickerSetState.ERROR,
})


STICKER_SET_LIMIT = 200
QUEUE_SIZE = 100
WRITE_INTERVAL = 1.0
RETRY_BASE = 600.0
RETRY_CAP = 12 * 3600.0
SAVE_FAILURE_LIMIT = 3
JOIN_TIMEOUT = 5.0
MAX_STICKER_SET_INDEX = 999
CUSTOM_EMOJI_SIZE = 100
_SET_FULL_MARKERS = ("stickers_too_much", "too many stickers")
_SET_MISSING_MARKERS = ("stickerset_invalid",)
# Errors that mean this bot cannot create emoji for the configured owner at all.
_REFUSAL_MARKERS = ("peer_id_invalid", "user_id_invalid", "user_is_bot", "not enough rights")


def _error_text(error: TelegramError) -> str:
    return error.message.lower()


def highest_set_index(set_names: Iterable[str], base_name: str, bot_username: str | None) -> int:
    """The largest ``n`` such that ``user_emoji_set_name(base_name, n, bot_username)`` is in ``set_names``; 1 if none."""
    highest = 1
    for name in set_names:
        match = re.search(r"_(\d+)_by_[^_]", name) if bot_username else re.search(r"_(\d+)$", name)
        if match:
            index = int(match.group(1))
            if index > highest and user_emoji_set_name(base_name, index, bot_username).lower() == name.lower():
                highest = index
    return highest


def make_avatar_png(picture: BinaryIO) -> io.BytesIO:
    """Crop the picture to a centred square and scale it to a 100x100 PNG."""
    picture.seek(0)
    image = Image.open(picture).convert("RGBA")
    side = min(image.size)
    left = (image.size[0] - side) // 2
    top = (image.size[1] - side) // 2
    image = image.crop((left, top, left + side, top + side))
    image = image.resize((CUSTOM_EMOJI_SIZE, CUSTOM_EMOJI_SIZE), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    image.save(out, "PNG")
    out.seek(0)
    return out


@dataclass
class _Author:
    state: AuthorEmojiState = AuthorEmojiState.ABSENT
    failures: int = 0
    retry_at: float = 0.0
    load_avatar: Callable[[], BinaryIO] | None = None
    unsaved: tuple[str, str] | None = None
    """(custom emoji id, set name) uploaded to Telegram but not saved yet."""


class _AuthorFailed(Exception):
    def __init__(self, reason: str, retry_after: float = 0.0):
        super().__init__(reason)
        self.retry_after = retry_after


class UserEmojiCreator:
    """Creates custom emoji from slave users' avatars on one background thread.

    ``request`` is cheap enough for the send path: it reads only memory and
    never blocks. Avatar fetches, image work, Telegram calls and saving happen
    on the worker thread, which uses its own bot and connection pool so it
    never takes connections from message sending.
    """

    def __init__(
        self,
        store: CustomEmojiStore,
        bot_factory: Callable[[], telegram.Bot],
        bot_username: str | None,
        title: str,
    ):
        self._store = store
        self._bot_factory = bot_factory
        self._bot_username = bot_username
        self._title = title
        config = store.config
        self.feature_state = FeatureState.ENABLED if config.enabled else FeatureState.DISABLED_BY_CONFIG
        self.unavailable_reason: str | None = None
        self.set_state = StickerSetState.UNKNOWN
        self.set_index = highest_set_index(store.sticker_set_names, config.sticker_set_name, bot_username)
        self._set_count = 0
        self._set_exists = False
        self._authors: dict[str, _Author] = {}
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._stopping = threading.Event()
        self._left_behind = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bot: telegram.Bot | None = None
        self._next_write_at = 0.0
        self._save_failures = 0

    # -- send path ---------------------------------------------------------

    def request(self, uid: str, load_avatar: Callable[[], BinaryIO]) -> None:
        """Queue creation of ``uid``'s emoji unless it exists, is pending, or failed recently.

        ``load_avatar`` runs later on the worker thread and returns the
        avatar image. Drops the request when the queue is full.
        """
        if self.feature_state is not FeatureState.ENABLED or self._stopping.is_set():
            return
        if self._store.lookup(uid) is not None:
            return
        try:
            with self._lock:
                author = self._authors.get(uid)
                if author is None:
                    author = _Author()
                elif author.state is not AuthorEmojiState.ABSENT and not (
                        author.state is AuthorEmojiState.FAILED and time.monotonic() >= author.retry_at):
                    return
                if len(self._queue) >= QUEUE_SIZE:
                    logger.debug("Custom emoji queue is full; not creating an emoji for %s now.", uid)
                    return
                self._authors[uid] = author
                self._move_author(uid, author, AuthorEmojiEvent.REQUESTED)
                author.load_avatar = load_avatar
                self._queue.append(uid)
                self._wakeup.notify()
        except IllegalTransition as error:
            logger.warning("Custom emoji request for %s ignored: %s", uid, error)

    def author_state(self, uid: str) -> AuthorEmojiState:
        if self._store.lookup(uid) is not None:
            return AuthorEmojiState.READY
        author = self._authors.get(uid)
        return author.state if author else AuthorEmojiState.ABSENT

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self.feature_state is not FeatureState.ENABLED or self._thread is not None:
            return
        if self._store.config.owner_user_id is None:
            self._make_unavailable("no sticker set owner: set user_emoji.owner_user_id or admins")
            return
        self._thread = threading.Thread(target=self._run, name="ETM custom emoji creator", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the worker and wait for it for a bounded time; queued requests are dropped."""
        with self._lock:
            self._stopping.set()
            self._queue.clear()
            self._authors.clear()
            self._wakeup.notify_all()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(JOIN_TIMEOUT)
        if thread.is_alive():
            self._left_behind = True
            logger.warning("Custom emoji worker did not stop within %.1f s; leaving it behind.", JOIN_TIMEOUT)

    # -- worker ------------------------------------------------------------

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._bot = self._bot_factory()
            while True:
                with self._lock:
                    while not self._queue and not self._stopping.is_set():
                        self._wakeup.wait()
                    if self._stopping.is_set():
                        return
                    uid = self._queue.popleft()
                    author = self._authors[uid]
                    self._move_author(uid, author, AuthorEmojiEvent.STARTED)
                try:
                    self._create(uid, author)
                except IllegalTransition as error:
                    logger.warning("Custom emoji job for %s stopped: %s", uid, error)
        except Exception:
            logger.warning("Custom emoji worker stopped after an unexpected error.", exc_info=True)
        finally:
            if self._bot is not None:
                with suppress(Exception):
                    self._loop.run_until_complete(self._bot.request.shutdown())
            self._loop.close()

    def _create(self, uid: str, author: _Author) -> None:
        try:
            if author.unsaved is None:
                load_avatar, author.load_avatar = author.load_avatar, None
                if load_avatar is None:
                    raise _AuthorFailed("no avatar source")
                png = self._avatar_png(load_avatar)
                author.unsaved = self._upload(uid, png)
            self._save(uid, author)
        except _AuthorFailed as failure:
            self._fail(uid, author, str(failure), failure.retry_after)
        except Exception as error:
            logger.warning("Failed to create a custom emoji for %s.", uid, exc_info=True)
            self._fail(uid, author, f"{type(error).__name__}: {error}")

    def _avatar_png(self, load_avatar: Callable[[], BinaryIO]) -> io.BytesIO:
        try:
            picture = load_avatar()
        except EFBOperationNotSupported:
            raise _AuthorFailed("the slave channel does not provide avatars") from None
        except Exception as error:
            raise _AuthorFailed(f"avatar unavailable: {type(error).__name__}: {error}") from error
        if picture is None:
            raise _AuthorFailed("no avatar")
        try:
            return make_avatar_png(picture)
        except Exception as error:
            raise _AuthorFailed(f"avatar image unreadable: {type(error).__name__}: {error}") from error
        finally:
            with suppress(Exception):
                picture.close()

    def _call(self, coroutine: Coroutine[Any, Any, T]) -> T:
        assert self._loop is not None
        return self._loop.run_until_complete(coroutine)

    def _upload(self, uid: str, png: io.BytesIO) -> tuple[str, str]:
        """Add the sticker to the current set, moving to the next set when it is full."""
        assert self._bot is not None
        owner = self._store.config.owner_user_id
        assert owner is not None
        sticker = InputSticker(
            sticker=png,
            emoji_list=[fallback_emoji(uid)],
            format="static",
            keywords=["etm-" + hashlib.sha1(uid.encode("utf-8")).hexdigest()[:16]],
        )
        while True:
            name = self._open_set()
            self._pace()
            try:
                if self._set_exists:
                    self._call(self._bot.add_sticker_to_set(user_id=owner, name=name, sticker=sticker))
                else:
                    self._call(self._bot.create_new_sticker_set(
                        user_id=owner, name=name, title=self._title, stickers=[sticker],
                        sticker_type=Sticker.CUSTOM_EMOJI))
                    self._set_exists = True
            except TelegramError as error:
                if isinstance(error, BadRequest) and any(
                        marker in _error_text(error) for marker in _SET_FULL_MARKERS):
                    self._move_set(StickerSetEvent.FILLED)
                    continue
                # A lost reply may hide a set that now exists, and a set may be deleted behind
                # our back: look the set up again before the next write.
                self._move_set(StickerSetEvent.WRITE_FAILED)
                self._telegram_failed(error)
            sticker_set = self._read_set(name)
            if not sticker_set.stickers or not sticker_set.stickers[-1].custom_emoji_id:
                raise _AuthorFailed(f"sticker set {name} has no new custom emoji")
            self._set_count = len(sticker_set.stickers)
            self._move_set(StickerSetEvent.FILLED if self._set_count >= STICKER_SET_LIMIT else StickerSetEvent.ADDED)
            return sticker_set.stickers[-1].custom_emoji_id, name

    def _open_set(self) -> str:
        """Name of a set with room, looking up each set at most once per session."""
        while self.set_state is not StickerSetState.OPEN:
            if self.set_state is StickerSetState.ERROR:
                raise _AuthorFailed("no sticker set available")
            index = self.set_index + 1 if self.set_state is StickerSetState.FULL else self.set_index
            if index > MAX_STICKER_SET_INDEX:
                self._move_set(StickerSetEvent.BROKEN)
                self._make_unavailable(f"all {MAX_STICKER_SET_INDEX} sticker sets are full")
                raise _AuthorFailed("no sticker set available")
            name = self._set_name(index)
            try:
                sticker_set = self._read_set(name)
            except _AuthorFailed as failure:
                if isinstance(failure.__cause__, BadRequest) and any(
                        marker in _error_text(failure.__cause__) for marker in _SET_MISSING_MARKERS):
                    self.set_index, self._set_exists, self._set_count = index, False, 0
                    self._move_set(StickerSetEvent.OPENED)
                    continue
                raise  # the set is looked up again for the next author
            self.set_index, self._set_exists, self._set_count = index, True, len(sticker_set.stickers)
            self._move_set(
                StickerSetEvent.FILLED if self._set_count >= STICKER_SET_LIMIT else StickerSetEvent.OPENED)
        return self._set_name()

    def _set_name(self, index: int | None = None) -> str:
        return user_emoji_set_name(
            self._store.config.sticker_set_name, self.set_index if index is None else index, self._bot_username)

    def _read_set(self, name: str) -> telegram.StickerSet:
        assert self._bot is not None
        try:
            return self._call(self._bot.get_sticker_set(name))
        except TelegramError as error:
            raise _AuthorFailed(f"reading sticker set {name} failed: {error}",
                                self._retry_after(error)) from error

    def _pace(self) -> None:
        delay = self._next_write_at - time.monotonic()
        if delay > 0 and self._stopping.wait(delay):
            raise _AuthorFailed("stopping")
        self._next_write_at = time.monotonic() + WRITE_INTERVAL

    @staticmethod
    def _retry_after(error: TelegramError) -> float:
        if not isinstance(error, RetryAfter):
            return 0.0
        value = error.retry_after
        return value.total_seconds() if isinstance(value, datetime.timedelta) else float(value)

    def _telegram_failed(self, error: TelegramError) -> NoReturn:
        if isinstance(error, (Forbidden, InvalidToken)) or (
                isinstance(error, BadRequest) and any(marker in _error_text(error) for marker in _REFUSAL_MARKERS)):
            self._make_unavailable(f"Telegram refused to create the emoji: {error}")
        retry_after = self._retry_after(error)
        if retry_after:
            self._next_write_at = max(self._next_write_at, time.monotonic() + retry_after)
        raise _AuthorFailed(f"Telegram error: {error}", retry_after)

    def _save(self, uid: str, author: _Author) -> None:
        assert author.unsaved is not None
        custom_emoji_id, set_name = author.unsaved
        if self._left_behind:
            raise _AuthorFailed("stopping")  # stop gave up waiting, so the database may be closed
        try:
            self._store.record(uid, custom_emoji_id, set_name)
        except Exception as error:
            self._save_failures += 1
            if self._save_failures >= SAVE_FAILURE_LIMIT:
                self._make_unavailable(f"saving custom emoji failed {self._save_failures} times: {error}")
            raise _AuthorFailed(f"saving failed: {type(error).__name__}: {error}") from error
        self._save_failures = 0
        with self._lock:
            self._move_author(uid, author, AuthorEmojiEvent.CREATED)
            self._authors.pop(uid, None)  # the store knows the emoji now
        logger.info("Created custom emoji for %s: custom_emoji_id=%s sticker_set=%s", uid, custom_emoji_id, set_name)

    def _fail(self, uid: str, author: _Author, reason: str, retry_after: float = 0.0) -> None:
        with self._lock:
            author.failures += 1
            delay = min(RETRY_BASE * 2 ** (author.failures - 1), RETRY_CAP)
            author.retry_at = time.monotonic() + max(delay, retry_after)
            self._move_author(uid, author, AuthorEmojiEvent.FAILED)
        logger.debug("No custom emoji for %s (attempt %d, next try in %.0f s): %s",
                     uid, author.failures, max(delay, retry_after), reason)

    def _make_unavailable(self, reason: str) -> None:
        with self._lock:
            if self.feature_state is not FeatureState.ENABLED:
                return
            self.feature_state = FEATURE_MACHINE.next_state("user emoji", self.feature_state, FeatureEvent.FAILED)
            self.unavailable_reason = reason
            while self._queue:
                uid = self._queue.popleft()
                self._move_author(uid, self._authors.pop(uid), AuthorEmojiEvent.DISCARDED)
        logger.warning("Custom emoji for slave users are unavailable for this session: %s", reason)

    @staticmethod
    def _move_author(uid: str, author: _Author, event: AuthorEmojiEvent) -> None:
        author.state = AUTHOR_EMOJI_MACHINE.next_state(uid, author.state, event)

    def _move_set(self, event: StickerSetEvent) -> None:
        self.set_state = STICKER_SET_MACHINE.next_state(self._set_name(), self.set_state, event)
