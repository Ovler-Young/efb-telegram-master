"""Remove held sends that can never be delivered; never send anything itself.

Run only while EFB is stopped. Without --apply, this only reports counts.
A held row is removed when its stored request can no longer be sent: its
external media file is gone, its queue-owned media file is gone, its request
cannot be decoded, or Telegram rejected its aggregated batch (BadRequest).
The --not-delivered file lists row ids found not delivered, one per line. A
listed held send whose media file is gone and whose caption has text from the
sender, beyond the header and the bot's media placeholder, is not removed: it
becomes a text send of its caption, to the same chat and topic, from the bot
that attempted it, which the bot sends on its next start in the row's place.
A listed send held after a timeout or a cut connection whose media files are
all still there is sent again on the next start, unchanged. Without that file
nothing is sent. A held or waiting container edit that has no container yet is
removed with the row that would have created it; it cannot be sent without it.
Every other row is left unchanged, so rows that were ordered behind a removed
row, or that the bot can still send, are free to be sent. With a PostgreSQL
queue, pass the same ``database`` settings the bot uses (``--config``).
"""
import argparse
import fcntl
import functools
import html
import inspect
import pickle
import re
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
from gettext import NullTranslations, translation
from pathlib import Path
from urllib.parse import urlsplit

from telegram import Bot

from .aggregation_states import ContextKind
from .db_runtime import DataDirectoryLock, postgresql_connection
from .delivery_states import Event, RowState, classify, next_state, parse_hold
from .outbound import (
    _DIRECT_MEDIA_ARGUMENTS,
    _POSTGRESQL_OWNER_LOCK,
    POSTGRESQL_QUEUE_MARKER,
    SUPPLEMENTAL_KEY,
    OutboundQueue,
)
from .paths import LOCALE_DIR
from .queued_log import decode_aggregation
from .utils import MEDIA_PROMPT_EMOJI, media_prompt

# Why a row is removed; only these names and their counts are reported.
MISSING_MEDIA = "missing_media"            # external media file gone
MISSING_SIDECAR = "missing_sidecar"        # queue-owned media file gone
INVALID_PAYLOAD = "invalid_payload"        # stored request cannot be decoded
REJECTED_BATCH = "rejected_batch"          # aggregated batch rejected with BadRequest
NEEDS_REMOVED_ROW = "needs_removed_row"    # edit of a container its removed predecessor would have created

_WAITING = (RowState.WAITING, RowState.DEFERRED)
_GONE_FILE = (MISSING_MEDIA, MISSING_SIDECAR)
# Telegram's names for a caption and its formatting in a text message.
_AS_TEXT = {"caption": "text", "caption_entities": "entities"}
_TEXT_PARAMETERS = inspect.signature(Bot.send_message).parameters
# Errors after which Telegram may not have received the send at all.
_TRANSIENT = ("TimedOut", "NetworkError")
# A held row whose caption is sent as text: its new request, its sender, the row id.
_REPLACE = (
    "UPDATE outbound_queue SET operation='send_message', payload=?, required_sender_bot_id=?, "
    "delivery_state='queued', delivery_hold=NULL, reconcile_after=0, reconcile_attempts=0, "
    "attempt_sender_bot_id=NULL, attempt_started_at=NULL, completion_receipt=NULL WHERE id=?"
)
# A held row sent again as it is stored: the row id.
_RESEND = (
    "UPDATE outbound_queue SET delivery_hold=NULL, reconcile_after=0, reconcile_attempts=0, "
    "attempt_sender_bot_id=NULL, attempt_started_at=NULL, completion_receipt=NULL WHERE id=?"
)


def missing_media(references, media_dir: Path) -> str | None:
    """Why the stored media ``references`` of a row cannot be sent, or None when every file is there."""
    for reference in references:
        if reference.external_uri is not None:
            # Replay downloads a missing web file again; a local one is gone for good.
            if urlsplit(reference.external_uri).scheme.lower() in {"http", "https"}:
                continue
            path = OutboundQueue._local_media_path(reference.external_uri)
            if path is None or not path.is_file():
                return MISSING_MEDIA
        elif reference.storage_name is None:
            return INVALID_PAYLOAD
        elif not (media_dir / reference.storage_name).is_file():
            return MISSING_SIDECAR
    return None


def _unsendable(state: RowState, hold: str, references, media_dir: Path) -> str | None:
    """Why a held row can never be sent, or None. ``references`` is None when not inspected."""
    if state is RowState.INVALID_PAYLOAD:
        return INVALID_PAYLOAD
    problem = missing_media(references or (), media_dir)
    if problem is not None:
        return problem
    if state is RowState.AGGREGATION_FAILED:
        parsed = parse_hold(hold)
        if parsed is not None and parsed[1].split("/", 1)[0] == "BadRequest":
            return REJECTED_BATCH
    return None


def _alone(log_context: bytes | None) -> bool:
    """Whether a row is sent on its own, not as part of a text batch.

    A member of a text batch is built again from its batch when sent.
    """
    try:
        context = None if log_context is None else decode_aggregation(log_context)
    except Exception:  # noqa: BLE001 - unpickling a damaged context can raise anything
        return False
    return context is None or context.get("kind") == ContextKind.BOUNDARY


def _caption_as_text(operation: str, payload: bytes, log_context: bytes | None) -> tuple[bytes, bool] | None:
    """The request that sends a gone media file's caption as text and whether
    that caption has no text from the sender; None to remove the row.

    A caption is kept for a send of one media file, alone or between text
    batches. A full-content attachment's caption only announces the file: it
    is removed.
    """
    if not operation.startswith("send_") or operation not in _DIRECT_MEDIA_ARGUMENTS or not _alone(log_context):
        return None
    args, kwargs = pickle.loads(payload[1:])
    if not isinstance(kwargs.get("caption"), str) or not kwargs["caption"].strip() or kwargs.get(SUPPLEMENTAL_KEY):
        return None
    signature = inspect.signature(getattr(Bot, operation))
    media_parameters = signature.parameters
    bound = signature.bind_partial(None, *args).arguments
    request = {}
    for key, value in {**bound, **kwargs}.items():
        name = _AS_TEXT.get(key, key)
        # The media file and media-only options go; queue metadata stays.
        if key != "self" and (name in _TEXT_PARAMETERS or key not in media_parameters):
            request[name] = value
    return OutboundQueue.encode_payload((), request), _header_only(kwargs["caption"], kwargs.get("parse_mode"))


def _log_context(run: Callable, row_id: int) -> bytes | None:
    value = run("SELECT log_context FROM outbound_queue WHERE id=?", (row_id,))[0][0]
    return None if value is None else bytes(value)


def _references(run: Callable, row_id: int, version: bytes, size: int):
    """The row's stored media references; None when they cannot be read."""
    if version == b"\x01":
        return []  # Older rows keep their media inline, not in sidecars.
    if version != b"\x02" or size > OutboundQueue.MAX_REPLAY_BYTES:
        return None
    payload = bytes(run("SELECT payload FROM outbound_queue WHERE id=?", (row_id,))[0][0])
    references: list = []
    try:
        OutboundQueue._collect_media_references(pickle.loads(payload[1:]), references)
    except Exception:  # noqa: BLE001 - unpickling a damaged row can raise anything
        return None
    return references


_HTML_TAG = re.compile(r"<[^>]*>")


@functools.cache
def _media_prompts() -> frozenset[str]:
    """Every line the bot writes for a media file sent without text, in any of its languages."""
    translators = [NullTranslations(), *(
        translation("efb_telegram_master", LOCALE_DIR, languages=[path.name], fallback=True)
        for path in LOCALE_DIR.iterdir() if path.is_dir())]
    return frozenset(media_prompt(kind, setting, translator.gettext)
                     for kind in MEDIA_PROMPT_EMOJI for setting in ("emoji", "text") for translator in translators)


def _header_only(caption: str, parse_mode: object) -> bool:
    """Whether a caption has no text from the sender: the bot's sender header,
    a line ending in ':' that the bot always ends with a newline, followed by
    nothing or only the bot's media placeholder.

    Such a caption is not sent. A caption with no newline has no header (the
    bot writes none in a chat linked to a single Telegram chat), so it is the
    sender's own text.
    """
    if str(parse_mode).lower() == "html":
        caption = html.unescape(_HTML_TAG.sub("", caption))
    header, newline, rest = caption.partition("\n")
    return (bool(newline) and header.rstrip().endswith(":")
            and all(not line.strip() or line.strip() in _media_prompts() for line in rest.split("\n")))


def _clear(
    run: Callable, media_dir: Path, not_delivered: frozenset[int],
) -> tuple[dict[int, str], dict[int, tuple], set[int], dict, set[str]]:
    """Decide which rows go, which send their caption as text instead and
    which are sent again.

    Only a row in ``not_delivered`` sends its caption, unless that caption has
    no text from the sender, or is sent again. Returns (row id -> reason, row
    id -> new request and sender, row ids sent again, report counts, sidecar
    names to delete).
    """
    now = time.time()
    states: dict[int, RowState] = {}
    operations: dict[int, str] = {}
    dependents: dict[int, list[int]] = defaultdict(list)
    predecessors: dict[int, int] = {}
    sidecars: dict[int, set[str]] = {}
    uninspected: set[int] = set()
    removed: dict[int, str] = {}
    captions: dict[int, tuple[bytes, str]] = {}
    resent: set[int] = set()
    unsent = 0  # Captions of removed rows that are not listed.
    header_only = 0  # Listed rows removed because their caption has no text from the sender.
    batch_members = 0  # Listed rows held after a timeout that are left held: they belong to a text batch.
    for row_id, operation, state, hold, reconcile_after, predecessor, version, size, required, attempted in run(
        "SELECT id, operation, delivery_state, delivery_hold, reconcile_after, predecessor_id, "
        "substr(payload, 1, 1), length(payload), required_sender_bot_id, attempt_sender_bot_id "
        "FROM outbound_queue ORDER BY id"
    ):
        current = classify(state, hold, reconcile_after or 0.0, now)
        states[row_id], operations[row_id] = current, operation
        if predecessor is not None and predecessor != row_id:
            dependents[predecessor].append(row_id)
            predecessors[row_id] = predecessor
        references = _references(run, row_id, bytes(version), size)
        if references is None:
            uninspected.add(row_id)
        else:
            sidecars[row_id] = {item.storage_name for item in references if item.storage_name is not None}
        if next_state(current, Event.DISCARD) is None:
            continue
        reason = _unsendable(current, hold, references, media_dir)
        listed = row_id in not_delivered
        if reason is None:
            # A listed send that may never have reached Telegram goes again, unchanged, if its files are all there.
            parsed = parse_hold(hold)
            if (listed and references and next_state(current, Event.RESEND) is not None
                    and parsed is not None and parsed[1].split("/", 1)[0] in _TRANSIENT):
                if _alone(_log_context(run, row_id)):
                    resent.add(row_id)
                else:
                    batch_members += 1
            continue
        caption = None
        if reason in _GONE_FILE and next_state(current, Event.REPLACE_REQUEST) is not None:
            payload = run("SELECT payload FROM outbound_queue WHERE id=?", (row_id,))[0][0]
            caption = _caption_as_text(operation, bytes(payload), _log_context(run, row_id))
        if caption is not None and listed and not caption[1]:
            # The same bot sends the caption; a row ordered behind it waits for it.
            captions[row_id] = (caption[0], required or attempted or "__main__")
            continue
        if caption is not None:
            header_only += listed
            unsent += not listed
        removed[row_id] = reason

    pending = list(removed)
    while pending:
        predecessor = pending.pop()
        for row_id in dependents.get(predecessor, ()):
            state = states[row_id]
            if row_id in removed or (state not in _WAITING and next_state(state, Event.DISCARD) is None):
                continue
            value = _log_context(run, row_id)
            try:
                own = None if value is None else decode_aggregation(value)
            except Exception:  # noqa: BLE001 - unpickling a damaged context can raise anything
                own = None
            if own is not None and own.get("kind") == ContextKind.MEMBER_UPDATE and not own.get("container_id"):
                removed[row_id] = NEEDS_REMOVED_ROW
                pending.append(row_id)

    kept = [row_id for row_id in states if row_id not in removed]
    media: set[str] = set()
    # A row whose media names cannot be read may share any sidecar: then keep them all.
    if not any(row_id in uninspected for row_id in kept):
        media = set().union(*(sidecars.get(row_id, set()) for row_id in (*removed, *captions)))
        media -= set().union(*(sidecars[row_id] for row_id in kept if row_id not in captions))
    report = {
        "rows": len(states),
        "removed": dict(Counter(removed.values())),
        "removed_by_operation": dict(Counter(operations[row_id] for row_id in removed)),
        "caption_sent_as_text": len(captions),
        # Removed rows with a caption that are not listed as not delivered.
        "caption_not_sent": unsent,
        # Listed rows removed without sending because their caption has no text from the sender.
        "caption_header_only": header_only,
        # Listed rows held after a timeout or a cut connection, with every file still there: sent again.
        "resend": len(resent),
        # Listed rows that would be sent again but belong to a text batch: left held.
        "resend_batch_member": batch_members,
        "kept": len(kept),
        # Kept rows that were ordered behind a removed row; nothing defers them now.
        "released": sum(1 for row_id in kept if predecessors.get(row_id) in removed),
    }
    return removed, captions, resent, report, media


def _clear_sqlite(directory: Path, apply: bool, not_delivered: frozenset[int]) -> tuple[dict, set[str]]:
    if (directory / POSTGRESQL_QUEUE_MARKER).exists():
        raise RuntimeError(
            "This outbound queue was moved to PostgreSQL; its SQLite copy is retained and no longer sends. "
            "Pass --config with the database settings the bot uses."
        )
    with closing(sqlite3.connect(
        (directory / OutboundQueue.filename).as_uri() + "?mode=rw", uri=True, timeout=5,
    )) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            removed, captions, resent, report, media = _clear(
                lambda sql, parameters=(): db.execute(sql, parameters).fetchall(), directory / "outbound-media",
                not_delivered)
            if apply:
                db.executemany("DELETE FROM outbound_queue WHERE id=?", ((row_id,) for row_id in removed))
                db.executemany(_REPLACE, ((*request, row_id) for row_id, request in captions.items()))
                db.executemany(_RESEND, ((row_id,) for row_id in resent))
                db.commit()
            else:
                db.rollback()
            return report, media
        except BaseException:
            db.rollback()
            raise


def _clear_postgresql(
    directory: Path, config: Mapping, apply: bool, not_delivered: frozenset[int],
) -> tuple[dict, set[str]]:
    connection = postgresql_connection(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '5s'")
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s::bigint + oid::bigint) FROM pg_namespace "
                "WHERE nspname = current_schema()", (_POSTGRESQL_OWNER_LOCK,),
            )
            owned = cursor.fetchone()
            if not owned or not owned[0]:
                raise RuntimeError("The outbound queue is in use by a running bot; stop it before clearing rows")

            def run(sql: str, parameters: tuple = ()) -> list:
                cursor.execute(sql.replace("?", "%s"), parameters)
                return cursor.fetchall()

            removed, captions, resent, report, media = _clear(run, directory / "outbound-media", not_delivered)
            if apply:
                cursor.executemany("DELETE FROM outbound_queue WHERE id=%s", [(row_id,) for row_id in removed])
                cursor.executemany(_REPLACE.replace("?", "%s"),
                                   [(*request, row_id) for row_id, request in captions.items()])
                cursor.executemany(_RESEND.replace("?", "%s"), [(row_id,) for row_id in resent])
                connection.commit()
            else:
                connection.rollback()
            return report, media
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def clear(
    directory: Path, *, apply: bool = False, database: Mapping | None = None, not_delivered: Path | None = None,
) -> dict:
    """Report, and with ``apply`` remove, the rows that can never be sent.

    ``not_delivered`` is a file of row ids, one per line, found not delivered.
    A row whose media file is gone, whose caption has text from the sender
    and whose id is in it is not removed: it becomes a text send of its
    caption. A listed row held after a timeout or a cut connection, with every
    media file still there, is sent again. Without the file nothing is sent.
    ``database`` is ETM's ``database`` setting; the rows are read from the
    queue of that backend. Queue-owned media files of removed rows, and of rows
    that now send only their caption, are deleted only when no remaining row
    refers to them; external media files are never deleted.
    """
    directory = directory.resolve()
    ids = frozenset() if not_delivered is None else frozenset(int(line) for line in not_delivered.read_text().split())
    backend = (database or {}).get("type", "sqlite")
    if backend not in ("sqlite", "postgresql"):
        raise ValueError(f"Unknown database type: {backend!r}")
    with ExitStack() as owner:
        owner.enter_context(DataDirectoryLock(directory))
        queue_lock = owner.enter_context((directory / ".outbound-queue.lock").open("a+b"))
        try:
            fcntl.flock(queue_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("The outbound queue is in use by a running bot; stop it before clearing rows") from None
        if database is not None and backend == "postgresql":
            report, media = _clear_postgresql(directory, database, apply, ids)
        else:
            report, media = _clear_sqlite(directory, apply, ids)
        # Without --apply this counts the files that would be deleted.
        deleted = 0
        for path in (directory / "outbound-media" / name for name in media):
            if not path.is_file():
                continue
            if apply:
                path.unlink()
            deleted += 1
    return dict(report, media_files_removed=deleted, applied=apply)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=None,
                        help="YAML with the database mapping the bot uses; needed only for a PostgreSQL queue")
    parser.add_argument("--not-delivered", type=Path, default=None,
                        help="File of row ids found not delivered, one per line; only these rows send their caption "
                             "as text when their media file is gone, or are sent again after a timeout")
    parser.add_argument("--apply", action="store_true",
                        help="Remove the rows, turn listed captions into text sends and release listed rows to be "
                             "sent again; without it only counts are printed")
    args = parser.parse_args(argv)
    errors: tuple = (OSError, ImportError, RuntimeError, ValueError, sqlite3.Error)
    try:
        database = None
        if args.config is not None:
            from ruamel.yaml import YAML

            with args.config.open() as stream:
                config = YAML(typ="safe").load(stream)
            if not isinstance(config, dict) or not isinstance(config.get("database"), dict):
                raise ValueError("Configuration must contain a database mapping")
            database = config["database"]
            if database.get("type") == "postgresql":
                import psycopg2

                errors += (psycopg2.Error,)
        print(clear(args.data_dir, apply=args.apply, database=database, not_delivered=args.not_delivered))
        if args.not_delivered is None:
            print("No --not-delivered file: no caption is sent as text and no held send is sent again; "
                  "every held send whose file is gone is removed.")
    except errors as error:
        parser.exit(1, f"Clear failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
