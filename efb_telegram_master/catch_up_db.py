"""Bring a PostgreSQL import up to date with the SQLite database it was copied from.

Run ``python -m efb_telegram_master.catch_up_db --help``. A catch-up without ``--final``
runs with the bot up; the final one needs the bot and all other writers stopped.
"""

import argparse
import datetime
import logging
import sqlite3
import uuid
from contextlib import closing, nullcontext, suppress
from pathlib import Path
from typing import Iterator

from peewee import CompositeKey, chunked

from .db import MsgLog, MsgLogMember
from .db_runtime import SCHEMA_LOCK, DataDirectoryLock, connection_scope, current_schema, postgresql_database
from .migrate_db import (
    IMPORT_TABLE, MODELS, RECEIPT_FILE, _cache_models, _fields, _fingerprint, _json, _order, _projection, _quote,
    _read_database_config, _read_import, _recovery_columns, _reset_sequence, _source_rows, _validate_recovery_extras,
    _validate_source, _value, _write_receipt,
)

logger = logging.getLogger(__name__)
# Import record key of the catch-up: the base file, the keys it updated and the write baseline.
CATCH_UP_KEY = "catch_up"
# Found by SQLite rowid on catch-up; every other recorded table is copied whole.
WINDOWED, APPEND_ONLY = "msglog", "msglogmember"


def _open_source(path: Path, *, immutable: bool) -> sqlite3.Connection:
    # immutable=1 also ignores any -wal, so it is only used on a file nothing writes.
    query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return sqlite3.connect(path.resolve().as_uri() + query, uri=True, isolation_level=None)


def _extent(source: sqlite3.Connection, table: str) -> tuple[int, int]:
    # Two statements: a bare count(*) scans the smallest index and max(rowid) is one seek;
    # combined, SQLite would scan the whole table.
    count = source.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0]
    top = source.execute(f"SELECT max(rowid) FROM {_quote(table)}").fetchone()[0]
    return count, top or 0


def _rowid_rows(source: sqlite3.Connection, table: str, fields, start: int):
    return source.execute(
        f"SELECT rowid, {_projection(source, table, fields)} FROM {_quote(table)} WHERE rowid >= ? ORDER BY rowid",
        (start,),
    )


def _window_start(base: sqlite3.Connection, top: int, days: int) -> int:
    """First msglog rowid whose time is at most ``days`` before the base file's newest message.

    Rowids follow insert order, but times are not monotonic in rowid, so every chat is
    searched through the history index instead of bisecting rowids.
    """
    seek = "FROM msglog INDEXED BY msglog_history_seek"
    chats = [row[0] for row in base.execute(
        f"WITH RECURSIVE chat(uid) AS (SELECT (SELECT min(slave_origin_uid) {seek}) UNION ALL "
        f"SELECT (SELECT min(slave_origin_uid) {seek} WHERE slave_origin_uid > uid) FROM chat WHERE uid IS NOT NULL) "
        "SELECT uid FROM chat WHERE uid IS NOT NULL"
    )]
    times = [time for chat in chats
             if (time := base.execute(f"SELECT max(time) {seek} WHERE slave_origin_uid = ?", (chat,)).fetchone()[0])
             is not None]
    if not times:
        return top + 1
    newest = MsgLog.time.python_value(max(times))
    if not isinstance(newest, datetime.datetime):
        raise TypeError("Unreadable msglog time in the base file; refusing to catch up.")
    cutoff = (newest - datetime.timedelta(days=days)).isoformat(" ")
    starts = [base.execute(f"SELECT min(rowid) {seek} WHERE slave_origin_uid = ? AND time >= ?",
                           (chat, cutoff)).fetchone()[0] for chat in chats]
    return min(start for start in starts if start is not None)


def _changed_messages(base, source, fields, start: int, top: int, updated: set, changed: set) -> Iterator[tuple]:
    """Yield the msglog rows of tgdata.db that PostgreSQL may lack.

    These are the rows from ``start`` on that differ from the base file, every row added
    after the base file, and the rows that earlier runs updated, which may have been
    edited back since. Keys of the differing rows the base file has are added to ``changed``.
    """
    key = [field.column_name for field in fields].index("master_msg_id")
    seen = set()
    base_rows = _rowid_rows(base, "msglog", fields, start)
    for rowid, *values in _rowid_rows(source, "msglog", fields, start):
        row = tuple(values)
        if rowid <= top:
            before = next(base_rows, None)
            if before is None or before[0] != rowid or before[1 + key] != row[key]:
                raise RuntimeError(
                    f"msglog of tgdata.db and of the base file differ at rowid {rowid}: rows were deleted or "
                    "renumbered; refusing to catch up."
                )
            if tuple(before[1:]) == row:
                continue
            changed.add(row[key])
        seen.add(row[key])
        yield row
    if next(base_rows, None) is not None:
        raise RuntimeError("A msglog row of the base file is missing from tgdata.db; refusing to catch up.")
    projection = _projection(source, "msglog", fields)
    for message in sorted(updated - seen):
        row = source.execute(f"SELECT {projection} FROM msglog WHERE master_msg_id = ?", (message,)).fetchone()
        if row is None:
            raise RuntimeError("A msglog row that an earlier catch-up wrote is missing from tgdata.db; refusing to catch up.")
        yield row


def _write_rows(db, model, fields, rows, *, update: bool) -> list:
    """Insert ``rows``; with ``update``, rewrite an existing row only where it differs.

    Returns the key and an inserted flag of every row written.
    """
    names = [_quote(field.column_name) for field in fields]
    primary = model._meta.primary_key
    keys = ([_quote(model._meta.fields[name].column_name) for name in primary.field_names]
            if isinstance(primary, CompositeKey) else [_quote(primary.column_name)])
    rest = [name for name in names if name not in keys]
    row = "(" + ", ".join("%s" for _ in names) + ")"
    sql = (f"INSERT INTO {_quote(model._meta.table_name)} AS target ({', '.join(names)}) VALUES "
           f"{', '.join(row for _ in rows)} ON CONFLICT ({', '.join(keys)}) ")
    if update and rest:
        sql += ("DO UPDATE SET " + ", ".join(f"{name} = EXCLUDED.{name}" for name in rest)
                + f" WHERE ROW({', '.join('target.' + name for name in rest)})"
                + f" IS DISTINCT FROM ROW({', '.join('EXCLUDED.' + name for name in rest)})")
    else:
        sql += "DO NOTHING"
    sql += f" RETURNING {', '.join('target.' + key for key in keys)}, target.xmax = 0"
    return db.execute_sql(sql, [value for item in rows for value in item]).fetchall()


def _target_rows(db, model, fields) -> list:
    projection = ", ".join(_quote(field.column_name) for field in fields)
    cursor = db.execute_sql(f"SELECT {projection} FROM {_quote(model._meta.table_name)} "
                            f"ORDER BY {_order(model, fields, postgres=True)}")
    return [tuple(_value(field, value) for field, value in zip(fields, row)) for row in cursor]


def _write_counters(db, tables) -> dict:
    counters = {name: list(values) for name, *values in db.execute_sql(
        "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables "
        "WHERE schemaname = current_schema() AND relname = ANY(%s)", (list(tables),),
    )}
    return {table: counters.get(table, [0, 0, 0]) for table in tables}


def _check_target(db, manifest: dict, columns: dict, baseline) -> None:
    """Refuse a target whose columns or rows no longer match the import record or the last catch-up."""
    for table, names in columns.items():
        if {column.name for column in db.get_columns(table, schema=current_schema(db))} != set(names):
            raise RuntimeError(f"PostgreSQL columns of {table} differ from the import record; refusing to catch up.")
    rows = {table: db.execute_sql(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0] for table in columns}
    if baseline is None:
        if rows != {table: manifest["tables"][table]["rows"] for table in columns}:
            raise RuntimeError("PostgreSQL row counts differ from the import record; refusing to catch up.")
        return
    counters = _write_counters(db, columns)
    if {table: [rows[table], *counters[table]] for table in columns} != baseline:
        raise RuntimeError(
            "PostgreSQL was written since the last catch-up (row counts or write counters changed); "
            "refusing to catch up."
        )


def _comparable(manifest: dict) -> dict:
    catch = {key: value for key, value in manifest.get(CATCH_UP_KEY, {}).items() if key != "baseline"}
    return dict(manifest, import_id=None, **{CATCH_UP_KEY: catch})


def _catch_up_target(db, directory: Path, base_path: Path, source_path: Path, final: bool,
                     window_days: int, batch_size: int) -> tuple[dict, dict]:
    summary: dict = {}
    with (closing(_open_source(base_path, immutable=True)) as base,
          closing(_open_source(source_path, immutable=final)) as source):
        # One read transaction: a live run sees one state of a file the bot keeps writing.
        source.execute("BEGIN")
        with connection_scope(db):
            if db.execute_sql("SELECT to_regprocedure('pg_stat_force_next_flush()')").fetchone()[0] is None:
                raise RuntimeError("The catch-up needs PostgreSQL 15 or later.")
            checked: dict | None = None  # the import record, once its baseline was checked
            try:
                with db.atomic():
                    db.execute_sql("SET LOCAL lock_timeout = '5s'")
                    db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                    manifest = _read_import(db)
                    if manifest is None:
                        raise RuntimeError("PostgreSQL has no import record; import tgdata.db first with migrate_db.")
                    models = MODELS + _cache_models(source, manifest)
                    _validate_source(source, models)
                    _validate_source(base, MODELS + _cache_models(base, manifest))
                    columns = _recovery_columns(models, manifest)
                    _validate_recovery_extras(source, db, columns)
                    models = tuple(model for model in models if model._meta.table_name in columns)
                    db.execute_sql(f"LOCK TABLE {', '.join(_quote(table) for table in columns)} IN SHARE ROW EXCLUSIVE MODE")
                    previous = manifest.get(CATCH_UP_KEY, {})
                    _check_target(db, manifest, columns, previous.get("baseline"))
                    checked = manifest if previous.get("baseline") is not None else None

                    base_extent = {table: _extent(base, table) for table in (WINDOWED, APPEND_ONLY) if table in columns}
                    stat = base_path.stat()
                    identity = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                "max_rowid": {table: top for table, (_, top) in base_extent.items()}}
                    if "base" in previous:
                        if previous["base"] != identity:
                            raise RuntimeError("The base file differs from the one earlier catch-ups used; refusing to catch up.")
                    elif any(manifest["tables"][table]["rows"] != count for table, (count, _) in base_extent.items()):
                        raise RuntimeError("The base file is not the file this target was imported from; refusing to catch up.")
                    counts = {}
                    for table, (count, top) in base_extent.items():
                        new_count, new_top = _extent(source, table)
                        added = source.execute(f"SELECT count(*) FROM {_quote(table)} WHERE rowid > ?", (top,)).fetchone()[0]
                        if new_top < top or new_count != count + added:
                            raise RuntimeError(
                                f"{table} of tgdata.db has {new_count} rows up to rowid {new_top}; the base file has "
                                f"{count} up to rowid {top}, and {added} were added after it. Rows were deleted or "
                                "renumbered (for example by VACUUM); refusing to catch up."
                            )
                        counts[table] = new_count

                    with db.bind_ctx(models):
                        updated = set(previous.get("updated", []))
                        changed: set = set()
                        start = None
                        if WINDOWED in columns:
                            fields = _fields(MsgLog, columns[WINDOWED])
                            key = [field.column_name for field in fields].index("master_msg_id")
                            start = _window_start(base, base_extent[WINDOWED][1], window_days)
                            rows = _changed_messages(base, source, fields, start, base_extent[WINDOWED][1], updated, changed)
                            inserted = rewritten = 0
                            for batch in chunked((tuple(_value(field, value) for field, value in zip(fields, row))
                                                  for row in rows), batch_size):
                                written = _write_rows(db, MsgLog, fields, batch, update=True)
                                expected = {row[key]: row for row in batch}
                                stored = {row[key]: row for row in (
                                    tuple(_value(field, value) for field, value in zip(fields, values))
                                    for values in db.execute_sql(
                                        f"SELECT {', '.join(_quote(field.column_name) for field in fields)} FROM msglog "
                                        "WHERE master_msg_id = ANY(%s)", ([message for message, _ in written],),
                                    )
                                )}
                                if any(stored.get(message) != expected[message] for message, _ in written):
                                    raise RuntimeError("Read-back of the written msglog rows failed; nothing was committed.")
                                inserted += sum(1 for _, new in written if new)
                                rewritten += sum(1 for _, new in written if not new)
                            summary[WINDOWED] = {"inserted": inserted, "updated": rewritten}
                        if APPEND_ONLY in columns:
                            fields = _fields(MsgLogMember, columns[APPEND_ONLY])
                            rows = (tuple(_value(field, value) for field, value in zip(fields, values))
                                    for _, *values in _rowid_rows(source, APPEND_ONLY, fields, base_extent[APPEND_ONLY][1] + 1))
                            summary[APPEND_ONLY] = {"inserted": sum(
                                len(_write_rows(db, MsgLogMember, fields, batch, update=False))
                                for batch in chunked(rows, batch_size)
                            )}
                        for model in models:
                            table = model._meta.table_name
                            if table in (WINDOWED, APPEND_ONLY):
                                continue
                            fields = _fields(model, columns[table])
                            wanted = list(_source_rows(source, model, columns[table]))
                            counts[table] = len(wanted)
                            deleted = replaced = 0
                            if _target_rows(db, model, fields) != wanted:
                                deleted = db.execute_sql(f"DELETE FROM {_quote(table)}").rowcount
                                for batch in chunked(wanted, batch_size):
                                    model.insert_many(batch, fields=fields).execute()
                                _reset_sequence(db, model)
                                if _target_rows(db, model, fields) != wanted:
                                    raise RuntimeError(f"Read-back of {table} failed; nothing was committed.")
                                replaced = len(wanted)
                            summary[table] = {"deleted": deleted, "replaced": replaced}
                    for table in columns:
                        stored = db.execute_sql(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0]
                        if stored != counts[table]:
                            raise RuntimeError(f"{table}: {stored} rows in PostgreSQL, {counts[table]} in tgdata.db; nothing was committed.")

                    catch = {"base": identity, "updated": sorted(updated | changed)}
                    if final:
                        record = {
                            "version": 2, "import_id": manifest["import_id"],
                            "tables": {table: {"rows": counts[table], "sha256": ""} for table in columns},
                            "columns": columns, "queue": None, "backup_queue": None,
                            "backup_directory": str(directory), CATCH_UP_KEY: catch,
                        }
                    else:
                        record = dict(manifest, **{CATCH_UP_KEY: catch})
                    wrote = any(any(values.values()) for values in summary.values())
                    if _comparable(record) != _comparable(manifest):
                        if final:
                            if (directory / RECEIPT_FILE).exists():
                                raise RuntimeError(
                                    "A cutover receipt for an earlier state of this target exists; refusing to replace it."
                                )
                            record["import_id"] = uuid.uuid4().hex
                    elif not wrote:
                        record = manifest
                    if record is not manifest:
                        db.execute_sql(f"UPDATE {IMPORT_TABLE} SET manifest = %s WHERE id = 1", (_json(record),))
            except BaseException:
                if checked is not None:
                    # The rollback restored the rows just checked, but PostgreSQL keeps counting
                    # the rolled-back writes; record the new counters so a retry is not refused.
                    with suppress(Exception):
                        db.execute_sql("SELECT pg_stat_force_next_flush()")
                        with db.atomic():
                            counters = _write_counters(db, columns)
                            baseline = checked[CATCH_UP_KEY]["baseline"]
                            fresh = {table: [baseline[table][0], *counters[table]] for table in columns}
                            if fresh != baseline:
                                record = dict(checked, **{CATCH_UP_KEY: dict(checked[CATCH_UP_KEY], baseline=fresh)})
                                db.execute_sql(f"UPDATE {IMPORT_TABLE} SET manifest = %s WHERE id = 1", (_json(record),))
                raise
            if record is not manifest:
                # The next run compares against counters that include this run's writes;
                # a backend reports its counters only when it goes idle.
                db.execute_sql("SELECT pg_stat_force_next_flush()")
                with db.atomic():
                    counters = _write_counters(db, columns)
                    record[CATCH_UP_KEY]["baseline"] = {table: [counts[table], *counters[table]] for table in columns}
                    db.execute_sql(f"UPDATE {IMPORT_TABLE} SET manifest = %s WHERE id = 1", (_json(record),))
    for table, values in summary.items():
        logger.info("Catch-up %s: %s", table, ", ".join(f"{name} {count}" for name, count in values.items()))
    return record, {"mode": "final" if final else "live", "import_id": record["import_id"],
                    "window_start_rowid": start, "tables": summary}


def catch_up(directory: Path, config: dict, base: Path, *, final: bool = False, window_days: int = 7,
             batch_size: int = 500) -> dict:
    """Bring an imported PostgreSQL target up to date with the data directory's newer tgdata.db.

    ``base`` is the SQLite file the target was imported from. Message logs are compared
    with it only from ``window_days`` before its newest message on, plus every row added
    after it; the other tables are copied whole. A live run reads tgdata.db while the
    bot runs, in one read transaction. The ``final`` run needs the bot stopped; it
    rewrites the import record and writes the cutover receipt. Neither file is written.
    """
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if window_days < 0:
        raise ValueError("window_days must not be negative")
    if config.get("type") != "postgresql":
        raise ValueError("Catch-up requires database.type: postgresql in the supplied configuration")
    directory = directory.resolve()
    source_path = directory / "tgdata.db"
    for path in (base, source_path):
        if not path.is_file():
            raise FileNotFoundError(f"SQLite file does not exist: {path}")
    db = postgresql_database(config)
    with DataDirectoryLock(directory) if final else nullcontext():
        wal = source_path.with_name(source_path.name + "-wal")
        if final and wal.exists() and wal.stat().st_size:
            raise RuntimeError(
                "tgdata.db-wal is not empty: stop the bot and every other writer and let them close the "
                "database before the final catch-up."
            )
        try:
            record, summary = _catch_up_target(db, directory, base, source_path, final, window_days, batch_size)
        finally:
            db.close_all()
        if final:
            # Written after every connection to tgdata.db has closed.
            _write_receipt(directory / RECEIPT_FILE, dict(record, source_fingerprint=_fingerprint(source_path)))
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="Channel data directory (tgdata.db); stop all writers first for --final")
    parser.add_argument("--config", type=Path, required=True, help="YAML with the target database mapping (password is never printed)")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--catch-up", type=Path, metavar="BASE", required=True,
                        help="Bring the PostgreSQL import up to date with tgdata.db; BASE is the SQLite file it was imported from")
    parser.add_argument("--final", action="store_true",
                        help="The last run, with the bot stopped; writes the cutover receipt")
    parser.add_argument("--window-days", type=int,
                        help="Days before BASE's newest message in which messages may have been edited (default 7)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        print(_json(catch_up(args.data_dir, _read_database_config(args.config), args.catch_up, final=args.final,
                             window_days=7 if args.window_days is None else args.window_days,
                             batch_size=args.batch_size)))
    except Exception as error:
        parser.exit(1, f"Migration failed: {error}\nOriginal SQLite files were not deleted.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
