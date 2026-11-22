"""Remove held sends that can never be delivered; never send anything.

Run only while EFB is stopped. Without --apply, this only reports counts.
A held row is removed when its stored request can no longer be sent: its
external media file is gone, its queue-owned media file is gone, its request
cannot be decoded, or Telegram rejected its aggregated batch (BadRequest).
A held or waiting container edit that has no container yet is removed with
the row that would have created it; it cannot be sent without it. Every other
row is left unchanged, so rows that were ordered behind a removed row, or that
the bot can still send, are free to be sent. With a PostgreSQL queue,
pass the same ``database`` settings the bot uses (``--config``).
"""
import argparse
import fcntl
import pickle
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from contextlib import ExitStack, closing
from pathlib import Path
from urllib.parse import urlsplit

from .aggregation_states import ContextKind
from .db_runtime import DataDirectoryLock, postgresql_connection
from .delivery_states import Event, RowState, classify, next_state, parse_hold
from .outbound import _POSTGRESQL_OWNER_LOCK, POSTGRESQL_QUEUE_MARKER, OutboundQueue
from .queued_log import decode_aggregation

# Why a row is removed; only these names and their counts are reported.
MISSING_MEDIA = "missing_media"            # external media file gone
MISSING_SIDECAR = "missing_sidecar"        # queue-owned media file gone
INVALID_PAYLOAD = "invalid_payload"        # stored request cannot be decoded
REJECTED_BATCH = "rejected_batch"          # aggregated batch rejected with BadRequest
NEEDS_REMOVED_ROW = "needs_removed_row"    # edit of a container its removed predecessor would have created

_WAITING = (RowState.WAITING, RowState.DEFERRED)


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


def _clear(run: Callable, media_dir: Path) -> tuple[dict[int, str], dict, set[str]]:
    """Decide which rows go: (row id -> reason, report counts, sidecar names to delete)."""
    now = time.time()
    states: dict[int, RowState] = {}
    operations: dict[int, str] = {}
    dependents: dict[int, list[int]] = defaultdict(list)
    predecessors: dict[int, int] = {}
    sidecars: dict[int, set[str]] = {}
    uninspected: set[int] = set()
    removed: dict[int, str] = {}
    for row_id, operation, state, hold, reconcile_after, predecessor, version, size in run(
        "SELECT id, operation, delivery_state, delivery_hold, reconcile_after, predecessor_id, "
        "substr(payload, 1, 1), length(payload) FROM outbound_queue ORDER BY id"
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
            if reason is not None:
                removed[row_id] = reason

    contexts: dict[int, dict | None] = {}

    def context(row_id: int) -> dict | None:
        if row_id not in contexts:
            value = run("SELECT log_context FROM outbound_queue WHERE id=?", (row_id,))[0][0]
            try:
                contexts[row_id] = None if value is None else decode_aggregation(bytes(value))
            except Exception:  # noqa: BLE001 - unpickling a damaged context can raise anything
                contexts[row_id] = None
        return contexts[row_id]

    pending = list(removed)
    while pending:
        predecessor = pending.pop()
        for row_id in dependents.get(predecessor, ()):
            state = states[row_id]
            if row_id in removed or (state not in _WAITING and next_state(state, Event.DISCARD) is None):
                continue
            own = context(row_id)
            if own is not None and own.get("kind") == ContextKind.MEMBER_UPDATE and not own.get("container_id"):
                removed[row_id] = NEEDS_REMOVED_ROW
                pending.append(row_id)

    kept = [row_id for row_id in states if row_id not in removed]
    media: set[str] = set()
    # A row whose media names cannot be read may share any sidecar: then keep them all.
    if not any(row_id in uninspected for row_id in kept):
        media = set().union(*(sidecars.get(row_id, set()) for row_id in removed))
        media -= set().union(*(sidecars[row_id] for row_id in kept))
    report = {
        "rows": len(states),
        "removed": dict(Counter(removed.values())),
        "removed_by_operation": dict(Counter(operations[row_id] for row_id in removed)),
        "kept": len(kept),
        # Kept rows that were ordered behind a removed row; nothing defers them now.
        "released": sum(1 for row_id in kept if predecessors.get(row_id) in removed),
    }
    return removed, report, media


def _clear_sqlite(directory: Path, apply: bool) -> tuple[dict, set[str]]:
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
            removed, report, media = _clear(
                lambda sql, parameters=(): db.execute(sql, parameters).fetchall(), directory / "outbound-media")
            if apply:
                db.executemany("DELETE FROM outbound_queue WHERE id=?", ((row_id,) for row_id in removed))
                db.commit()
            else:
                db.rollback()
            return report, media
        except BaseException:
            db.rollback()
            raise


def _clear_postgresql(directory: Path, config: Mapping, apply: bool) -> tuple[dict, set[str]]:
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

            removed, report, media = _clear(run, directory / "outbound-media")
            if apply:
                cursor.executemany("DELETE FROM outbound_queue WHERE id=%s", [(row_id,) for row_id in removed])
                connection.commit()
            else:
                connection.rollback()
            return report, media
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def clear(directory: Path, *, apply: bool = False, database: Mapping | None = None) -> dict:
    """Report, and with ``apply`` remove, the rows that can never be sent.

    ``database`` is ETM's ``database`` setting; the rows are read from the queue
    of that backend. Queue-owned media files of removed rows are deleted only when
    no remaining row refers to them; external media files are never deleted.
    """
    directory = directory.resolve()
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
            report, media = _clear_postgresql(directory, database, apply)
        else:
            report, media = _clear_sqlite(directory, apply)
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
    parser.add_argument("--apply", action="store_true", help="Remove the rows; without it only counts are printed")
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
        print(clear(args.data_dir, apply=args.apply, database=database))
    except errors as error:
        parser.exit(1, f"Clear failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
