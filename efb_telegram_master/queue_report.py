"""Read-only report on the outbound queue: depth, age and blockers per destination.

Run it against the channel data directory, which holds both databases:

    python3 -m efb_telegram_master.queue_report --data-dir ~/.ehforwarderbot/profiles/default/blueset.telegram

Nothing is written: the queue and ``tgdata.db`` are opened read-only, so this is
safe while EFB is running. ``payload`` and ``log_context`` are pickled, so row
contents are not readable here; use ``python -m efb_telegram_master.hold_send``
for a single row.
"""
import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .paths import get_base_path

QUEUE_FILENAME = "outbound-queue.sqlite3"
TG_FILENAME = "tgdata.db"
MODULE_ID = "blueset.telegram"
NOW_SECONDS = "CAST(strftime('%s','now') AS INTEGER)"

QUERIES = (
    ("per destination: depth, holds, age", f"""
        SELECT q.telegram_chat_id AS tg_chat, COALESCE(q.thread_id, 0) AS topic,
               COUNT(*) AS queued, SUM(q.delivery_hold IS NOT NULL) AS held,
               GROUP_CONCAT(DISTINCT q.operation) AS operations,
               datetime(MIN(q.created_at), 'unixepoch') AS oldest,
               {NOW_SECONDS} - CAST(MIN(q.created_at) AS INTEGER) AS oldest_age_s,
               {NOW_SECONDS} - CAST(MAX(q.created_at) AS INTEGER) AS newest_age_s
        FROM outbound_queue q
        WHERE q.delivery_state = 'queued'
        GROUP BY 1, 2
        ORDER BY queued DESC
    """),
    # One Telegram group can carry many chatassoc rows (every relink adds one), so
    # depth must be aggregated before the name lookup or the count multiplies.
    ("per destination with the bound slave chat", f"""
        WITH depth AS (
            SELECT q.telegram_chat_id AS tg_chat, COALESCE(q.thread_id, 0) AS topic,
                   COUNT(*) AS queued, MIN(q.created_at) AS oldest_created
            FROM outbound_queue q
            WHERE q.delivery_state = 'queued'
            GROUP BY 1, 2
        ),
        names AS (
            SELECT c.master_uid AS master_uid,
                   MIN(COALESCE(s.slave_chat_name, c.slave_uid)) AS slave_chat
            FROM tg.chatassoc c
            LEFT JOIN tg.slavechatinfo s ON s.slave_chat_uid = c.slave_uid
            GROUP BY c.master_uid
        )
        SELECT d.tg_chat AS tg_chat, d.topic AS topic, d.queued AS queued,
               {NOW_SECONDS} - CAST(d.oldest_created AS INTEGER) AS oldest_age_s,
               COALESCE(n.slave_chat, '(unlinked)') AS slave_chat
        FROM depth d
        LEFT JOIN names n ON n.master_uid LIKE '% ' || d.tg_chat
        ORDER BY queued DESC
    """),
    ("by source: live traffic, aggregation rows, history backfill", f"""
        SELECT CASE WHEN q.slave_id LIKE '__history__:%' THEN 'history'
                    WHEN q.slave_id IS NULL THEN 'plain'
                    ELSE 'live' END AS kind,
               COALESCE(REPLACE(q.slave_id, '__history__:', ''), '(none)') AS slave_chat,
               COUNT(*) AS rows,
               {NOW_SECONDS} - CAST(MIN(q.created_at) AS INTEGER) AS oldest_age_s
        FROM outbound_queue q
        GROUP BY 1, 2
        ORDER BY rows DESC
    """),
    ("age distribution", """
        SELECT CASE WHEN age < 60 THEN '<1m'
                    WHEN age < 600 THEN '1-10m'
                    WHEN age < 3600 THEN '10m-1h'
                    WHEN age < 21600 THEN '1-6h'
                    WHEN age < 86400 THEN '6-24h'
                    ELSE '>1d' END AS bucket,
               COUNT(*) AS rows
        FROM (SELECT CAST(strftime('%s','now') AS INTEGER) - CAST(created_at AS INTEGER) AS age
              FROM outbound_queue WHERE delivery_state = 'queued')
        GROUP BY bucket
        ORDER BY MIN(age)
    """),
    # implied_per_min is the lane's own drain rate in steady state: depth divided by
    # how long its oldest dispatchable row has waited. Far below the bot-chat limit
    # (18/minute) means the lane is waiting on uploads, not on the rate limiter.
    ("head per destination: age, position and drain rate", f"""
        WITH heads AS (
            SELECT COALESCE(MIN(CASE WHEN priority = 1 THEN id END), MIN(id)) AS head_id,
                   COUNT(*) AS lane_depth
            FROM outbound_queue
            WHERE delivery_state = 'queued' AND delivery_hold IS NULL
            GROUP BY telegram_chat_id, COALESCE(thread_id, 0)
        )
        SELECT q.telegram_chat_id AS tg_chat, COALESCE(q.thread_id, 0) AS topic, q.id AS head_id,
               q.operation, q.priority, h.lane_depth AS lane_depth,
               {NOW_SECONDS} - CAST(q.created_at AS INTEGER) AS head_age_s,
               (SELECT COUNT(*) FROM outbound_queue w
                 WHERE w.telegram_chat_id = q.telegram_chat_id
                   AND COALESCE(w.thread_id, 0) = COALESCE(q.thread_id, 0)
                   AND w.delivery_state = 'queued'
                   AND w.id <= q.id) AS position_in_lane,
               ROUND(h.lane_depth * 60.0 / MAX(1, {NOW_SECONDS} - CAST(q.created_at AS INTEGER)), 2)
                   AS implied_per_min
        FROM outbound_queue q
        JOIN heads h ON q.id = h.head_id
        ORDER BY head_age_s DESC
    """),
    ("blocked rows: awaiting reconciliation or held", f"""
        SELECT telegram_chat_id AS tg_chat, COALESCE(thread_id, 0) AS topic, delivery_state,
               COALESCE(delivery_hold, '-') AS hold, COUNT(*) AS rows,
               {NOW_SECONDS} - CAST(MIN(created_at) AS INTEGER) AS oldest_age_s
        FROM outbound_queue
        WHERE delivery_state = 'sent_pending' OR delivery_hold IS NOT NULL
        GROUP BY 1, 2, 3, 4
        ORDER BY rows DESC
    """),
    ("history replay progress: staged entries", """
        SELECT target_chat_id AS tg_chat, COALESCE(message_thread_id, '0') AS topic,
               COUNT(*) AS staged,
               MIN(COALESCE(source_time, received_time)) AS oldest_source_time,
               MIN(position) AS next_position
        FROM tg.historymigrationentry
        GROUP BY 1, 2
        ORDER BY staged DESC
    """),
    ("topic routing: target chat, thread, slave chat", """
        SELECT topic_chat_id, message_thread_id, slave_uid
        FROM tg.topicassoc
        ORDER BY topic_chat_id + 0, message_thread_id + 0
    """),
)


def resolve_data_dir(value: Path | None) -> Path:
    if value is not None:
        return Path(value).expanduser().resolve()
    if (Path.cwd() / QUEUE_FILENAME).exists():
        return Path.cwd()
    from ehforwarderbot import coordinator
    profile = getattr(coordinator, "profile", None) or "default"
    return get_base_path() / "profiles" / profile / MODULE_ID


def report(directory: Path, out=sys.stdout) -> int:
    directory = Path(directory).resolve()
    queue_path = directory / QUEUE_FILENAME
    if not queue_path.exists():
        raise FileNotFoundError(f"No outbound queue at {queue_path}")
    tg_path = directory / TG_FILENAME
    with closing(sqlite3.connect(f"{queue_path.as_uri()}?mode=ro", uri=True, timeout=5)) as db:
        if tg_path.exists():
            db.execute(f"ATTACH '{tg_path.as_uri()}?mode=ro' AS tg")
        else:
            print(f"# {tg_path} is missing; queries that join tg.* are skipped", file=out)
        for title, sql in QUERIES:
            print(f"\n== {title}", file=out)
            try:
                cursor = db.execute(sql)
            except sqlite3.Error as error:
                print(f"   skipped: {error}", file=out)
                continue
            print("   " + " | ".join(column[0] for column in cursor.description), file=out)
            for row in cursor.fetchall():
                print("   " + " | ".join(str(value) for value in row), file=out)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Channel data directory (defaults to the current directory "
                             "or the default profile)")
    args = parser.parse_args(argv)
    try:
        return report(resolve_data_dir(args.data_dir))
    except (OSError, sqlite3.Error) as error:
        parser.exit(1, f"Queue report failed: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
