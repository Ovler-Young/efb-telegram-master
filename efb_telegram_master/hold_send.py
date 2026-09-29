"""Hold an already-observed duplicate before restarting EFB; never delete or resend.

Run only while EFB is stopped. Without --apply, this only reports row metadata.
"""
import argparse
from contextlib import closing
from pathlib import Path
import sqlite3

from .db_runtime import DataDirectoryLock

OPERATOR_HOLD = "uncertain:operator_observed_delivery"


def hold(directory: Path, row_id: int, *, apply: bool = False) -> dict:
    directory = directory.resolve()
    if row_id <= 0:
        raise ValueError("row-id must be positive")
    with DataDirectoryLock(directory), closing(sqlite3.connect(
        (directory / "outbound-queue.sqlite3").as_uri() + "?mode=rw", uri=True, timeout=5,
    )) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT operation, telegram_chat_id, delivery_state, completion_receipt IS NOT NULL "
                "FROM outbound_queue WHERE id=?", (row_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Queue row not found; no data changed")
            operation, chat_id, state, has_receipt = row
            if state != "queued" or has_receipt:
                raise ValueError("This row already has a completion state/receipt; do not override it")
            if apply:
                columns = {col[1] for col in db.execute("PRAGMA table_info(outbound_queue)")}
                if "delivery_hold" not in columns:
                    db.execute("ALTER TABLE outbound_queue ADD COLUMN delivery_hold TEXT NULL")
                db.execute("UPDATE outbound_queue SET delivery_hold=? WHERE id=?", (OPERATOR_HOLD, row_id))
                db.commit()
            else:
                db.rollback()
            return dict(row_id=row_id, operation=operation, telegram_chat_id=chat_id, held=apply)
        except BaseException:
            db.rollback()
            raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--row-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="Persist hold for this operator-confirmed duplicate")
    args = parser.parse_args(argv)
    try:
        print(hold(args.data_dir, args.row_id, apply=args.apply))
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"Hold failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
