"""Hold an already-observed duplicate before restarting EFB; never delete or resend.

Run only while EFB is stopped. Without --apply, this only reports row metadata.
With a PostgreSQL queue, pass the same ``database`` settings the bot uses
(``--config``) and the tool refuses while a bot owns the queue.
"""
import argparse
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
import sqlite3

from .db_runtime import DataDirectoryLock, postgresql_connection
from .delivery_states import OPERATOR_HOLD, Event, RowState, classify, next_state


def _check_holdable(row) -> tuple:
    """The row's operation and chat; raises if the hold must not be written."""
    if row is None:
        raise ValueError("Queue row not found; no data changed")
    operation, chat_id, state, has_receipt, current_hold = row
    current = classify(state, current_hold)
    if current is RowState.SENT_PENDING or has_receipt:
        raise ValueError("This row already has a completion state/receipt; do not override it")
    if next_state(current, Event.OPERATOR_HOLD) is None:
        raise ValueError(f"This row is already held as {current_hold}; no data changed")
    return operation, chat_id


def _hold_sqlite(directory: Path, row_id: int, apply: bool) -> tuple:
    with closing(sqlite3.connect(
        (directory / "outbound-queue.sqlite3").as_uri() + "?mode=rw", uri=True, timeout=5,
    )) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            columns = {col[1] for col in db.execute("PRAGMA table_info(outbound_queue)")}
            # Queues from versions without holds have no delivery_hold column yet.
            hold_column = "delivery_hold" if "delivery_hold" in columns else "NULL"
            row = db.execute(
                "SELECT operation, telegram_chat_id, delivery_state, completion_receipt IS NOT NULL, "
                f"{hold_column} FROM outbound_queue WHERE id=?", (row_id,),
            ).fetchone()
            operation, chat_id = _check_holdable(row)
            if apply:
                if "delivery_hold" not in columns:
                    db.execute("ALTER TABLE outbound_queue ADD COLUMN delivery_hold TEXT NULL")
                db.execute("UPDATE outbound_queue SET delivery_hold=? WHERE id=?", (OPERATOR_HOLD, row_id))
                db.commit()
            else:
                db.rollback()
            return operation, chat_id
        except BaseException:
            db.rollback()
            raise


def _hold_postgresql(config: Mapping, row_id: int, apply: bool) -> tuple:
    # The key the queue takes for as long as it is open.
    from .outbound import _POSTGRESQL_OWNER_LOCK

    connection = postgresql_connection(config)
    try:
        with connection.cursor() as cursor:
            # Give up on a row another session keeps locked, as SQLite gives up on its busy timeout.
            cursor.execute("SET lock_timeout = '5s'")
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s::bigint + oid::bigint) FROM pg_namespace "
                "WHERE nspname = current_schema()", (_POSTGRESQL_OWNER_LOCK,),
            )
            owned = cursor.fetchone()
            if not owned or not owned[0]:
                raise RuntimeError("The outbound queue is in use by a running bot; stop it before holding a row")
            cursor.execute(
                "SELECT operation, telegram_chat_id, delivery_state, completion_receipt IS NOT NULL, "
                "delivery_hold FROM outbound_queue WHERE id=%s FOR UPDATE", (row_id,),
            )
            operation, chat_id = _check_holdable(cursor.fetchone())
            if apply:
                cursor.execute("UPDATE outbound_queue SET delivery_hold=%s WHERE id=%s", (OPERATOR_HOLD, row_id))
                connection.commit()
            else:
                connection.rollback()
            return operation, chat_id
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def hold(directory: Path, row_id: int, *, apply: bool = False, database: Mapping | None = None) -> dict:
    """``database`` is ETM's ``database`` setting; the hold goes to the queue of that backend."""
    directory = directory.resolve()
    if row_id <= 0:
        raise ValueError("row-id must be positive")
    backend = (database or {}).get("type", "sqlite")
    if backend not in ("sqlite", "postgresql"):
        raise ValueError(f"Unknown database type: {backend!r}")
    with DataDirectoryLock(directory):
        if database is not None and backend == "postgresql":
            operation, chat_id = _hold_postgresql(database, row_id, apply)
        else:
            operation, chat_id = _hold_sqlite(directory, row_id, apply)
    return {"row_id": row_id, "operation": operation, "telegram_chat_id": chat_id, "held": apply}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--row-id", type=int, required=True)
    parser.add_argument("--config", type=Path, default=None,
                        help="YAML with the database mapping the bot uses; needed only for a PostgreSQL queue")
    parser.add_argument("--apply", action="store_true", help="Persist hold for this operator-confirmed duplicate")
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
        print(hold(args.data_dir, args.row_id, apply=args.apply, database=database))
    except errors as error:
        parser.exit(1, f"Hold failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
