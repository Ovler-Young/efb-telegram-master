"""Offline, verified SQLite -> PostgreSQL import. Never deletes a source file.

Run ``python -m efb_telegram_master.migrate_db --help``. The bot and all other
writers must be stopped. The existing outbound SQLite queue remains in place.
"""

import argparse
import datetime
import hashlib
import io
import json
import logging
import os
import pickle
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import ExitStack, closing, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Optional

from peewee import (
    AutoField, BigIntegerField, BlobField, CharField, CompositeKey, DateTimeField, DoubleField, FloatField,
    IntegerField, Model, TextField, chunked,
)
from ruamel.yaml import YAML

from .db import ChatAssoc, DatabaseManager, HistoryMigrationEntry, MsgLog, SlaveChatInfo, TopicAssoc
from .db_runtime import SCHEMA_LOCK, DataDirectoryLock, connection_scope, current_schema, postgresql_database

MODELS = (ChatAssoc, TopicAssoc, SlaveChatInfo, MsgLog, HistoryMigrationEntry)
CACHE_TABLES = {"topiciconcache", "useremojicache"}
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
        not isinstance(manifest, dict) or manifest.get("version") not in (1, 2)
        or not isinstance(manifest.get("import_id"), str) or not manifest["import_id"]
        or not isinstance(manifest.get("tables"), dict)
        or not {model._meta.table_name for model in MODELS}.issubset(manifest["tables"])
        or set(manifest["tables"]) - {model._meta.table_name for model in MODELS} - CACHE_TABLES
        or (manifest.get("version") == 2 and (
            not isinstance(manifest.get("columns"), dict)
            or set(manifest["columns"]) != set(manifest["tables"])
        ))
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


def _cache_models(source: sqlite3.Connection, manifest=None) -> tuple:
    """Preserve only the two known historical caches, using their actual DDL."""
    models = []
    types = {"TEXT": TextField, "INTEGER": BigIntegerField, "INT": BigIntegerField,
             "BIGINT": BigIntegerField, "SMALLINT": BigIntegerField,
             "BLOB": BlobField, "DATETIME": DateTimeField, "TIMESTAMP": DateTimeField,
             "REAL": DoubleField, "DOUBLE": DoubleField, "FLOAT": DoubleField}
    for table in sorted(CACHE_TABLES & _source_tables(source)):
        if manifest is not None and manifest["version"] == 1 and table not in manifest["tables"]:
            # v1 accepted empty caches without importing them. Preserve that
            # projection only while there is still no unrecorded data.
            if source.execute(f"SELECT 1 FROM {_quote(table)} LIMIT 1").fetchone():
                raise RuntimeError(f"Unrecorded source cache {table!r} is no longer empty; source is preserved.")
            continue
        columns = list(source.execute(f"PRAGMA table_xinfo({_quote(table)})"))
        fields = {}
        primary = []
        for _, name, declared, not_null, default, pk, hidden in columns:
            declared = declared.upper()
            field_type = TextField if re.fullmatch(r"(?:VAR)?CHAR(?:\(\d+\))?", declared) else types.get(declared)
            if field_type is None or hidden:
                raise RuntimeError(f"Unsupported cache column {table}.{name}: {declared}; source is preserved.")
            fields[name] = field_type(column_name=name, null=not not_null)
            if pk:
                primary.append((pk, name))
        # Constraints beyond simple keys need an explicit conversion, not a guess.
        if source.execute(f"PRAGMA foreign_key_list({_quote(table)})").fetchone():
            raise RuntimeError(f"Unsupported foreign key in {table}; source is preserved.")
        meta = type("Meta", (), {"table_name": table, "primary_key":
                    CompositeKey(*(name for _, name in sorted(primary))) if primary else False})
        model = type(table, (Model,), dict(fields, Meta=meta))
        models.append(model)
    return tuple(models)


def _validate_source(source: sqlite3.Connection, models=MODELS) -> None:
    tables = _source_tables(source)
    if not {"msglog", "chatassoc", "slavechatinfo"}.issubset(tables):
        raise RuntimeError("Source is not a complete ETM database (msglog/chatassoc/slavechatinfo required).")
    known = {model._meta.table_name for model in models}
    for table in tables - known:
        if not table.startswith("sqlite_") and source.execute(f"SELECT 1 FROM {_quote(table)} LIMIT 1").fetchone():
            raise RuntimeError(f"Nonempty unsupported source table {table!r}; refusing to drop data during import.")
    for model in models:
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
    if isinstance(field, FloatField):
        if not isinstance(value, (int, float)):
            raise ValueError(f"Invalid real value in {field.name}")
        return float(value)
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


def _fields(model, columns=None):
    fields = model._meta.sorted_fields
    return fields if columns is None else [model._meta.columns[name] for name in columns]


def _order(model, fields, *, postgres=False):
    primary = model._meta.primary_key
    if isinstance(primary, CompositeKey):
        ordered = [model._meta.fields[name] for name in primary.field_names]
    elif primary:
        ordered = [primary]
    else:
        ordered = fields
    terms = []
    for field in ordered:
        term = _quote(field.column_name)
        if isinstance(field, TextField):
            term += ' COLLATE "C"' if postgres else " COLLATE BINARY"
        if postgres:
            term += " NULLS FIRST"
        terms.append(term)
    return ", ".join(terms)


def _source_rows(source: sqlite3.Connection, model, columns=None) -> Iterator[tuple]:
    table = model._meta.table_name
    if table not in _source_tables(source):
        return
    actual = {row[1] for row in source.execute(f"PRAGMA table_info({_quote(table)})")}
    fields = _fields(model, columns)
    projection = ", ".join(_quote(field.column_name) if field.column_name in actual else "NULL" for field in fields)
    order = _order(model, fields)
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


def _external_payload(payload: bytes, visit) -> bytes:
    """Visit references inside a v2 pickle without opening a live queue manager."""
    if not payload or payload[0] != 2:
        return payload
    from .outbound import OutboundQueue, _StoredMediaSnapshot

    changed = False

    class MediaPickler(pickle.Pickler):
        def reducer_override(self, value):
            nonlocal changed
            if isinstance(value, _StoredMediaSnapshot) and value.external_uri is not None:
                replacement = visit(value)
                changed = changed or replacement is not value
                return replacement.__reduce_ex__(5)
            return NotImplemented

    value = OutboundQueue.decode_payload_raw(payload)
    stream = io.BytesIO()
    MediaPickler(stream, protocol=5).dump(value)
    return b"\x02" + stream.getvalue() if changed else payload


def _external_path(reference) -> Path:
    from .outbound import OutboundQueue

    path = OutboundQueue._local_media_path(reference.external_uri)
    if path is None or not path.is_file():
        raise RuntimeError("External queued media is missing or unsupported; backup is incomplete.")
    return path


def _file_digest(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _backup_queue(source: Path, archive: Path) -> Optional[dict]:
    before = _queue_digest(source)
    if before is None:
        return None
    destination = archive / source.name
    _backup(source, destination)
    media = archive / "outbound-media"
    _backup_media_directory(source.parent / "outbound-media", media)
    external_index = 0

    def copy_external(reference):
        nonlocal external_index
        path = _external_path(reference)
        expected = _file_digest(path)
        media.mkdir(mode=0o700, exist_ok=True)
        # Stable row/reference order gives unchanged retries the same archived
        # payload and media digest, while preserving existing sidecar names.
        while True:
            external_index += 1
            name = f"external-{external_index}"
            target = media / name
            if not target.exists():
                break
        with path.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        if _file_digest(target) != expected or _file_digest(path) != expected:
            raise RuntimeError("External queued media changed during backup; keep all writers stopped and retry.")
        return replace(reference, storage_name=name, external_uri=None, cleanup_external=False)

    with closing(sqlite3.connect(destination)) as queue, queue:
        if "outbound_queue" in _source_tables(queue):
            for row_id, payload in queue.execute("SELECT rowid, payload FROM outbound_queue ORDER BY rowid"):
                restored = _external_payload(payload, copy_external)
                if restored != payload:
                    queue.execute("UPDATE outbound_queue SET payload = ? WHERE rowid = ?", (restored, row_id))
    if media.exists():
        _sync_directory(media)
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())
    if _queue_digest(source) != before:
        raise RuntimeError("Outbound queue or media changed during backup; keep all writers stopped and retry.")
    return before


def _queue_digest(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    # Include all queue tables and columns, including future retry metadata.
    with closing(sqlite3.connect(path)) as source:
        digest = hashlib.sha256()
        external = hashlib.sha256()
        external_count = 0

        def hash_external(reference):
            nonlocal external_count
            _hash_row(external, (reference.external_uri, _json(_file_digest(_external_path(reference)))))
            external_count += 1
            return reference

        for table in sorted(_source_tables(source)):
            schema = source.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
            digest.update(_json([table, schema]).encode())
            columns = [row[1] for row in source.execute(f"PRAGMA table_info({_quote(table)})")]
            payload_index = columns.index("payload") if table == "outbound_queue" and "payload" in columns else None
            for row in source.execute(f"SELECT * FROM {_quote(table)} ORDER BY rowid"):
                _hash_row(digest, row)
                if payload_index is not None:
                    _external_payload(row[payload_index], hash_external)
        result = {
            "sha256": digest.hexdigest(),
            "media": _media_directory_digest(path.parent / "outbound-media"),
        }
        if external_count:
            result["external"] = {"references": external_count, "sha256": external.hexdigest()}
        return result


def _queue_digest_matches(current: Optional[dict], recorded: Optional[dict], *, legacy_external=False) -> bool:
    if current == recorded:
        return True
    # Imports committed before queue sidecars existed recorded only the SQLite
    # digest. Treat a missing/empty media directory as the same historical state.
    if not isinstance(current, dict) or not isinstance(recorded, dict):
        return False
    if legacy_external and "external" not in recorded:
        current = {key: value for key, value in current.items() if key != "external"}
        if current == recorded:
            return True
    if "external" in current or "media" in recorded or current.get("sha256") != recorded.get("sha256"):
        return False
    media = current.get("media")
    return media is None or (
        isinstance(media, dict)
        and media.get("files") == 0
        and media.get("bytes") == 0
    )


def _target_digest(db, model, batch_size: int, columns=None) -> dict:
    fields = _fields(model, columns)
    projection = ", ".join(_quote(field.column_name) for field in fields)
    order = _order(model, fields, postgres=True)
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


def _import_rows(db, source: sqlite3.Connection, batch_size: int, models=MODELS) -> dict:
    summaries = {}
    for model in models:
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


def _recovery_columns(source, models, manifest):
    columns = {model._meta.table_name: [field.column_name for field in model._meta.sorted_fields] for model in models}
    if set(columns) != set(manifest["tables"]):
        raise RuntimeError("Source tables changed since the committed import.")
    if manifest["version"] == 1:
        # v1 hashes predate this optional field. Verify their original projection,
        # but never let that projection hide newly populated source data.
        columns["msglog"].remove("master_message_thread_id")
        actual = {row[1] for row in source.execute('PRAGMA table_info("msglog")')}
        if "master_message_thread_id" in actual and source.execute(
            'SELECT 1 FROM msglog WHERE master_message_thread_id IS NOT NULL LIMIT 1'
        ).fetchone():
            raise RuntimeError("Source has unimported message thread IDs; preserving both databases.")
    elif columns != manifest["columns"]:
        raise RuntimeError("Source columns changed since the committed import.")
    return columns


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
                if queue_path.exists():
                    fences.enter_context(_fence(queue_path))
                backup_root = directory / "database-backups"
                backup_root.mkdir(mode=0o700, exist_ok=True)
                archive = Path(tempfile.mkdtemp(prefix="sqlite-", dir=backup_root))
                _backup(source_path, archive / source_path.name)
                queue_summary = _backup_queue(queue_path, archive)
                for path in (archive, backup_root, directory):
                    _sync_directory(path)
                logger.info("Consistent SQLite backups: %s", archive)
                with closing(sqlite3.connect(archive / source_path.name)) as source:
                    with ExitStack() as bindings, connection_scope(db), db.atomic():
                        db.execute_sql("SET LOCAL lock_timeout = '5s'")
                        db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                        manifest = _read_import(db)
                        models = MODELS + _cache_models(source, manifest)
                        _validate_source(source, models)
                        bindings.enter_context(db.bind_ctx(models))
                        receipt_path = directory / RECEIPT_FILE
                        if receipt_path.exists():
                            previous = json.loads(receipt_path.read_text())
                            if manifest is None or previous.get("import_id") != manifest.get("import_id"):
                                raise RuntimeError("Existing cutover receipt does not match this target; refusing another import.")
                        if manifest is None:
                            if db.get_tables(schema=current_schema(db)):
                                raise RuntimeError("Target schema is not empty and has no verified import record; refusing to overwrite or skip SQLite.")
                            db.create_tables(models)
                            tables = _import_rows(db, source, batch_size, models)
                            columns = {model._meta.table_name: [field.column_name for field in model._meta.sorted_fields]
                                       for model in models}
                            DatabaseManager._create_lookup_indexes(db)
                            manifest = {
                                "version": 2, "import_id": uuid.uuid4().hex,
                                "tables": tables, "columns": columns, "queue": queue_summary,
                                "backup_queue": _queue_digest(archive / queue_path.name),
                                "backup_directory": str(archive),
                            }
                        else:
                            # Recovery must not verify different tables at different
                            # points in a concurrent target writer's transaction.
                            names = ", ".join(_quote(name) for name in manifest["tables"])
                            db.execute_sql(f"LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE")
                            columns = _recovery_columns(source, models, manifest)
                            tables = {model._meta.table_name: _digest(_source_rows(source, model, columns[model._meta.table_name]))
                                      for model in models}
                            queue_matches = _queue_digest_matches(
                                queue_summary, manifest["queue"], legacy_external=manifest["version"] == 1
                            ) or ("backup_queue" in manifest and queue_summary == manifest["backup_queue"])
                            if tables != manifest["tables"] or not queue_matches:
                                raise RuntimeError("Source changed since the committed import; refusing to merge divergent databases.")
                            if (queue_summary is not None and "external" in queue_summary
                                    and "external" not in manifest["queue"]):
                                logger.warning(
                                    "Legacy import has no external-media content hashes. Verified current files and "
                                    "established their first content baseline; pre-existing changes cannot be detected."
                                )
                            manifest = dict(manifest, queue=queue_summary,
                                            backup_queue=_queue_digest(archive / queue_path.name),
                                            backup_directory=str(archive))
                        for model in models:
                            if _target_digest(db, model, batch_size, columns[model._meta.table_name]) != tables[model._meta.table_name]:
                                raise RuntimeError(f"Content verification failed for {model._meta.table_name}; both sources are preserved.")
                        if _queue_digest(queue_path) != queue_summary:
                            raise RuntimeError("Outbound queue or media changed during import; keep all writers stopped and retry.")
                        db.execute_sql(f"CREATE TABLE IF NOT EXISTS {IMPORT_TABLE} (id INTEGER PRIMARY KEY CHECK (id = 1), manifest TEXT NOT NULL)")
                        db.execute_sql(f"INSERT INTO {IMPORT_TABLE} (id, manifest) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET manifest = EXCLUDED.manifest", (_json(manifest),))
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
