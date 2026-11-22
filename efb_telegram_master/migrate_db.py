"""Offline, verified SQLite -> PostgreSQL import. Never deletes a source file.

Run ``python -m efb_telegram_master.migrate_db --help``. The bot and all other
writers must be stopped. The outbound queue moves too; its SQLite file and media
stay in place.
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
from collections import Counter
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Iterator, Optional

from peewee import (
    AutoField, BigIntegerField, BlobField, BooleanField, CharField, CompositeKey, DateTimeField, DoubleField, FloatField,
    IntegerField, Model, TextField, chunked,
)
from ruamel.yaml import YAML

from .db import ChatAssoc, ContentTextField, DatabaseManager, HistoryMigrationEntry, HistoryMigrationTarget, MsgLog, MsgLogMember, SlaveChatInfo, TopicAssoc, replace_nul
from .db_runtime import SCHEMA_LOCK, DataDirectoryLock, connection_scope, current_schema, postgresql_database

CORE_MODELS = (ChatAssoc, TopicAssoc, SlaveChatInfo, MsgLog, HistoryMigrationEntry)
MODELS = CORE_MODELS + (HistoryMigrationTarget, MsgLogMember)
# Version 1 did not record its column projections in the manifest.
V1_COLUMNS = {
    "chatassoc": ["id", "master_uid", "slave_uid"],
    "topicassoc": ["id", "topic_chat_id", "message_thread_id", "slave_uid"],
    "slavechatinfo": ["id", "slave_channel_id", "slave_channel_emoji", "slave_chat_uid",
                      "slave_chat_group_id", "slave_chat_name", "slave_chat_alias", "slave_chat_type", "pickle"],
    "msglog": ["master_msg_id", "master_msg_id_alt", "slave_message_id", "text", "slave_origin_uid",
               "slave_origin_display_name", "slave_member_uid", "slave_member_display_name", "media_type",
               "mime", "file_id", "file_unique_id", "msg_type", "pickle", "sent_to", "sender_bot_id", "time"],
    "historymigrationentry": ["id", "slave_chat_id", "target_chat_id", "message_thread_id",
                              "source_master_msg_id", "formatted_text", "media_type", "source_time",
                              "position", "created_at"],
}
V1_OPTIONAL_COLUMNS = {"msglogmember": ["master_msg_id", "slave_origin_uid", "slave_message_id"]}
CACHE_TABLES = {"topiciconcache", "useremojicache"}
IMPORT_TABLE = "etm_sqlite_import"
RECEIPT_FILE = ".postgresql-cutover.json"
QUEUE_FILE = "outbound-queue.sqlite3"
QUEUE_IMPORT_TABLE = "etm_queue_import"
# Import record key of the catch-up: the base file, the keys it updated and the write baseline.
CATCH_UP_KEY = "catch_up"
# Found by SQLite rowid on catch-up; every other recorded table is copied whole.
WINDOWED, APPEND_ONLY = "msglog", "msglogmember"
# Marks a queue column that every released queue has; other columns were added
# later and take this default when an older queue lacks them.
_REQUIRED = object()
# Each queue table's columns as PostgreSQL stores them: (name, type, nullable, default).
QUEUE_COLUMNS = {
    "outbound_queue": (
        ("id", "bigint", False, _REQUIRED), ("priority", "integer", False, _REQUIRED),
        ("telegram_chat_id", "bigint", False, _REQUIRED), ("operation", "text", False, _REQUIRED),
        ("payload", "bytea", False, _REQUIRED), ("slave_id", "text", True, _REQUIRED),
        ("required_sender_bot_id", "text", True, _REQUIRED), ("created_at", "double", False, _REQUIRED),
        ("log_context", "bytea", True, None), ("delivery_state", "text", False, "queued"),
        ("completion_receipt", "bytea", True, None), ("reconcile_after", "double", False, 0.0),
        ("reconcile_attempts", "integer", False, 0), ("delivery_hold", "text", True, None),
        ("attempt_sender_bot_id", "text", True, None), ("attempt_started_at", "double", True, None),
        ("predecessor_id", "bigint", True, None), ("thread_id", "bigint", True, None),
    ),
    "history_ownership": (("entry_key", "text", False, _REQUIRED),),
    "source_cancellations": (
        ("origin_uid", "text", False, _REQUIRED), ("source_id", "text", False, _REQUIRED),
        ("source_revision", "integer", False, _REQUIRED), ("log_context", "bytea", False, _REQUIRED),
    ),
}
QUEUE_KEYS = {"outbound_queue": ("id",), "history_ownership": ("entry_key",),
              "source_cancellations": ("origin_uid", "source_id")}
_INTEGER_LIMITS = {"integer": 2**31, "bigint": 2**63}
# psycopg2 sends a BYTEA value hex-encoded inside one statement, which PostgreSQL
# caps at 1 GiB; this keeps one value, and the memory it needs, well below that.
MAX_QUEUE_VALUE_BYTES = 256 * 1024 * 1024
# One INSERT carries at most this many value bytes, whatever the row count; hex
# encoding and the statement copy need about five times this much memory.
QUEUE_BATCH_BYTES = 4 * 1024 * 1024
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
    # Version 3 is accepted because earlier builds wrote it; it is read as version 2.
    if (
        not isinstance(manifest, dict) or manifest.get("version") not in (1, 2, 3)
        or not isinstance(manifest.get("import_id"), str) or not manifest["import_id"]
        or not isinstance(manifest.get("tables"), dict)
        or not {model._meta.table_name for model in CORE_MODELS}.issubset(manifest["tables"])
        or set(manifest["tables"]) - {model._meta.table_name for model in MODELS} - CACHE_TABLES
        or (manifest.get("version") in (2, 3) and (
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
    if manifest["queue"] is not None and not (directory / QUEUE_FILE).is_file():
        raise RuntimeError(
            "The imported outbound queue is missing; refusing to replace pending sends and "
            "Telegram receipts with an empty queue. Restore the current queue before startup."
        )
    if source_path.exists() and receipt.get("source_fingerprint") != _fingerprint(source_path):
        raise RuntimeError("SQLite source changed after import; refusing an ambiguous cutover. Keep both databases offline.")


def _read_queue_import(db) -> dict | None:
    tables = set(db.get_tables(schema=current_schema(db)))
    if QUEUE_IMPORT_TABLE not in tables:
        return None
    rows = db.execute_sql(f"SELECT manifest FROM {QUEUE_IMPORT_TABLE} WHERE id = 1").fetchall()
    record = json.loads(rows[0][0]) if len(rows) == 1 else None
    if (
        not isinstance(record, dict) or record.get("version") != 1
        or not isinstance(record.get("import_id"), str) or not record["import_id"]
        or not isinstance(record.get("tables"), dict) or set(record["tables"]) != set(QUEUE_COLUMNS)
        or "queue" not in record or "backup_queue" not in record
    ):
        raise RuntimeError("Unsupported or corrupt PostgreSQL queue import record; preserving both queues.")
    missing = set(QUEUE_COLUMNS) - tables
    if missing:
        raise RuntimeError(
            f"Moved outbound queue tables are missing: {sorted(missing)}; "
            "refusing to recreate empty replacements. Restore the complete target."
        )
    return record


def validate_runtime_queue(directory: Path, db) -> None:
    """On PostgreSQL, refuse a queue that is still in SQLite or whose move is unfinished."""
    from .outbound import POSTGRESQL_QUEUE_MARKER

    record = _read_queue_import(db)
    if record is None:
        if (directory / QUEUE_FILE).exists():
            raise RuntimeError(
                f"The outbound queue is still in {QUEUE_FILE}. Stop the bot and move it with python -m "
                "efb_telegram_master.migrate_db --data-dir <directory> --config <postgresql.yaml> --queue-only; "
                "startup never moves it implicitly."
            )
        return
    marker = directory / POSTGRESQL_QUEUE_MARKER
    if not marker.exists() or json.loads(marker.read_text()).get("import_id") != record["import_id"]:
        raise RuntimeError(
            "The outbound queue move into PostgreSQL is unfinished or belongs to another data directory; "
            "run the same migrate_db command again to verify and finish it."
        )


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


def _source_columns(source: sqlite3.Connection, table: str) -> list:
    columns = list(source.execute(f"PRAGMA table_xinfo({_quote(table)})"))
    hidden = [row[1] for row in columns if row[6]]
    if hidden:
        raise RuntimeError(f"Unsupported generated or hidden columns in {table}: {hidden}; source is preserved.")
    return columns


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
        columns = _source_columns(source, table)
        fields = {}
        primary = []
        for _, name, declared, not_null, default, pk, hidden in columns:
            declared = declared.upper()
            field_type = TextField if re.fullmatch(r"(?:VAR)?CHAR(?:\(\d+\))?", declared) else types.get(declared)
            if field_type is None:
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
        actual = {row[1] for row in _source_columns(source, table)}
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
    if isinstance(field, BooleanField):
        if not isinstance(value, (bool, int)) or value not in (0, 1):
            raise ValueError(f"Invalid boolean value in {field.name}")
        return bool(value)
    if isinstance(field, IntegerField):
        converted = int(value)
        if isinstance(value, float) and value != converted:
            raise ValueError(f"Non-integer value in {field.name}")
        return converted
    if isinstance(field, ContentTextField) and isinstance(value, str):
        return replace_nul(value)
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


def _projection(source: sqlite3.Connection, table: str, fields) -> str:
    """Select ``fields``, as NULL where an older source lacks the column."""
    actual = {row[1] for row in _source_columns(source, table)}
    return ", ".join(_quote(field.column_name) if field.column_name in actual else "NULL" for field in fields)


def _source_rows(source: sqlite3.Connection, model, columns=None, replaced=None) -> Iterator[tuple]:
    table = model._meta.table_name
    if table not in _source_tables(source):
        return
    fields = _fields(model, columns)
    projection = _projection(source, table, fields)
    order = _order(model, fields)
    cursor = source.execute(f"SELECT {projection} FROM {_quote(table)} ORDER BY {order}")
    try:
        for row in cursor:
            if replaced is not None:
                for field, value in zip(fields, row):
                    if isinstance(field, ContentTextField) and isinstance(value, str) and "\x00" in value:
                        replaced[f"{table}.{field.column_name}"] += 1
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


def _media_payload(payload: bytes, visit) -> bytes:
    """Visit references inside a v2 pickle without opening a live queue manager."""
    if not payload or payload[0] != 2:
        return payload
    from .outbound import OutboundQueue, _StoredMediaSnapshot

    changed = False

    class MediaPickler(pickle.Pickler):
        def reducer_override(self, value):
            nonlocal changed
            if isinstance(value, _StoredMediaSnapshot):
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
        if reference.external_uri is None:
            return reference
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
                restored = _media_payload(payload, copy_external)
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

        def check_media(reference):
            nonlocal external_count
            if reference.external_uri is None:
                if reference.storage_name is None or not (path.parent / "outbound-media" / reference.storage_name).is_file():
                    raise RuntimeError("Local queued media is missing; backup is incomplete.")
                return reference
            _hash_row(external, (reference.external_uri, _json(_file_digest(_external_path(reference)))))
            external_count += 1
            return reference

        for table in sorted(_source_tables(source)):
            schema = source.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
            digest.update(_json([table, schema]).encode())
            columns = [row[1] for row in _source_columns(source, table)]
            payload_index = columns.index("payload") if table == "outbound_queue" and "payload" in columns else None
            for row in source.execute(f"SELECT * FROM {_quote(table)} ORDER BY rowid"):
                _hash_row(digest, row)
                if payload_index is not None:
                    _media_payload(row[payload_index], check_media)
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


def _reset_sequence(db, model) -> None:
    """Move an id sequence past the highest preserved id."""
    if isinstance(model._meta.primary_key, AutoField):
        table = model._meta.table_name
        pk = model._meta.primary_key.column_name
        db.execute_sql(
            f"SELECT setval(pg_get_serial_sequence(%s, %s), "
            f"GREATEST(COALESCE(MAX({_quote(pk)}), 1), 1), "
            f"COALESCE(MAX({_quote(pk)}) >= 1, FALSE)) FROM {_quote(table)}",
            (table, pk),
        )


def _import_rows(db, source: sqlite3.Connection, batch_size: int, models=MODELS) -> dict:
    summaries = {}
    for model in models:
        count = 0
        digest = hashlib.sha256()
        replaced: Counter[str] = Counter()
        for batch in chunked(_source_rows(source, model, replaced=replaced), batch_size):
            model.insert_many(batch, fields=model._meta.sorted_fields).execute()
            for row in batch:
                _hash_row(digest, row)
            count += len(batch)
        summaries[model._meta.table_name] = {"rows": count, "sha256": digest.hexdigest()}
        _reset_sequence(db, model)
        logger.info("Imported %s: %d rows", model._meta.table_name, count)
        for column, replaced_count in sorted(replaced.items()):
            logger.info("Replaced U+0000 in %d values of %s", replaced_count, column)
    return summaries


def _recovery_columns(models, manifest):
    available = {model._meta.table_name: model for model in models}
    if not set(manifest["tables"]).issubset(available):
        raise RuntimeError("Source tables changed since the committed import.")
    v1_columns = V1_COLUMNS | V1_OPTIONAL_COLUMNS
    columns = ({table: v1_columns[table] for table in manifest["tables"] if table in v1_columns}
               if manifest["version"] == 1 else manifest["columns"])
    if set(columns) != set(manifest["tables"]):
        raise RuntimeError("Unsupported column projection in committed import.")
    for table, names in columns.items():
        fields = available[table]._meta.sorted_fields
        if (not isinstance(names, list) or not names
                or names != [field.column_name for field in fields if field.column_name in names]
                or any(not field.null and field.column_name not in names for field in fields)):
            raise RuntimeError(f"Unsupported column projection in committed import: {table}.")
    return columns


def _validate_recovery_extras(source, db, columns):
    """Old projections may omit only empty tables and NULL added fields, on both sides."""
    for label, tables, execute, actual_columns in (
        ("Source", _source_tables(source), source.execute,
         lambda table: {row[1] for row in _source_columns(source, table)}),
        ("Target", set(db.get_tables(schema=current_schema(db))), db.execute_sql,
         lambda table: {field.name for field in db.get_columns(table, schema=current_schema(db))}),
    ):
        for table in tables:
            # The outbound queue has its own import record and verification.
            if table in (IMPORT_TABLE, QUEUE_IMPORT_TABLE) or table in QUEUE_COLUMNS or table.startswith("sqlite_"):
                continue
            if table not in columns:
                if execute(f"SELECT 1 FROM {_quote(table)} LIMIT 1").fetchone():
                    raise RuntimeError(f"{label} has unrecorded data in {table}; preserving both databases.")
                continue
            for name in actual_columns(table) - set(columns[table]):
                if execute(f"SELECT 1 FROM {_quote(table)} WHERE {_quote(name)} IS NOT NULL LIMIT 1").fetchone():
                    raise RuntimeError(f"{label} has unrecorded data in {table}.{name}; preserving both databases.")


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


def _queue_value(table: str, key: tuple, name: str, kind: str, nullable: bool, value):
    """Return ``value`` as PostgreSQL stores it; anything else fails, naming the row."""
    if value is None:
        if nullable:
            return None
    elif kind in _INTEGER_LIMITS:
        if type(value) is int and -_INTEGER_LIMITS[kind] <= value < _INTEGER_LIMITS[kind]:
            return value
    elif kind == "double":
        if type(value) in (int, float):
            return float(value)
    elif kind == "text":
        if isinstance(value, str) and "\x00" not in value:
            return value
    elif isinstance(value, bytes) and len(value) <= MAX_QUEUE_VALUE_BYTES:
        return value
    raise RuntimeError(
        f"Outbound queue {table} row {key}: {name} cannot be stored in PostgreSQL as {kind} (NULL, a "
        f"different type, a NUL character or over {MAX_QUEUE_VALUE_BYTES // (1024 * 1024)} MiB); "
        "the queue was not moved and its source is preserved."
    )


def _queue_source_columns(source: sqlite3.Connection) -> dict:
    """Each queue table's columns, or None when an older queue does not have the table."""
    tables = _source_tables(source)
    for table in tables - set(QUEUE_COLUMNS):
        if not table.startswith("sqlite_") and source.execute(f"SELECT 1 FROM {_quote(table)} LIMIT 1").fetchone():
            raise RuntimeError(f"Nonempty unsupported outbound queue table {table!r}; the queue was not moved.")
    present: dict[str, set | None] = {}
    for table, columns in QUEUE_COLUMNS.items():
        if table not in tables:
            present[table] = None
            continue
        actual = {row[1] for row in _source_columns(source, table)}
        unknown = actual - {name for name, *_ in columns}
        if unknown:
            raise RuntimeError(f"Unsupported columns in outbound queue {table}: {sorted(unknown)}; the queue was not moved.")
        missing = [name for name, _, _, default in columns if default is _REQUIRED and name not in actual]
        if missing:
            raise RuntimeError(f"Missing required columns in outbound queue {table}: {missing}")
        present[table] = actual
    return present


def _queue_source_rows(source: sqlite3.Connection, table: str, present) -> Iterator[tuple]:
    if present is None:
        return
    columns = QUEUE_COLUMNS[table]
    names = [name for name, *_ in columns]
    projection = ", ".join(_quote(name) if name in present else "NULL" for name in names)
    order = ", ".join(_quote(name) + " COLLATE BINARY" for name in QUEUE_KEYS[table])
    keys = [names.index(name) for name in QUEUE_KEYS[table]]
    cursor = source.execute(f"SELECT {projection} FROM {_quote(table)} ORDER BY {order}")
    try:
        for row in cursor:
            key = tuple(row[index] for index in keys)
            yield tuple(
                default if name not in present else _queue_value(table, key, name, kind, nullable, value)
                for (name, kind, nullable, default), value in zip(columns, row)
            )
    finally:
        cursor.close()


def _queue_digest_row(row) -> tuple:
    # BYTEA values are compared by their SHA-256, which PostgreSQL computes
    # itself, so verification never pulls a large payload into memory.
    return tuple(hashlib.sha256(value).digest() if isinstance(value, bytes) else value for value in row)


def _copy_queue_table(db, source: sqlite3.Connection, table: str, present, batch_size: int) -> dict:
    names = [name for name, *_ in QUEUE_COLUMNS[table]]
    insert = f"INSERT INTO {_quote(table)} ({', '.join(_quote(name) for name in names)}) VALUES "
    placeholders = "(" + ", ".join("%s" for _ in names) + ")"
    digest = hashlib.sha256()
    count = 0
    batch: list = []
    size = 0
    for row in _queue_source_rows(source, table, present):
        batch.append(row)
        size += sum(len(value) for value in row if isinstance(value, (bytes, str)))
        _hash_row(digest, _queue_digest_row(row))
        count += 1
        if len(batch) >= batch_size or size >= QUEUE_BATCH_BYTES:
            db.execute_sql(insert + ", ".join(placeholders for _ in batch), [value for item in batch for value in item])
            batch, size = [], 0
    if batch:
        db.execute_sql(insert + ", ".join(placeholders for _ in batch), [value for item in batch for value in item])
    logger.info("Moved outbound queue %s: %d rows", table, count)
    return {"rows": count, "sha256": digest.hexdigest()}


def _queue_target_digest(db, table: str, batch_size: int) -> dict:
    projection = ", ".join(f"sha256({_quote(name)})" if kind == "bytea" else _quote(name)
                           for name, kind, *_ in QUEUE_COLUMNS[table])
    order = ", ".join(_quote(name) + ' COLLATE "C"' if table != "outbound_queue" else _quote(name)
                      for name in QUEUE_KEYS[table])
    with db.connection().cursor(name="etm_verify_" + uuid.uuid4().hex, withhold=True) as cursor:
        cursor.itersize = batch_size
        cursor.execute(f"SELECT {projection} FROM {_quote(table)} ORDER BY {order}")
        return _digest(tuple(bytes(value) if isinstance(value, memoryview) else value for value in row)
                       for row in cursor)


def _import_queue(db, directory: Path, archive: Path, expected: dict, batch_size: int) -> dict:
    """Move the SQLite outbound queue into PostgreSQL, or verify an earlier move.

    Reads the live queue under the caller's write fence: its rows keep naming the
    live ``outbound-media`` files, which stay where they are. One transaction
    holds the rows, the identity reset, verification and the queue import record.
    """
    from .outbound import (
        _POSTGRESQL_OWNER_LOCK,
        POSTGRESQL_QUEUE_MARKER,
        create_postgresql_queue_tables,
    )

    queue_path = directory / QUEUE_FILE
    marker_path = directory / POSTGRESQL_QUEUE_MARKER
    with (
        closing(sqlite3.connect(queue_path.resolve().as_uri() + "?mode=ro", uri=True)) as source,
        connection_scope(db), db.atomic(),
    ):
        db.execute_sql("SET LOCAL lock_timeout = '5s'")
        db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        record = _read_queue_import(db)
        # The bot's queue holds this lock for as long as it runs.
        free = db.execute_sql(
            "SELECT pg_try_advisory_xact_lock(%s::bigint + oid::bigint) FROM pg_namespace "
            "WHERE nspname = current_schema()", (_POSTGRESQL_OWNER_LOCK,),
        ).fetchone()
        if not free or not free[0]:
            raise RuntimeError("The PostgreSQL outbound queue is in use; stop the bot before moving the queue.")
        if marker_path.exists() and (
            record is None or json.loads(marker_path.read_text()).get("import_id") != record["import_id"]
        ):
            raise RuntimeError("Existing outbound queue marker does not match this target; refusing another queue move.")
        present = _queue_source_columns(source)
        if record is None:
            create_postgresql_queue_tables(db.execute_sql)
            for table in QUEUE_COLUMNS:
                if db.execute_sql(f"SELECT 1 FROM {_quote(table)} LIMIT 1").fetchone():
                    raise RuntimeError(
                        f"PostgreSQL queue table {table} is not empty and has no verified queue import "
                        "record; refusing to merge two queues."
                    )
            tables = {table: _copy_queue_table(db, source, table, present[table], batch_size)
                      for table in QUEUE_COLUMNS}
            # SQLite never reuses a deleted queue id; an aggregation context or a
            # predecessor link may still name one.
            sequence = (source.execute("SELECT seq FROM sqlite_sequence WHERE name = 'outbound_queue'").fetchone()
                        if "sqlite_sequence" in _source_tables(source) else None)
            last = 0 if sequence is None else sequence[0]
            if type(last) is not int:
                raise RuntimeError("Invalid outbound queue id sequence; the queue was not moved.")
            db.execute_sql(
                "SELECT setval(pg_get_serial_sequence('outbound_queue', 'id'), "
                "GREATEST(COALESCE(MAX(id), 0), %s, 1), GREATEST(COALESCE(MAX(id), 0), %s) >= 1) "
                "FROM outbound_queue", (last, last),
            )
            record = {
                "version": 1, "import_id": uuid.uuid4().hex, "tables": tables, "queue": expected,
                "backup_queue": _queue_digest(archive / QUEUE_FILE), "backup_directory": str(archive),
            }
            db.execute_sql(f"CREATE TABLE {QUEUE_IMPORT_TABLE} (id INTEGER PRIMARY KEY CHECK (id = 1), manifest TEXT NOT NULL)")
            db.execute_sql(f"INSERT INTO {QUEUE_IMPORT_TABLE} (id, manifest) VALUES (1, %s)", (_json(record),))
        elif expected not in (record["queue"], record["backup_queue"]):
            raise RuntimeError("Outbound queue changed since it was moved; refusing to merge divergent queues.")
        for table in QUEUE_COLUMNS:
            if _queue_target_digest(db, table, batch_size) != record["tables"][table]:
                raise RuntimeError(f"Content verification failed for outbound queue {table}; the SQLite queue is preserved.")
        if _queue_digest(queue_path) != expected:
            raise RuntimeError("Outbound queue or media changed during import; keep all writers stopped and retry.")
    return record


def _import_tables(db, directory: Path, archive: Path, queue_summary: dict | None, batch_size: int) -> dict:
    source_path = directory / "tgdata.db"
    queue_path = directory / QUEUE_FILE
    with (
        closing(sqlite3.connect(archive / source_path.name)) as source,
        ExitStack() as bindings, connection_scope(db), db.atomic(),
    ):
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
            names = ", ".join(_quote(name) for name in db.get_tables(schema=current_schema(db)))
            db.execute_sql(f"LOCK TABLE {names} IN SHARE ROW EXCLUSIVE MODE")
            columns = _recovery_columns(models, manifest)
            _validate_recovery_extras(source, db, columns)
            models = tuple(model for model in models if model._meta.table_name in columns)
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
    return manifest


def migrate(directory: Path, config: dict, batch_size: int = 500, *, queue_only: bool = False) -> dict:
    """Import an empty target, or verify/recover an already-committed import.

    The transaction includes all rows, indexes, sequence resets, verification
    and provenance. After a post-commit crash, re-running verifies both sides
    before recreating the local cutover receipt; no rows are inserted twice.
    The outbound queue then moves the same way in a second transaction.
    ``queue_only`` moves just the queue, for an install whose other data is
    already in PostgreSQL; it returns the queue import record.
    """
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if config.get("type") != "postgresql":
        raise ValueError("Migration requires database.type: postgresql in the supplied configuration")
    from .outbound import POSTGRESQL_QUEUE_MARKER

    directory = directory.resolve()
    source_path = directory / "tgdata.db"
    queue_path = directory / QUEUE_FILE
    required = queue_path if queue_only else source_path
    if not required.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {required}")
    db = postgresql_database(config)
    with DataDirectoryLock(directory):
        try:
            if queue_only:
                with connection_scope(db):
                    manifest = _read_import(db)
                receipt_path = directory / RECEIPT_FILE
                if source_path.exists() and (manifest is None or not receipt_path.exists()):
                    # Moving only the queue would leave tgdata.db unimportable
                    # (the target is no longer empty) and the SQLite queue closed.
                    # Without a receipt the plain command verifies the import first.
                    raise RuntimeError(
                        "tgdata.db has not been imported into this PostgreSQL target; run python -m "
                        "efb_telegram_master.migrate_db --data-dir <directory> --config <postgresql.yaml> "
                        "without --queue-only to move the data and the outbound queue together."
                    )
                receipt_id = json.loads(receipt_path.read_text()).get("import_id") if receipt_path.exists() else None
                if receipt_id != (manifest["import_id"] if manifest is not None else None):
                    # Another install's target would take this directory's queue.
                    raise RuntimeError(
                        "Existing cutover receipt does not match this target; refusing another import."
                    )
            with ExitStack() as fences:
                if not queue_only:
                    fences.enter_context(_fence(source_path))
                if queue_path.exists():
                    fences.enter_context(_fence(queue_path))
                backup_root = directory / "database-backups"
                backup_root.mkdir(mode=0o700, exist_ok=True)
                archive = Path(tempfile.mkdtemp(prefix="sqlite-", dir=backup_root))
                if not queue_only:
                    _backup(source_path, archive / source_path.name)
                queue_summary = _backup_queue(queue_path, archive)
                for path in (archive, backup_root, directory):
                    _sync_directory(path)
                logger.info("Consistent SQLite backups: %s", archive)
                manifest = None if queue_only else _import_tables(db, directory, archive, queue_summary, batch_size)
                queue_record = None if queue_summary is None else _import_queue(
                    db, directory, archive, queue_summary, batch_size,
                )
            # SQLite locks/connections have closed, including any resulting WAL
            # checkpoint, so the recorded fingerprint is stable across startup.
            receipt = None
            if manifest is not None:
                receipt = dict(manifest, source_fingerprint=_fingerprint(source_path))
                _write_receipt(archive / "manifest.json", receipt)
                _write_receipt(directory / RECEIPT_FILE, receipt)
            if queue_record is not None:
                _write_receipt(directory / POSTGRESQL_QUEUE_MARKER, queue_record)
                logger.info("Outbound queue is in PostgreSQL. Its SQLite file is retained and must not be resumed.")
            if receipt is None:
                assert queue_record is not None
                return queue_record
            logger.info("Import verified. Retained source and outbound queue; switch configuration to PostgreSQL.")
            return receipt
        finally:
            db.close_all()


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
            with db.atomic():
                db.execute_sql("SET LOCAL lock_timeout = '5s'")
                db.execute_sql("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
                manifest = _read_import(db)
                if manifest is None:
                    raise RuntimeError("PostgreSQL has no import record; import tgdata.db first, without --catch-up.")
                models = MODELS + _cache_models(source, manifest)
                _validate_source(source, models)
                _validate_source(base, MODELS + _cache_models(base, manifest))
                columns = _recovery_columns(models, manifest)
                _validate_recovery_extras(source, db, columns)
                models = tuple(model for model in models if model._meta.table_name in columns)
                db.execute_sql(f"LOCK TABLE {', '.join(_quote(table) for table in columns)} IN SHARE ROW EXCLUSIVE MODE")
                previous = manifest.get(CATCH_UP_KEY, {})
                _check_target(db, manifest, columns, previous.get("baseline"))

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
                        help="Channel data directory (tgdata.db, outbound-queue.sqlite3); stop all writers first")
    parser.add_argument("--config", type=Path, required=True, help="YAML with the target database mapping (password is never printed)")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--queue-only", action="store_true",
                        help="Move only the outbound queue, when the other data is already in PostgreSQL")
    parser.add_argument("--catch-up", type=Path, metavar="BASE",
                        help="Bring the PostgreSQL import up to date with tgdata.db; BASE is the SQLite file it was imported from")
    parser.add_argument("--final", action="store_true",
                        help="With --catch-up: the last run, with the bot stopped; writes the cutover receipt")
    parser.add_argument("--window-days", type=int,
                        help="With --catch-up: days before BASE's newest message in which messages may have been edited (default 7)")
    args = parser.parse_args(argv)
    if args.catch_up is None and (args.final or args.window_days is not None):
        parser.error("--final and --window-days need --catch-up")
    if args.catch_up is not None and args.queue_only:
        parser.error("--catch-up and --queue-only are separate steps")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        with args.config.open() as stream:
            config = YAML(typ="safe").load(stream)
        if not isinstance(config, dict) or not isinstance(config.get("database"), dict):
            raise ValueError("Configuration must contain a database mapping")
        if args.catch_up is not None:
            print(_json(catch_up(args.data_dir, config["database"], args.catch_up, final=args.final,
                                 window_days=7 if args.window_days is None else args.window_days,
                                 batch_size=args.batch_size)))
            return 0
        result = migrate(args.data_dir, config["database"], args.batch_size, queue_only=args.queue_only)
        print(_json({"import_id": result["import_id"], "tables": result["tables"], "backup_directory": result["backup_directory"]}))
    except Exception as error:
        parser.exit(1, f"Migration failed: {error}\nOriginal SQLite files were not deleted.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
