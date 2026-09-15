"""Offline, verified SQLite -> PostgreSQL import. Never deletes a source file.

Run ``python -m efb_telegram_master.migrate_db --help``. The bot and all other
writers must be stopped. The existing outbound SQLite queue remains in place.
"""

import argparse
import datetime
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Iterator, Optional

from peewee import AutoField, BlobField, CharField, DateTimeField, IntegerField, TextField, chunked
from ruamel.yaml import YAML

from .db import ChatAssoc, DatabaseManager, HistoryMigrationEntry, MsgLog, SlaveChatInfo, TopicAssoc
from .db_runtime import SCHEMA_LOCK, DataDirectoryLock, connection_scope, current_schema, postgresql_database

MODELS = (ChatAssoc, TopicAssoc, SlaveChatInfo, MsgLog, HistoryMigrationEntry)
IMPORT_TABLE = "etm_sqlite_import"
RECEIPT_FILE = ".postgresql-cutover.json"
logger = logging.getLogger(__name__)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _fingerprint(path: Path) -> dict:
    """Conservative, cheap change detection for the retained, now-frozen source."""
    result = {}
    for candidate in (path, path.with_name(path.name + "-wal")):
        if candidate.exists():
            stat = candidate.stat()
            result[candidate.name] = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    return result


def _read_import(db) -> Optional[dict]:
    tables = set(db.get_tables(schema=current_schema(db)))
    if IMPORT_TABLE not in tables:
        return None
    rows = db.execute_sql(f"SELECT manifest FROM {IMPORT_TABLE} WHERE id = 1").fetchall()
    if len(rows) != 1:
        raise RuntimeError("Invalid PostgreSQL import record; refusing to infer migration success.")
    manifest = json.loads(rows[0][0])
    if (
        not isinstance(manifest, dict) or manifest.get("version") != 1
        or not isinstance(manifest.get("import_id"), str) or not manifest["import_id"]
        or not isinstance(manifest.get("tables"), dict)
        or set(manifest["tables"]) != {model._meta.table_name for model in MODELS}
        or "queue" not in manifest
    ):
        raise RuntimeError("Unsupported or corrupt PostgreSQL import record; preserving both databases.")
    missing = set(manifest["tables"]) - tables
    if missing:
        raise RuntimeError(
            f"Imported PostgreSQL tables are missing: {sorted(missing)}; "
            "refusing to recreate empty replacements. Restore the complete target."
        )
    return manifest


def validate_runtime_cutover(directory: Path, db) -> None:
    """Fail closed on a missing import, wrong target, or reused SQLite source."""
    receipt_path = directory / RECEIPT_FILE
    source_path = directory / "tgdata.db"
    if db is None:
        if receipt_path.exists():
            raise RuntimeError("This directory was cut over to PostgreSQL; refusing to resume its stale SQLite database.")
        return
    manifest = _read_import(db)
    if not source_path.exists() and not receipt_path.exists() and manifest is None:
        return  # A native PostgreSQL deployment, not an implicit import.
    if manifest is None or not receipt_path.exists():
        raise RuntimeError(
            "SQLite data requires an explicit offline import: run python -m "
            "efb_telegram_master.migrate_db --data-dir <directory> --config <postgresql.yaml>. "
            "Existing PostgreSQL tables do not prove that SQLite was imported."
        )
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("import_id") != manifest.get("import_id"):
        raise RuntimeError("Cutover receipt belongs to a different PostgreSQL import; preserving both databases.")
    if manifest["queue"] is not None and not (directory / "outbound-queue.sqlite3").is_file():
        raise RuntimeError(
            "The imported outbound queue is missing; refusing to replace pending sends and "
            "Telegram receipts with an empty queue. Restore the current queue before startup."
        )
    if source_path.exists() and receipt.get("source_fingerprint") != _fingerprint(source_path):
        raise RuntimeError("SQLite source changed after import; refusing an ambiguous cutover. Keep both databases offline.")


@contextmanager
def _fence(path: Path) -> Iterator[sqlite3.Connection]:
    # A reserved write lock also fences older clients that do not use our flock.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _backup(path: Path, destination: Path) -> None:
    """Use a separate reader: backing up the write-lock connection would block."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
        with closing(sqlite3.connect(destination)) as archive:
            reader.backup(archive, pages=256)
            if archive.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError(f"Backup integrity check failed: {destination.name}")
    # The original main file, WAL, SHM and any older archives are untouched.
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _source_tables(source: sqlite3.Connection) -> set[str]:
    return {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _validate_source(source: sqlite3.Connection) -> None:
    tables = _source_tables(source)
    if not {"msglog", "chatassoc", "slavechatinfo"}.issubset(tables):
        raise RuntimeError("Source is not a complete ETM database (msglog/chatassoc/slavechatinfo required).")
    known = {model._meta.table_name for model in MODELS}
    for table in tables - known:
        if not table.startswith("sqlite_") and source.execute(f"SELECT 1 FROM {_quote(table)} LIMIT 1").fetchone():
            raise RuntimeError(f"Nonempty unsupported source table {table!r}; refusing to drop data during import.")
    for model in MODELS:
        table = model._meta.table_name
        if table not in tables:
            continue
        actual = {row[1] for row in source.execute(f"PRAGMA table_info({_quote(table)})")}
        fields = {field.column_name: field for field in model._meta.sorted_fields}
        unknown = actual - fields.keys()
        if unknown:
            raise RuntimeError(f"Unsupported columns in {table}: {sorted(unknown)}; source is preserved.")
        missing = [name for name, field in fields.items() if name not in actual and not field.null]
        if missing:
            raise RuntimeError(f"Missing required columns in {table}: {missing}")


def _value(field, value):
    if value is None:
        if not field.null:
            raise ValueError(f"NULL in required field {field.name}")
        return None
    if isinstance(field, BlobField):
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise ValueError(f"Invalid binary value in {field.name}")
        return bytes(value)
    if isinstance(field, DateTimeField):
        value = field.python_value(value)
        if not isinstance(value, datetime.datetime) or value.tzinfo is not None:
            raise ValueError(f"Invalid or timezone-aware timestamp in {field.name}; conversion must be explicit")
        return value
    if isinstance(field, IntegerField):
        converted = int(value)
        if isinstance(value, float) and value != converted:
            raise ValueError(f"Non-integer value in {field.name}")
        return converted
    if isinstance(field, (TextField, CharField)):
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"Invalid PostgreSQL text in {field.name}")
        return value
    raise TypeError(f"Unsupported field type: {type(field).__name__}")


def _source_rows(source: sqlite3.Connection, model) -> Iterator[tuple]:
    table = model._meta.table_name
    if table not in _source_tables(source):
        return
    actual = {row[1] for row in source.execute(f"PRAGMA table_info({_quote(table)})")}
    fields = model._meta.sorted_fields
    projection = ", ".join(_quote(field.column_name) if field.column_name in actual else "NULL" for field in fields)
    primary_key = model._meta.primary_key
    order = _quote(primary_key.column_name)
    if isinstance(primary_key, TextField):
        order += " COLLATE BINARY"
    cursor = source.execute(f"SELECT {projection} FROM {_quote(table)} ORDER BY {order}")
    try:
        for row in cursor:
            yield tuple(_value(field, value) for field, value in zip(fields, row))
    finally:
        cursor.close()


def _hash_row(digest, row) -> None:
    # Length-prefixed fields make boundaries unambiguous without hex/JSON copies
    # of media BLOBs. Memory is bounded by a batch, or one queue row, not a table.
    for value in row:
        if isinstance(value, (bytes, memoryview)):
            tag, content = b"B", value
        elif isinstance(value, datetime.datetime):
            tag, content = b"T", value.isoformat(timespec="microseconds").encode()
        else:
            tag, content = b"J", _json(value).encode()
        digest.update(tag + str(len(content)).encode() + b":")
        digest.update(content)
    digest.update(b"\n")


def _digest(rows) -> dict:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        _hash_row(digest, row)
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def _media_directory_digest(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    if not path.is_dir():
        raise RuntimeError(f"Outbound media path is not a directory: {path}")
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for candidate in sorted(path.iterdir(), key=lambda item: item.name):
        if candidate.is_symlink() or not candidate.is_file():
            raise RuntimeError(f"Unexpected outbound media entry: {candidate.name}")
        stat = candidate.stat()
        digest.update(_json([candidate.name, stat.st_size]).encode())
        with candidate.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        files += 1
        total_bytes += stat.st_size
    return {"files": files, "bytes": total_bytes, "sha256": digest.hexdigest()}


def _backup_media_directory(source: Path, destination: Path) -> None:
    before = _media_directory_digest(source)
    if before is None:
        return
    destination.mkdir(mode=0o700)
    for candidate in sorted(source.iterdir(), key=lambda item: item.name):
        if candidate.is_symlink() or not candidate.is_file():
            raise RuntimeError(f"Unexpected outbound media entry: {candidate.name}")
        target = destination / candidate.name
        with candidate.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
    _sync_directory(destination)
    after = _media_directory_digest(source)
    copied = _media_directory_digest(destination)
    if before != after or copied != after:
        raise RuntimeError("Outbound media changed during backup; keep all writers stopped and retry.")


def _queue_digest(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    # Include all queue tables and columns, including future retry metadata.
    with closing(sqlite3.connect(path)) as source:
        digest = hashlib.sha256()
        for table in sorted(_source_tables(source)):
            schema = source.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
            digest.update(_json([table, schema]).encode())
            for row in source.execute(f"SELECT * FROM {_quote(table)} ORDER BY rowid"):
                _hash_row(digest, row)
        return {
            "sha256": digest.hexdigest(),
            "media": _media_directory_digest(path.parent / "outbound-media"),
        }


def _queue_digest_matches(current: Optional[dict], recorded: Optional[dict]) -> bool:
    if current == recorded:
        return True
    # Imports committed before queue sidecars existed recorded only the SQLite
    # digest. Treat a missing/empty media directory as the same historical state.
    if not isinstance(current, dict) or not isinstance(recorded, dict):
        return False
    if "media" in recorded or current.get("sha256") != recorded.get("sha256"):
        return False
    media = current.get("media")
    return media is None or (
        isinstance(media, dict)
        and media.get("files") == 0
        and media.get("bytes") == 0
    )


def _target_digest(db, model, batch_size: int) -> dict:
    fields = model._meta.sorted_fields
    projection = ", ".join(_quote(field.column_name) for field in fields)
    order = _quote(model._meta.primary_key.column_name)
    if isinstance(model._meta.primary_key, TextField):
        order += ' COLLATE "C"'  # Same byte ordering as SQLite BINARY, independent of locale.
    # A normal psycopg2 cursor buffers the entire result even with fetchmany().
    if not db.in_transaction():
        raise RuntimeError("Content verification requires the import transaction")
    # Peewee issues BEGIN explicitly while leaving psycopg2.autocommit enabled.
    # withhold avoids psycopg2's client-side autocommit guard; this cursor is
    # always closed before COMMIT, so it is never materialized across commits.
    with db.connection().cursor(name="etm_verify_" + uuid.uuid4().hex, withhold=True) as cursor:
        cursor.itersize = batch_size
        cursor.execute(f"SELECT {projection} FROM {_quote(model._meta.table_name)} ORDER BY {order}")
        return _digest(tuple(_value(field, value) for field, value in zip(fields, row)) for row in cursor)


def _import_rows(db, source: sqlite3.Connection, batch_size: int) -> dict:
    summaries = {}
    for model in MODELS:
        count = 0
        digest = hashlib.sha256()
        for batch in chunked(_source_rows(source, model), batch_size):
            model.insert_many(batch, fields=model._meta.sorted_fields).execute()
            for row in batch:
                _hash_row(digest, row)
            count += len(batch)
        summaries[model._meta.table_name] = {"rows": count, "sha256": digest.hexdigest()}
        if isinstance(model._meta.primary_key, AutoField):
            table = model._meta.table_name
            pk = model._meta.primary_key.column_name
            db.execute_sql(
                f"SELECT setval(pg_get_serial_sequence(%s, %s), "
                f"GREATEST(COALESCE(MAX({_quote(pk)}), 1), 1), "
                f"COALESCE(MAX({_quote(pk)}) >= 1, FALSE)) FROM {_quote(table)}",
                (table, pk),
            )
        logger.info("Imported %s: %d rows", model._meta.table_name, count)
    return summaries


def _write_receipt(path: Path, receipt: dict) -> None:
    if path.exists() and json.loads(path.read_text()).get("import_id") != receipt["import_id"]:
        raise RuntimeError("An existing cutover receipt belongs to another import; refusing to overwrite it.")
    descriptor, name = tempfile.mkstemp(prefix=".cutover-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(_json(receipt) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def migrate(directory: Path, config: dict, batch_size: int = 500) -> dict:
    """Import an empty target, or verify/recover an already-committed import.

    The transaction includes all rows, indexes, sequence resets, verification
    and provenance. After a post-commit crash, re-running verifies both sides
    before recreating the local cutover receipt; no rows are inserted twice.
    """
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if config.get("type") != "postgresql":
        raise ValueError("Migration requires database.type: postgresql in the supplied configuration")
    directory = directory.resolve()
    source_path = directory / "tgdata.db"
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source_path}")
    db = postgresql_database(config)
    with DataDirectoryLock(directory):
        try:
            with ExitStack() as fences:
                fences.enter_context(_fence(source_path))
                queue_path = directory / "outbound-queue.sqlite3"
                queue_media_path = directory / "outbound-media"
                if queue_path.exists():
                    fences.enter_context(_fence(queue_path))
                backup_root = directory / "database-backups"
                backup_root.mkdir(mode=0o700, exist_ok=True)
                archive = Path(tempfile.mkdtemp(prefix="sqlite-", dir=backup_root))
                _backup(source_path, archive / source_path.name)
                if queue_path.exists():
                    _backup(queue_path, archive / queue_path.name)
                    _backup_media_directory(queue_media_path, archive / queue_media_path.name)
                for path in (archive, backup_root, directory):
                    _sync_directory(path)
                logger.info("Consistent SQLite backups: %s", archive)
                with closing(sqlite3.connect(archive / source_path.name)) as source:
                    _validate_source(source)
                    queue_summary = _queue_digest(archive / queue_path.name)
                    with connection_scope(db), db.bind_ctx(MODELS), db.atomic():
                        db.execute_sql("SET LOCAL lock_timeout = '5s'")
                        db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                        manifest = _read_import(db)
                        receipt_path = directory / RECEIPT_FILE
                        if receipt_path.exists():
                            previous = json.loads(receipt_path.read_text())
                            if manifest is None or previous.get("import_id") != manifest.get("import_id"):
                                raise RuntimeError("Existing cutover receipt does not match this target; refusing another import.")
                        if manifest is None:
                            if db.get_tables(schema=current_schema(db)):
                                raise RuntimeError("Target schema is not empty and has no verified import record; refusing to overwrite or skip SQLite.")
                            db.create_tables(MODELS)
                            tables = _import_rows(db, source, batch_size)
                            DatabaseManager._create_lookup_indexes(db)
                            manifest = {
                                "version": 1, "import_id": uuid.uuid4().hex,
                                "tables": tables, "queue": queue_summary,
                                "backup_directory": str(archive),
                            }
                        else:
                            # Recovery must not verify different tables at different
                            # points in a concurrent target writer's transaction.
                            names = ", ".join(_quote(model._meta.table_name) for model in MODELS)
                            db.execute_sql(f"LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE")
                            tables = {model._meta.table_name: _digest(_source_rows(source, model)) for model in MODELS}
                            if tables != manifest["tables"] or not _queue_digest_matches(queue_summary, manifest["queue"]):
                                raise RuntimeError("Source changed since the committed import; refusing to merge divergent databases.")
                        for model in MODELS:
                            if _target_digest(db, model, batch_size) != tables[model._meta.table_name]:
                                raise RuntimeError(f"Content verification failed for {model._meta.table_name}; both sources are preserved.")
                        db.execute_sql(f"CREATE TABLE IF NOT EXISTS {IMPORT_TABLE} (id INTEGER PRIMARY KEY CHECK (id = 1), manifest TEXT NOT NULL)")
                        db.execute_sql(f"INSERT INTO {IMPORT_TABLE} (id, manifest) VALUES (1, %s) ON CONFLICT (id) DO NOTHING", (_json(manifest),))
            # SQLite locks/connections have closed, including any resulting WAL
            # checkpoint, so the recorded fingerprint is stable across startup.
            receipt = dict(manifest, source_fingerprint=_fingerprint(source_path))
            _write_receipt(archive / "manifest.json", receipt)
            _write_receipt(directory / RECEIPT_FILE, receipt)
            logger.info("Import verified. Retained source and outbound queue; switch configuration to PostgreSQL.")
            return receipt
        finally:
            db.close_all()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Channel data directory containing tgdata.db; stop all writers first")
    parser.add_argument("--config", type=Path, required=True, help="YAML with the target database mapping (password is never printed)")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        with args.config.open() as stream:
            config = YAML(typ="safe").load(stream)
        if not isinstance(config, dict) or not isinstance(config.get("database"), dict):
            raise ValueError("Configuration must contain a database mapping")
        result = migrate(args.data_dir, config["database"], args.batch_size)
        print(_json({"import_id": result["import_id"], "tables": result["tables"], "backup_directory": result["backup_directory"]}))
    except Exception as error:
        parser.exit(1, f"Migration failed: {error}\nOriginal SQLite files were not deleted.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
