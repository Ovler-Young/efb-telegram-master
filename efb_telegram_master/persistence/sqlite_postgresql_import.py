import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from peewee import AutoField, Model, chunked

from ..legacy_outbound_retirement import LegacyOutboundRetirement
from ..models import ChatAssoc, HistoryMigrationEntry, MsgLog, MsgLogIngestionScan, SlaveChatInfo, SlaveMessageDelivery, TopicAssoc
from .schema_migration import DatabaseSchemaMigrator


@dataclass(frozen=True)
class SQLiteSourceProjection:
    model: type[Model]
    column_names: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


@dataclass(frozen=True)
class SQLiteImportSnapshot:
    projections: tuple[SQLiteSourceProjection, ...]
    identity: str


class SQLitePostgresqlImportCoordinator:
    """Preserve and import a historical SQLite source into PostgreSQL."""

    SQLITE_IMPORT_LOCK_KEY = 681_774_240_616_480_001
    SQLITE_IMPORT_PROVENANCE_TABLE = "sqliteimportprovenance"
    MODELS = (ChatAssoc, TopicAssoc, SlaveChatInfo, MsgLog, HistoryMigrationEntry, MsgLogIngestionScan, SlaveMessageDelivery)

    def __init__(self, database: Any, schema: DatabaseSchemaMigrator, logger: Any) -> None:
        self.database = database
        self.schema = schema
        self.logger = logger

    @contextmanager
    def _lifecycle_lock(self):
        self.database.execute_sql("SELECT pg_advisory_lock(%s)", (self.SQLITE_IMPORT_LOCK_KEY,))
        try:
            yield
        finally:
            self.database.execute_sql("SELECT pg_advisory_unlock(%s)", (self.SQLITE_IMPORT_LOCK_KEY,))

    def initialize(self, base_path: Path) -> None:
        with self._lifecycle_lock():
            sqlite_path = base_path / "tgdata.db"
            migrated_path = sqlite_path.with_suffix(".db.migrated")
            if sqlite_path.exists() and migrated_path.exists() and not os.path.samefile(sqlite_path, migrated_path):
                raise RuntimeError("SQLite-to-PostgreSQL migration finalization collision: both tgdata.db and tgdata.db.migrated exist with different contents; preserving both files. Resolve the conflict before restarting.")
            target_initialized = ChatAssoc.table_exists()
            if sqlite_path.exists() or migrated_path.exists():
                LegacyOutboundRetirement(self.database).reject_target_data()
            if not target_initialized and sqlite_path.exists():
                self._migrate_from_sqlite(sqlite_path, finalize_source=True)
            elif not target_initialized and migrated_path.exists():
                self._migrate_from_sqlite(migrated_path, finalize_source=False)
            elif target_initialized and sqlite_path.exists():
                self._finalize_completed_sqlite_import(sqlite_path)
                self.schema.create()
            else:
                self.schema.create()

    @staticmethod
    def _snapshot_value(value: object) -> object:
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        isoformat = getattr(value, "isoformat", None)
        return {"isoformat": isoformat()} if callable(isoformat) else {"string": str(value)}

    @staticmethod
    def _dict_row_values(row: object, column_names: tuple[str, ...]) -> tuple[object, ...]:
        if not isinstance(row, Mapping):
            raise TypeError("Peewee dictionary query returned a non-mapping row.")
        return tuple(row[column_name] for column_name in column_names)

    @classmethod
    def sqlite_source_snapshot(cls, source_database: Any, models: tuple[type[Model], ...]) -> SQLiteImportSnapshot:
        table_names = set(source_database.get_tables())
        projections: list[SQLiteSourceProjection] = []
        serialized_projections: list[dict[str, object]] = []
        for model in models:
            if model._meta.table_name not in table_names:
                column_names: tuple[str, ...] = ()
                rows: tuple[tuple[object, ...], ...] = ()
            else:
                source_columns = {column.name for column in source_database.get_columns(model._meta.table_name)}
                fields = tuple(field for field in model._meta.sorted_fields if field.column_name in source_columns)
                column_names = tuple(field.column_name for field in fields)
                canonical_primary_keys = DatabaseSchemaMigrator.canonical_historic_primary_key_values(model)
                primary_key = model._meta.primary_key
                query = model.select(*fields)
                if canonical_primary_keys is not None:
                    if primary_key is False:
                        raise ValueError(f"{model.__name__} has no primary key")
                    query = query.where(primary_key.in_(canonical_primary_keys))
                rows = tuple(cls._dict_row_values(row, column_names) for row in query.dicts())
                if model in (MsgLogIngestionScan, SlaveMessageDelivery) and "lease_clock" not in source_columns:
                    column_names += ("lease_clock",)
                    rows = tuple((*row, None) for row in rows)
            projections.append(SQLiteSourceProjection(model, column_names, rows))
            serialized_projections.append({"table": model._meta.table_name, "columns": column_names, "rows": sorted((tuple(cls._snapshot_value(value) for value in row) for row in rows), key=lambda row: json.dumps(row, ensure_ascii=True, separators=(",", ":"), sort_keys=True))})
        serialized_snapshot = json.dumps(serialized_projections, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return SQLiteImportSnapshot(tuple(projections), sha256(serialized_snapshot.encode()).hexdigest())

    @contextmanager
    def _source_fence(self, sqlite_path: Path):
        from peewee import SqliteDatabase

        source = SqliteDatabase(str(sqlite_path))
        source.connect()
        try:
            with source.atomic("IMMEDIATE"):
                with source.bind_ctx(self.MODELS):
                    LegacyOutboundRetirement(source).reject_source_data()
                    yield self.sqlite_source_snapshot(source, self.MODELS), source
        finally:
            source.close()

    def _target_matches_snapshot(self, snapshot: SQLiteImportSnapshot) -> bool:
        def serialized(rows: tuple[tuple[object, ...], ...]) -> list[str]:
            return sorted(json.dumps(tuple(self._snapshot_value(value) for value in row), ensure_ascii=True, separators=(",", ":"), sort_keys=True) for row in rows)

        for projection in snapshot.projections:
            if not projection.column_names:
                if projection.model.select().count() != len(projection.rows):
                    return False
                continue
            fields = [field for field in projection.model._meta.sorted_fields if field.column_name in projection.column_names]
            target_rows = tuple(self._dict_row_values(row, projection.column_names) for row in projection.model.select(*fields).dicts())
            if serialized(target_rows) != serialized(projection.rows):
                return False
        return True

    def _reconcile_sequences(self) -> None:
        for model in self.MODELS:
            primary_key = model._meta.primary_key
            if isinstance(primary_key, AutoField):
                self.database.execute_sql(f"SELECT setval(pg_get_serial_sequence({self.database.param}, {self.database.param}), COALESCE(MAX(\"{primary_key.column_name}\"), 1), MAX(\"{primary_key.column_name}\") IS NOT NULL) FROM \"{model._meta.table_name}\"", (model._meta.table_name, primary_key.column_name))

    def _record_provenance(self, snapshot: SQLiteImportSnapshot) -> None:
        self.database.execute_sql(f'CREATE TABLE IF NOT EXISTS "{self.SQLITE_IMPORT_PROVENANCE_TABLE}" (snapshot_identity TEXT PRIMARY KEY)')
        self.database.execute_sql(f'INSERT INTO "{self.SQLITE_IMPORT_PROVENANCE_TABLE}" (snapshot_identity) VALUES ({self.database.param})', (snapshot.identity,))

    def _has_provenance(self, snapshot: SQLiteImportSnapshot) -> bool:
        return self.SQLITE_IMPORT_PROVENANCE_TABLE in self.database.get_tables() and self.database.execute_sql(f'SELECT 1 FROM "{self.SQLITE_IMPORT_PROVENANCE_TABLE}" WHERE snapshot_identity = {self.database.param}', (snapshot.identity,)).fetchone() is not None

    @staticmethod
    def _create_archive(source_database: Any, archive_path: Path) -> None:
        source_connection = sqlite3.connect(source_database.database)
        archive_connection = sqlite3.connect(str(archive_path))
        try:
            source_connection.backup(archive_connection)
            if archive_connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("SQLite archive integrity check failed")
        finally:
            archive_connection.close()
            source_connection.close()

    @classmethod
    def _finalize_source(cls, source_database: Any, sqlite_path: Path) -> None:
        migrated_path = sqlite_path.with_suffix(".db.migrated")
        if migrated_path.exists():
            raise RuntimeError("SQLite-to-PostgreSQL migration finalization collision: tgdata.db.migrated already exists; preserving both files. Resolve the conflict before restarting.")
        descriptor, archive_name = tempfile.mkstemp(prefix=f".{migrated_path.name}.", suffix=".tmp", dir=sqlite_path.parent)
        os.close(descriptor)
        archive_path = Path(archive_name)
        try:
            try:
                cls._create_archive(source_database, archive_path)
                os.link(archive_path, migrated_path)
            except FileExistsError as error:
                raise RuntimeError("SQLite-to-PostgreSQL migration finalization collision: tgdata.db.migrated already exists; preserving both files. Resolve the conflict before restarting.") from error
            except (OSError, RuntimeError, sqlite3.Error) as error:
                raise RuntimeError(f"SQLite-to-PostgreSQL migration finalization failed; source remains at {sqlite_path}") from error
        finally:
            archive_path.unlink(missing_ok=True)
        for source_path in (sqlite_path, sqlite_path.with_name(f"{sqlite_path.name}-wal"), sqlite_path.with_name(f"{sqlite_path.name}-shm")):
            source_path.unlink(missing_ok=True)

    def _finalize_completed_sqlite_import(self, sqlite_path: Path) -> None:
        with self._source_fence(sqlite_path) as (snapshot, source_database):
            with self.database.bind_ctx(self.MODELS):
                if not self._has_provenance(snapshot):
                    raise RuntimeError("SQLite-to-PostgreSQL migration restart conflict: target import provenance does not match tgdata.db; preserving the source. Resolve the target/source conflict before restarting.")
                if not self._target_matches_snapshot(snapshot):
                    raise RuntimeError("SQLite-to-PostgreSQL migration restart conflict: target data does not exactly match tgdata.db; preserving the source. Resolve the target/source conflict before restarting.")
                self._finalize_source(source_database, sqlite_path)
        self.logger.info("SQLite-to-PostgreSQL migration source finalization completed; source renamed to %s", sqlite_path.with_suffix(".db.migrated"))

    def _migrate_from_sqlite(self, sqlite_path: Path, *, finalize_source: bool) -> None:
        self.logger.info("Detected existing SQLite database. Migrating to PostgreSQL.")
        with self._source_fence(sqlite_path) as (snapshot, source_database):
            with self.database.bind_ctx(self.MODELS):
                with self.database.atomic():
                    self.schema.create()
                    for projection in snapshot.projections:
                        rows = [dict(zip(projection.column_names, row)) for row in projection.rows]
                        for batch in chunked(rows, 500):
                            projection.model.insert_many(batch).execute()
                    self._reconcile_sequences()
                    self._record_provenance(snapshot)
                    if not self._target_matches_snapshot(snapshot):
                        raise RuntimeError("SQLite-to-PostgreSQL migration verification failed: target content differs from the source snapshot")
            if finalize_source:
                self._finalize_source(source_database, sqlite_path)
        if finalize_source:
            self.logger.info("SQLite-to-PostgreSQL migration completed; source renamed to %s", sqlite_path.with_suffix(".db.migrated"))
        else:
            self.logger.info("SQLite-to-PostgreSQL migration completed from preserved source %s", sqlite_path)
