"""Small connection/ownership helpers shared by the runtime and offline importer."""

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from peewee import Database, PostgresqlDatabase
from playhouse.pool import PooledPostgresqlDatabase, PooledSqliteDatabase

# Both schema initialization and offline import use this transaction-scoped lock.
SCHEMA_LOCK = 681_774_240_616_480_001


def postgresql_database(config: dict) -> PooledPostgresqlDatabase:
    """Use the standard pool, whose import path is stable in Peewee 3 and 4."""
    return PooledPostgresqlDatabase(
        config.get("database", "efb_telegram"),
        host=config.get("host", "localhost"),
        port=config.get("port", 5432),
        user=config.get("user", "postgres"),
        password=config.get("password", ""),
        max_connections=config.get("max_connections", 8),
        stale_timeout=config.get("stale_timeout", 300),
        timeout=config.get("pool_timeout", 5),
        options=config.get("options", "-c timezone=UTC"),
    )


def sqlite_database(path: Path, config: dict) -> PooledSqliteDatabase:
    return PooledSqliteDatabase(
        str(path),
        max_connections=config.get("max_connections", 8),
        stale_timeout=config.get("stale_timeout", 300),
        timeout=config.get("pool_timeout", 5),
        pragmas={"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 5000},
        check_same_thread=False,
    )


def current_schema(db: Database) -> str | None:
    """Peewee 3 otherwise introspects public even when search_path is different."""
    if isinstance(db, PostgresqlDatabase):
        schema = db.execute_sql("SELECT current_schema()").fetchone()[0]
        if schema is None:
            raise RuntimeError("PostgreSQL search_path has no existing schema; create the target schema first.")
        return str(schema)
    return None


@contextmanager
def connection_scope(db: Database) -> Iterator[None]:
    """Return only the connection we acquired; nested calls keep the outer lease."""
    opened = db.is_closed()
    if opened:
        db.connect()
    try:
        yield
    finally:
        if opened:
            db.close()


class DataDirectoryLock:
    """Exclude a second bot or importer from the same local data directory.

    The bot and importer run on Unix. Older deployments and other programs do
    not participate in this lock and must also be stopped before migration.
    """

    def __init__(self, directory: Path):
        import fcntl

        directory.mkdir(parents=True, exist_ok=True)
        self._file = (directory / ".database.lock").open("a+b")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            raise RuntimeError("Data directory is in use; stop the bot before migration or startup.") from None

    def close(self) -> None:
        # close() releases flock, including when called from a different thread.
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
