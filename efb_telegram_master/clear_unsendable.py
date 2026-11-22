"""Remove held sends that can never be delivered; never send anything itself.

Run only while EFB is stopped. Without --apply, this only reports counts.
A held row is removed when its stored request can no longer be sent: its
external media file is gone, its queue-owned media file is gone, its request
cannot be decoded, or Telegram rejected its aggregated batch (BadRequest).
A held send whose media file is gone, whose message has text and whose id is
listed in the --not-delivered file (one row id per line, the rows found not
delivered) is not removed: it becomes a text send of its caption, to the same
chat and topic, from the bot that attempted it, which the bot sends on its next
start in the row's place. Without that file no caption is sent. A held or
waiting container edit that has no container yet is removed with the row that
would have created it; it cannot be sent without it. Every
other row is left unchanged, so rows that were ordered behind a removed row,
or that the bot can still send, are free to be sent. With a PostgreSQL queue,
pass the same ``database`` settings the bot uses (``--config``).
"""
import argparse
import fcntl
import inspect
import pickle
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
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
from .queued_log import decode, decode_aggregation

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
# A held row whose caption is sent as text: its new request, its sender, the row id.
_REPLACE = (
    "UPDATE outbound_queue SET operation='send_message', payload=?, required_sender_bot_id=?, "
    "delivery_state='queued', delivery_hold=NULL, reconcile_after=0, reconcile_attempts=0, "
    "attempt_sender_bot_id=NULL, attempt_started_at=NULL, completion_receipt=NULL WHERE id=?"
)


def _unsendable(state: RowState, hold: str, references, media_dir: Path) -> str | None:
    """Why a held row can never be sent, or None. ``references`` is None when not inspected."""
    if state is RowState.INVALID_PAYLOAD:
        return INVALID_PAYLOAD
    for reference in references or ():
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
    if state is RowState.AGGREGATION_FAILED:
        parsed = parse_hold(hold)
        if parsed is not None and parsed[1].split("/", 1)[0] == "BadRequest":
            return REJECTED_BATCH
    return None


def _caption_as_text(operation: str, payload: bytes, log_context: bytes | None) -> bytes | None:
    """The request that sends a gone media file's caption as text, or None to remove the row.

    A caption is kept for a send of one media file, alone or between text
    batches, whose message has text. A stored caption usually starts with the
    sender's name and shows a placeholder when the message has no text, so a
    message without text is removed. A member of a text batch is built again
    from its batch when sent, and a full-content attachment's caption only
    announces the file: both are removed too.
    """
    if not operation.startswith("send_") or operation not in _DIRECT_MEDIA_ARGUMENTS:
        return None
    try:
        context = None if log_context is None else decode_aggregation(log_context)
        if context is not None and context.get("kind") != ContextKind.BOUNDARY:
            return None
        message = context["legacy_context"] if context is not None else log_context
        if message is not None and not (decode(message)[0].text or "").strip():
            return None
    except Exception:  # noqa: BLE001 - unpickling a damaged context can raise anything
        return None
    args, kwargs = pickle.loads(payload[1:])
    if not isinstance(kwargs.get("caption"), str) or not kwargs["caption"].strip() or kwargs.get(SUPPLEMENTAL_KEY):
        return None
    media_parameters = inspect.signature(getattr(Bot, operation)).parameters
    bound = inspect.signature(getattr(Bot, operation)).bind_partial(None, *args).arguments
    request = {}
    for key, value in {**bound, **kwargs}.items():
        name = _AS_TEXT.get(key, key)
        # The media file and media-only options go; queue metadata stays.
        if key != "self" and (name in _TEXT_PARAMETERS or key not in media_parameters):
            request[name] = value
    return OutboundQueue.encode_payload((), request)


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


def _clear(
    run: Callable, media_dir: Path, not_delivered: frozenset[int],
) -> tuple[dict[int, str], dict[int, tuple], dict, set[str]]:
    """Decide which rows go and which send their caption as text instead.

    Only a row in ``not_delivered`` sends its caption. Returns (row id -> reason, row id -> new request and sender, report counts,
    sidecar names to delete).
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
    unsent = 0  # Captions of removed rows.
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
        if next_state(current, Event.DISCARD) is not None:
            reason = _unsendable(current, hold, references, media_dir)
            text = None
            if reason in _GONE_FILE and next_state(current, Event.REPLACE_REQUEST) is not None:
                payload, log_context = run("SELECT payload, log_context FROM outbound_queue WHERE id=?", (row_id,))[0]
                text = _caption_as_text(operation, bytes(payload), None if log_context is None else bytes(log_context))
            if text is not None and row_id in not_delivered:
                # The same bot sends the caption; a row ordered behind it waits for it.
                captions[row_id] = (text, required or attempted or "__main__")
            elif reason is not None:
                unsent += text is not None
                removed[row_id] = reason

    pending = list(removed)
    while pending:
        predecessor = pending.pop()
        for row_id in dependents.get(predecessor, ()):
            state = states[row_id]
            if row_id in removed or (state not in _WAITING and next_state(state, Event.DISCARD) is None):
                continue
            value = run("SELECT log_context FROM outbound_queue WHERE id=?", (row_id,))[0][0]
            try:
                own = None if value is None else decode_aggregation(bytes(value))
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
        # Removed rows whose message had text: not listed as not delivered.
        "caption_not_sent": unsent,
        "kept": len(kept),
        # Kept rows that were ordered behind a removed row; nothing defers them now.
        "released": sum(1 for row_id in kept if predecessors.get(row_id) in removed),
    }
    return removed, captions, report, media


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
            removed, captions, report, media = _clear(
                lambda sql, parameters=(): db.execute(sql, parameters).fetchall(), directory / "outbound-media",
                not_delivered)
            if apply:
                db.executemany("DELETE FROM outbound_queue WHERE id=?", ((row_id,) for row_id in removed))
                db.executemany(_REPLACE, ((*request, row_id) for row_id, request in captions.items()))
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

            removed, captions, report, media = _clear(run, directory / "outbound-media", not_delivered)
            if apply:
                cursor.executemany("DELETE FROM outbound_queue WHERE id=%s", [(row_id,) for row_id in removed])
                cursor.executemany(_REPLACE.replace("?", "%s"),
                                   [(*request, row_id) for row_id, request in captions.items()])
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
    A row whose media file is gone, whose message has text and whose id is in
    it is not removed: it becomes a text send of its caption. Without the file
    no caption is sent. ``database`` is ETM's ``database`` setting; the rows
    are read from the queue of that backend. Queue-owned media files of
    removed rows, and of rows that now send only their caption, are deleted
    only when no remaining row refers to them; external media files are never
    deleted.
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
                             "as text when their media file is gone")
    parser.add_argument("--apply", action="store_true",
                        help="Remove the rows and turn listed captions into text sends; without it only counts are printed")
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
            print("No --not-delivered file: no caption is sent as text; every held send whose file is gone is removed.")
    except errors as error:
        parser.exit(1, f"Clear failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
