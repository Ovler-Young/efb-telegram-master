"""Check held sends against the destination chat itself.

A send whose result was lost (``uncertain:<error>`` hold) may or may not have reached its chat. This
module reads the chat around the attempt, with the same bot's own account over MTProto, and decides
for each held row whether a message matching it exists:

* ``FOUND``: a message matches. The matched message is handed to the owner, which records it as the
  delivery. The row is never sent again.
* ``NOT_FOUND``: the whole span in which the message could be is read and nothing matches.
* ``AMBIGUOUS``: the evidence does not decide. Nothing is written.
* ``TOO_EARLY``: the span is not closed yet (the attempt is recent, or no later message exists yet).

Only message reads by id and the lookup of a chat are sent to Telegram; nothing is sent, edited or
deleted. Logs carry counts only: never message text, titles, chat ids, tokens or API values.
"""
import asyncio
import collections
import datetime
import hashlib
import html
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from telethon import TelegramClient, errors, functions, types, utils
from telethon.sessions import StringSession

from .delivery_states import OPERATOR_OBSERVED_DELIVERY, HoldKind, parse_hold
from .outbound import OutboundQueue

logger = logging.getLogger(__name__)

WINDOW = 600           # seconds after the attempt in which a delivered message may carry its date
PRE = 5                # seconds before the attempt
AFTER_SLACK = 60       # a row is decided only this long after its window
RANGE_CAP = 800        # most ids read for one row
BATCH = 100            # most ids in one read
PACE = 15.0            # seconds between two calls of one bot
FLOOD_EXTRA = 5.0      # added to the seconds Telegram asks to wait
FIRST_PASS_DELAY = 60.0
PASS_INTERVAL = 600.0
MIN_PREFIX = 30        # shortest caption that may be matched as the start of a longer text


class Verdict(Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    TOO_EARLY = "too_early"


class ReadState(Enum):
    """How far the ids of a row's span were read."""

    CLOSED = "closed"   # up to a message dated after the window, or up to a logged later message
    CAPPED = "capped"   # RANGE_CAP ids read without closing
    OPEN = "open"       # the chat ended before a later message was seen


# --------------------------------------------------------------------------- what a row and a message are
KIND_OF_OPERATION = {
    "send_message": "text", "send_photo": "photo", "send_animation": "animation", "send_video": "video",
    "send_document": "document", "send_sticker": "sticker", "send_audio": "audio", "send_voice": "voice",
    "edit_message_text": "edit", "edit_message_caption": "edit",
}
# What Telegram may turn a send into: an animation is delivered as a video or document, and so on.
COMPATIBLE = {
    "text": {"text"}, "photo": {"photo"}, "sticker": {"sticker"},
    "animation": {"animation", "video", "document"},
    "video": {"video", "animation", "document"},
    "document": {"document", "animation", "video"},
    "audio": {"audio", "voice", "document"},
    "voice": {"voice", "audio", "document"},
}
_TAG = re.compile(r"<[^>]*>")


def _normalize(text: object, html_mode: bool) -> str:
    if not isinstance(text, str):
        return ""
    if html_mode:
        text = html.unescape(_TAG.sub("", text))
    return " ".join(text.split())


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class HeldRow:
    """A held send as the owner lists it (the queue columns the check needs)."""

    id: int
    chat_id: int
    operation: str
    payload: bytes = field(repr=False)
    slave_id: str | None
    sender_bot_id: str | None        # the bot that made the attempt; None is the main bot
    attempt_started_at: float | None
    hold: str
    thread_id: int | None = None


@dataclass(frozen=True)
class RowKeys:
    """What a held row is matched on; parsed from its payload and never logged."""

    id: int
    chat_id: int
    author: int                          # the bot that made the attempt
    kind: str                            # a COMPATIBLE key, or "edit"
    attempt: float
    thread: int | None
    text: str = field(repr=False)
    digest: str = field(repr=False)
    file_name: str = field(repr=False)
    target: int | None = None         # the message an edit changes


@dataclass(frozen=True)
class Seen:
    """One chat message as read over MTProto. ``message`` is the Telethon message itself."""

    id: int
    date: float
    author: int | None
    kind: str
    file_name: str = field(repr=False)
    digest: str = field(repr=False)
    length: int
    thread: int | None
    forwarded: bool
    edited: bool
    message: Any = field(repr=False, compare=False)

    @classmethod
    def from_message(cls, message: types.Message) -> "Seen":
        kind, file_name = "text", ""
        media = message.media
        if isinstance(media, types.MessageMediaPhoto):
            kind = "photo"
        elif isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
            attributes = media.document.attributes
            names = {type(attribute) for attribute in attributes}
            if types.DocumentAttributeSticker in names:
                kind = "sticker"
            elif types.DocumentAttributeAnimated in names:
                kind = "animation"
            elif types.DocumentAttributeVideo in names:
                kind = "video"
            elif types.DocumentAttributeAudio in names:
                voice = any(isinstance(a, types.DocumentAttributeAudio) and a.voice for a in attributes)
                kind = "voice" if voice else "audio"
            else:
                kind = "document"
            for attribute in attributes:
                if isinstance(attribute, types.DocumentAttributeFilename):
                    file_name = attribute.file_name or ""
        elif media is not None and not isinstance(media, (types.MessageMediaWebPage, types.MessageMediaEmpty)):
            kind = "other"
        text = _normalize(message.message, False)
        reply = message.reply_to
        top = getattr(reply, "reply_to_top_id", None) if reply else None
        direct = getattr(reply, "reply_to_msg_id", None) if reply else None
        thread = top or (direct if reply and getattr(reply, "forum_topic", False) else None)
        return cls(
            id=message.id, date=message.date.timestamp(), author=getattr(message.from_id, "user_id", None),
            kind=kind, file_name=file_name, digest=_digest(text), length=len(text), thread=thread,
            forwarded=message.fwd_from is not None, edited=message.edit_date is not None, message=message,
        )


def row_keys(row: HeldRow, author: int) -> RowKeys | None:
    """The match keys of a row; None when the row cannot be matched (operation, payload or time)."""
    kind = KIND_OF_OPERATION.get(row.operation)
    if kind is None or row.attempt_started_at is None:
        return None
    try:
        args, kwargs = OutboundQueue.decode_payload_raw(bytes(row.payload))
    except Exception:  # noqa: BLE001 - a damaged payload is simply not matchable
        return None
    field_name = "text" if row.operation in ("send_message", "edit_message_text") else "caption"
    parse_mode = kwargs.get("parse_mode")
    text = _normalize(kwargs.get(field_name), isinstance(parse_mode, str) and "html" in parse_mode.lower())
    thread = kwargs.get("message_thread_id")
    target = kwargs.get("message_id") if kind == "edit" else None
    if kind == "edit" and (isinstance(target, bool) or not isinstance(target, int)):
        return None
    references: list = []
    OutboundQueue._collect_media_references((args, kwargs), references)
    names = [kwargs.get("filename")] + [reference.filename for reference in references]
    file_name = next((name for name in names if isinstance(name, str) and name), "")
    return RowKeys(
        id=row.id, chat_id=row.chat_id, author=author, kind=kind, attempt=row.attempt_started_at,
        thread=thread if isinstance(thread, int) else row.thread_id, text=text, digest=_digest(text),
        file_name=file_name, target=target,
    )


def eligible(row: HeldRow) -> bool:
    """Rows this check may decide: an uncertain hold the bot itself made, on a supported operation."""
    parsed = parse_hold(row.hold)
    return (
        parsed is not None and parsed[0] is HoldKind.UNCERTAIN and parsed[1] != OPERATOR_OBSERVED_DELIVERY
        and row.attempt_started_at is not None and row.operation in KIND_OF_OPERATION
    )


# --------------------------------------------------------------------------- the decision
@dataclass(frozen=True)
class Span:
    """The ids read for a row: ``first`` to ``end``. ``exact`` is False when no earlier message was logged
    to start from, so the span may begin after the message."""

    first: int
    end: int
    state: ReadState
    exact: bool = True


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    message: Seen | None = None


def too_early(attempt: float, now: float) -> bool:
    return attempt + WINDOW + AFTER_SLACK > now


def _thread_ok(keys: RowKeys, seen: Seen) -> bool:
    return (keys.thread if keys.thread not in (None, 0, 1) else None) == (
        seen.thread if seen.thread not in (None, 0, 1) else None)


def _text_match(keys: RowKeys, seen: Seen) -> bool | str:
    """True for a text or caption match, "weak" when the message was edited since, else False."""
    if seen.digest == keys.digest and seen.length == len(keys.text):
        return True
    cut = keys.kind != "text" and MIN_PREFIX <= seen.length < len(keys.text)
    if cut and _digest(keys.text[:seen.length]) == seen.digest:
        return True           # a caption Telegram cut
    return "weak" if seen.edited else False


def _in_window(keys: RowKeys, seen: Seen) -> bool:
    return keys.attempt - PRE <= seen.date <= keys.attempt + WINDOW


def _visible(keys: RowKeys, span: Span, chat: Mapping[int, Seen]):
    for message_id in range(span.first, span.end + 1):
        seen = chat.get(message_id)
        if seen is not None and seen.author == keys.author and not seen.forwarded:
            yield seen


def _candidates(keys, span, chat, logged) -> list[tuple[Seen, bool]]:
    found = []
    for seen in _visible(keys, span, chat):
        if seen.kind not in COMPATIBLE[keys.kind] or not _in_window(keys, seen) or not _thread_ok(keys, seen):
            continue
        match = _text_match(keys, seen)
        if match and not logged(keys.chat_id, seen.id):
            found.append((seen, match is True))
    strong = [item for item in found if item[1]]
    found = strong or found
    if keys.file_name:
        found = [item for item in found if item[0].file_name == keys.file_name] or found
    return found


def _near_miss(keys, span, chat, logged) -> bool:
    """A same-bot, same-kind, same-topic, same-time message that is not logged but has other text."""
    return any(
        seen.kind in COMPATIBLE[keys.kind] and _in_window(keys, seen) and _thread_ok(keys, seen)
        and not logged(keys.chat_id, seen.id)
        for seen in _visible(keys, span, chat)
    )


def _assign(members: Sequence[RowKeys], pools: Mapping[int, list[Seen]]) -> dict[int, Seen] | None:
    """One message for each row, none twice; None when that is impossible."""
    taken: dict[int, Seen] = {}
    claimed: set[int] = set()
    for keys in sorted(members, key=lambda k: (len(pools[k.id]), k.attempt, k.id)):
        pick = next((seen for seen in sorted(pools[keys.id], key=lambda s: s.id) if seen.id not in claimed), None)
        if pick is None:
            return None
        claimed.add(pick.id)
        taken[keys.id] = pick
    return taken


def _clusters(rows: Sequence[RowKeys]) -> list[list[RowKeys]]:
    """Rows that look the same and whose windows touch: they compete for the same messages."""
    groups = collections.defaultdict(list)
    for keys in rows:
        groups[(keys.chat_id, keys.author, keys.kind, keys.thread, keys.digest, keys.file_name)].append(keys)
    clusters = []
    for members in groups.values():
        members.sort(key=lambda k: (k.attempt, k.id))
        current = [members[0]]
        for keys in members[1:]:
            if keys.attempt - PRE <= current[-1].attempt + WINDOW:
                current.append(keys)
            else:
                clusters.append(current)
                current = [keys]
        clusters.append(current)
    return clusters


def _edit_chains(rows: Sequence[RowKeys]) -> list[list[RowKeys]]:
    """Edits of one message, oldest first: a later applied edit shows that the earlier ones were applied."""
    chains = collections.defaultdict(list)
    for keys in rows:
        if keys.kind == "edit":
            chains[(keys.chat_id, keys.target)].append(keys)
    return [sorted(members, key=lambda k: k.id) for members in chains.values()]


def _missing(keys: RowKeys, span: Span, chat, logged) -> Verdict:
    if span.state is ReadState.OPEN:
        return Verdict.TOO_EARLY
    if span.state is ReadState.CAPPED or not span.exact or _near_miss(keys, span, chat, logged):
        return Verdict.AMBIGUOUS
    return Verdict.NOT_FOUND


def decide(rows: Sequence[RowKeys], spans: Mapping[int, Span], seen: Mapping[int, Mapping[int, Seen]],
           logged: Callable[[int, int], bool], now: float) -> dict[int, Decision]:
    """Decide every row. ``spans`` holds the read state of each row, ``seen[chat][message id]`` the messages
    read, ``logged(chat, message id)`` whether a message is already recorded. Pure: nothing is read or written."""
    out: dict[int, Decision] = {}
    sends = [keys for keys in rows if keys.kind != "edit"]
    for members in _edit_chains(rows):
        chat_id, target = members[0].chat_id, members[0].target
        assert target is not None   # an edit row always has its target
        if any(too_early(keys.attempt, now) for keys in members):
            out.update({keys.id: Decision(Verdict.TOO_EARLY) for keys in members})
            continue
        message = seen.get(chat_id, {}).get(target)
        applied = -1
        for index, keys in enumerate(members):
            if message is not None and message.digest == keys.digest and message.length == len(keys.text):
                applied = index
        for index, keys in enumerate(members):
            # A not applied edit carries the message it changes: a later edit date shows a newer edit.
            out[keys.id] = Decision(Verdict.FOUND if index <= applied else Verdict.NOT_FOUND, message)

    deferred = []
    contenders: list[tuple[list[RowKeys], dict[int, list[Seen]]]] = []
    early_ids: set[int] = set()
    early_weak: set[tuple[int, int]] = set()   # edited messages a recent cluster may own
    for cluster in _clusters(sends):
        early = any(too_early(keys.attempt, now) for keys in cluster)
        if early:
            out.update({keys.id: Decision(Verdict.TOO_EARLY) for keys in cluster})
            if not all(keys.id in spans for keys in cluster):
                continue
        chat = seen.get(cluster[0].chat_id, {})
        found = {keys.id: _candidates(keys, spans[keys.id], chat, logged) for keys in cluster}
        strong = {keys.id: [s for s, is_strong in found[keys.id] if is_strong] for keys in cluster}
        if any(strong.values()):
            contenders.append((cluster, strong))
            if early:
                early_ids.update(keys.id for keys in cluster)
        elif early:
            early_weak.update((keys.chat_id, s.id) for keys in cluster for s, _ in found[keys.id])
            continue
        elif any(found.values()):
            deferred.append((cluster, {keys.id: [s for s, _ in found[keys.id]] for keys in cluster}))
        else:
            for keys in cluster:
                out[keys.id] = Decision(_missing(keys, spans[keys.id], chat, logged))

    # Clusters that match the same message compete for it, so they are assigned together: a message is
    # never the delivery of two rows.
    groups: list[tuple[list[RowKeys], dict[int, list[Seen]], set[tuple[int, int]]]] = []
    for cluster, pools in contenders:
        claimed = {(cluster[0].chat_id, s.id) for pool in pools.values() for s in pool}
        members, merged = list(cluster), dict(pools)
        for other in [group for group in groups if group[2] & claimed]:
            groups.remove(other)
            members += other[0]
            merged.update(other[1])
            claimed |= other[2]
        groups.append((members, merged, claimed))
    for members, pools, _ in groups:
        taken = _assign(members, pools)
        for keys in members:
            if keys.id in early_ids:   # a recent cluster is decided on a later pass, but it still holds its messages
                continue
            out[keys.id] = Decision(Verdict.FOUND, taken[keys.id]) if taken else Decision(Verdict.AMBIGUOUS)
    taken_by_strong = set().union(*(group[2] for group in groups))

    # A message edited since it was sent matches only on its other keys, so it may belong to one cluster only
    # and not to a cluster that matched it on its text.
    claims = collections.Counter((cluster[0].chat_id, s.id) for cluster, pools in deferred
                                 for pool in pools.values() for s in pool)
    for cluster, pools in deferred:
        contested = any(claims[(cluster[0].chat_id, s.id)] > 1 or (cluster[0].chat_id, s.id) in taken_by_strong
                        or (cluster[0].chat_id, s.id) in early_weak
                        for pool in pools.values() for s in pool)
        taken = None if contested or not all(pools.values()) else _assign(cluster, pools)
        for keys in cluster:
            out[keys.id] = Decision(Verdict.FOUND, taken[keys.id]) if taken else Decision(Verdict.AMBIGUOUS)
    return out


# --------------------------------------------------------------------------- reading a chat
class ChatUnreadable(Exception):
    """The bot cannot resolve or read this chat."""


class BotReader:
    """One bot's read-only MTProto session. Calls: bot login, the chat lookup, ``channels.GetMessages``.

    Calls are at least ``PACE`` seconds apart, and a flood wait is waited out, so the limit is rarely met.
    """

    def __init__(self, token: str, api_id: int, api_hash: str,
                 client_factory: Callable[[], Any] | None = None):
        self._token = token
        self.bot_id = int(token.split(":", 1)[0])
        self._factory = client_factory or (lambda: TelegramClient(
            StringSession(), api_id, api_hash, receive_updates=False, flood_sleep_threshold=0))
        self._client = None
        self._entities: dict[int, Any] = {}
        self._next_call = 0.0
        self.calls = 0
        self.flood_waits = 0

    async def _paced(self, call: Callable):
        while True:
            delay = self._next_call - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            wait = PACE
            try:
                self.calls += 1
                return await call()
            except errors.FloodWaitError as error:
                self.flood_waits += 1
                wait = error.seconds + FLOOD_EXTRA
            finally:
                self._next_call = time.monotonic() + wait

    async def _connected(self):
        if self._client is None:
            client = self._factory()
            try:
                await client.start(bot_token=self._token)
            except BaseException:
                await client.disconnect()
                raise
            self._client = client
        return self._client

    async def _channel(self, chat_id: int):
        if chat_id in self._entities:
            return self._entities[chat_id]
        if utils.resolve_id(chat_id)[1] is not types.PeerChannel:
            raise ChatUnreadable("not a supergroup")
        client = await self._connected()
        try:
            channel = utils.get_input_channel(await self._paced(lambda: client.get_input_entity(chat_id)))
        except (errors.RPCError, ValueError, TypeError) as error:
            raise ChatUnreadable(type(error).__name__) from error
        self._entities[chat_id] = channel
        return channel

    async def messages(self, chat_id: int, ids: Sequence[int]) -> dict[int, Seen]:
        """The visible messages among ``ids`` (at most ``BATCH``)."""
        channel = await self._channel(chat_id)
        client = await self._connected()
        request = functions.channels.GetMessagesRequest(
            channel=channel, id=[types.InputMessageID(message_id) for message_id in ids])
        try:
            reply = await self._paced(lambda: client(request))
        except errors.RPCError as error:
            raise ChatUnreadable(type(error).__name__) from error
        messages = [message for message in reply.messages if isinstance(message, types.Message)]
        if any(message.post for message in messages):
            raise ChatUnreadable("broadcast channel")   # a channel post names no bot, so it cannot be matched
        return {message.id: Seen.from_message(message) for message in messages}

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()


# --------------------------------------------------------------------------- the check
class HeldSendCheck:
    """Decides held sends in a daemon thread with its own event loop.

    ``list_rows() -> list[HeldRow]`` lists the held rows. ``resolve(row_id, verdict, seen, reader_bot_id) ->
    bool`` applies a ``FOUND`` or ``NOT_FOUND`` verdict and returns whether it was applied; it is not called
    for ``AMBIGUOUS`` or ``TOO_EARLY``. ``seen`` is the matched message for a found send, the message an edit
    changes for an edit (None when it cannot be read), and None for a send not found. ``tokens``
    maps a sender bot id (None for the main bot) to its token. ``db`` is the ``DatabaseManager``.
    """

    def __init__(self, *, api_id: int, api_hash: str, tokens: Mapping[str | None, str], db,
                 list_rows: Callable[[], Sequence[HeldRow]],
                 resolve: Callable[[int, Verdict, Seen | None, int], bool],
                 client_factory: Callable[[], Any] | None = None):
        self._api = (api_id, api_hash)
        self._tokens = dict(tokens)
        self._db = db
        self._list_rows = list_rows
        self._resolve = resolve
        self._factory = client_factory
        self._readers: dict[str | None, BotReader] = {}
        self._done: set[tuple[int, float]] = set()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self._ready = threading.Event()

    # -- thread -------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="held-send-check", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self, timeout: float = 10.0) -> None:
        thread, loop, stopping = self._thread, self._loop, self._stopping
        if thread is None:
            return
        if loop is not None and stopping is not None:
            try:
                loop.call_soon_threadsafe(stopping.set)
            except RuntimeError:
                pass  # the loop already ended
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("Held-send check did not stop within %s seconds.", timeout)
        self._thread = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            asyncio.set_event_loop(loop)
            self._stopping = asyncio.Event()
            self._ready.set()
            loop.run_until_complete(self._main())
        finally:
            try:
                loop.run_until_complete(self._close_readers())
            except Exception as error:  # noqa: BLE001 - only the class name is logged
                logger.warning("Held-send check could not close its sessions: %s", type(error).__name__)
            loop.close()

    async def _wait(self, seconds: float) -> bool:
        """True when the check was asked to stop before ``seconds`` passed."""
        assert self._stopping is not None
        try:
            await asyncio.wait_for(self._stopping.wait(), seconds)
            return True
        except asyncio.TimeoutError:  # noqa: UP041 - a different class from the builtin before Python 3.11
            return False

    async def _main(self) -> None:
        assert self._stopping is not None
        delay = FIRST_PASS_DELAY
        while not await self._wait(delay):
            task = asyncio.ensure_future(self.run_pass())
            stop = asyncio.ensure_future(self._stopping.wait())
            await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
            stop.cancel()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
            if task.exception() is not None:
                logger.error("Held-send check pass failed: %s", type(task.exception()).__name__)
            delay = PASS_INTERVAL

    async def _close_readers(self) -> None:
        readers, self._readers = list(self._readers.values()), {}
        for reader in readers:
            await reader.close()

    # -- one pass -----------------------------------------------------------
    async def run_pass(self) -> collections.Counter:
        """Check every held row not decided yet; returns counts by outcome."""
        counts: collections.Counter = collections.Counter()
        rows = [row for row in self._list_rows() if eligible(row)]
        by_bot = collections.defaultdict(list)
        for row in rows:
            if row.sender_bot_id not in self._tokens:
                counts["bot_not_configured"] += 1
            else:
                by_bot[row.sender_bot_id].append(row)
        results = await asyncio.gather(*(self._check_bot(bot, held) for bot, held in by_bot.items()),
                                       return_exceptions=True)
        for bot, result in zip(by_bot, results):
            if isinstance(result, BaseException):
                counts["bot_failed"] += 1
                logger.warning("Held-send check could not read as one bot: %s", type(result).__name__)
            else:
                counts.update(result)
        logger.info("Held-send check pass: %s", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no rows")
        return counts

    def _reader(self, bot: str | None) -> BotReader:
        if bot not in self._readers:
            self._readers[bot] = BotReader(self._tokens[bot], *self._api, client_factory=self._factory)
        return self._readers[bot]

    def _span(self, row: HeldRow, keys: RowKeys) -> tuple[int, int | None, bool] | None:
        """(first id, last id or None, exact) to read for a send; None when it cannot be placed."""
        if row.slave_id is None:
            return None
        before, after = self._db.logged_id_bounds(
            row.slave_id, row.chat_id,
            datetime.datetime.fromtimestamp(keys.attempt - PRE),
            datetime.datetime.fromtimestamp(keys.attempt + WINDOW + AFTER_SLACK))
        if before is None and after is None:
            return None
        if before is None:
            return max(1, after - RANGE_CAP), after - 1, False
        return before + 1, None if after is None else after - 1, True

    @staticmethod
    async def _read(reader: BotReader, store, asked, chat_id: int, ids: Sequence[int]) -> None:
        """Read the ids of one chat that have not been read in this pass."""
        missing = [i for i in ids if i not in asked[chat_id]]
        if missing:
            store[chat_id].update(await reader.messages(chat_id, missing))
            asked[chat_id].update(missing)

    async def _fill(self, reader: BotReader, store, asked, keys: RowKeys, first: int, last: int | None,
                    exact: bool) -> Span:
        """Read upward from ``first`` until a message after the window is seen, ``last`` or the cap is reached."""
        chat = store[keys.chat_id]
        high = first + RANGE_CAP - 1 if last is None else min(last, first + RANGE_CAP - 1)
        window_end = keys.attempt + WINDOW
        cursor = first
        while cursor <= high:
            batch = range(cursor, min(cursor + BATCH, high + 1))
            await self._read(reader, store, asked, keys.chat_id, batch)
            visible = [chat[i] for i in batch if i in chat]
            if any(seen.date > window_end for seen in visible):
                return Span(first, batch[-1], ReadState.CLOSED, exact)
            if last is None and not visible:
                return Span(first, batch[-1], ReadState.OPEN, exact)
            cursor = batch[-1] + 1
        state = ReadState.CLOSED if last is not None and high == last else ReadState.CAPPED
        return Span(first, high, state, exact)

    async def _check_bot(self, bot: str | None, held: list[HeldRow]) -> collections.Counter:
        counts: collections.Counter = collections.Counter()
        reader = self._reader(bot)
        now = time.time()
        keyed: dict[int, tuple[HeldRow, RowKeys]] = {}
        for row in held:
            keys = row_keys(row, reader.bot_id)
            if keys is None:
                counts["undecodable"] += 1
                assert row.attempt_started_at is not None   # only rows with an attempt time are eligible
                self._done.add((row.id, row.attempt_started_at))
            else:
                keyed[row.id] = (row, keys)
        every = [keys for _, keys in keyed.values()]
        groups = _clusters([keys for keys in every if keys.kind != "edit"]) + _edit_chains(every)
        # a group is decided as a whole, and only when none of its rows is too recent
        groups = [group for group in groups if not all((keys.id, keys.attempt) in self._done for keys in group)]
        # recent edits wait; a recent send cluster is still read, so it can hold back the messages it may own
        counts["too_early"] += sum(len(group) for group in groups
                                   if group[0].kind == "edit" and any(too_early(k.attempt, now) for k in group))
        groups = [group for group in groups
                  if group[0].kind != "edit" or not any(too_early(keys.attempt, now) for keys in group)]
        store: dict[int, dict[int, Seen]] = collections.defaultdict(dict)
        asked: dict[int, set[int]] = collections.defaultdict(set)
        spans: dict[int, Span] = {}
        usable: list[RowKeys] = []
        targets: dict[int, set[int]] = collections.defaultdict(set)
        for group in groups:
            target = group[0].target
            if group[0].kind == "edit" and target is not None:
                targets[group[0].chat_id].add(target)
        unreadable_chats: set[int] = set()
        for chat_id, ids in targets.items():
            ordered = sorted(ids)
            try:
                for index in range(0, len(ordered), BATCH):
                    await self._read(reader, store, asked, chat_id, ordered[index:index + BATCH])
            except ChatUnreadable:
                unreadable_chats.add(chat_id)
        for group in groups:
            if group[0].kind == "edit":
                if group[0].chat_id in unreadable_chats:
                    counts["chat_unreadable"] += 1
                else:
                    usable.extend(group)
                continue
            placed = [self._span(keyed[keys.id][0], keys) for keys in group]
            located = [where for where in placed if where is not None]
            if len(located) < len(placed):
                counts["no_neighbours"] += 1
                continue
            try:
                for keys, where in zip(group, located, strict=True):
                    spans[keys.id] = await self._fill(reader, store, asked, keys, *where)
            except ChatUnreadable:
                counts["chat_unreadable"] += 1
            else:
                usable.extend(group)
        decisions = decide(usable, spans, store, self._db.is_message_logged, now)
        for row_id, decision in decisions.items():
            key = (row_id, keyed[row_id][1].attempt)
            if key in self._done:
                continue
            if decision.verdict is Verdict.TOO_EARLY:
                counts["too_early"] += 1
                continue
            self._done.add(key)
            if decision.verdict is Verdict.AMBIGUOUS:
                counts["ambiguous"] += 1
                continue
            try:
                applied = self._resolve(row_id, decision.verdict, decision.message, reader.bot_id)
            except Exception as error:  # noqa: BLE001 - one row must not stop the others
                self._done.discard(key)
                counts["resolve_failed"] += 1
                logger.warning("Held-send check could not apply a verdict: %s", type(error).__name__)
                continue
            counts[decision.verdict.value if applied else decision.verdict.value + "_not_applied"] += 1
        counts["calls"] += reader.calls
        counts["flood_waits"] += reader.flood_waits
        reader.calls = reader.flood_waits = 0
        return counts
