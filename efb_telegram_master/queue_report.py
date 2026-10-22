"""Read-only report on the outbound queue: depth, age and blockers per destination.

Two ways to run it:

    # anywhere, any python3, no package needed (copy this one file around)
    python3 queue_report.py --data-dir ~/.ehforwarderbot/profiles/default/blueset.telegram
    python3 queue_report.py --queue /path/outbound-queue.sqlite3 --tg /path/tgdata.db

    # inside the checkout, with the package importable
    python3 -m efb_telegram_master.queue_report --data-dir ...

Nothing is written: the queue and ``tgdata.db`` are opened read-only, so this is
safe while EFB is running. ``payload`` and ``log_context`` are pickled, so row
contents are not readable in SQL; only the topic a queued row targets is decoded
here, and only to report rows that lost their lane. Use
``python -m efb_telegram_master.hold_send`` to inspect a single row.
"""
import argparse
import pickle
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

QUEUE_FILENAME = "outbound-queue.sqlite3"
TG_FILENAME = "tgdata.db"
MODULE_ID = "blueset.telegram"
NOW_SECONDS = "CAST(strftime('%s','now') AS INTEGER)"
AGGREGATION_MARKER = b"\x03"
MEDIA_SIDECAR_MARKER = b"\x02"

QUERIES = (
    ("per destination: depth, holds, age", f"""
        SELECT q.telegram_chat_id AS tg_chat, COALESCE(q.thread_id, 0) AS topic,
               COUNT(*) AS queued, SUM(q.delivery_hold IS NOT NULL) AS held,
           SUM(q.delivery_hold IS NULL) AS dispatchable,
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
    # Depth divided by head age is NOT a drain rate here: a backlog created by a
    # burst days ago keeps a large depth against a large age whatever the current
    # speed, and reading it as "messages per minute" misleads. Measure drain with
    # the queue depth over time instead.
    ("head per destination: age, position and retry", f"""
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
               MAX(0, CAST(ROUND(q.reconcile_after - {NOW_SECONDS}) AS INTEGER)) AS retry_in_s,
               COALESCE(q.delivery_hold, '-') AS hold
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

MAX_DECODE_BYTES = 1024 * 1024


class _Unknown:
    """Stand-in for pickled classes this report does not need to understand."""

    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)

    def __init__(self, *args, **kwargs):
        pass

    def __setstate__(self, state):
        if isinstance(state, dict):
            self.__dict__.update(state)
        elif isinstance(state, tuple) and len(state) == 2 and isinstance(state[1], dict):
            self.__dict__.update(state[1])


class _TopicUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module == "builtins":
            return super().find_class(module, name)
        return type(name, (_Unknown,), {})


def _decode(blob: bytes):
    import io

    try:
        return _TopicUnpickler(io.BytesIO(blob)).load()
    except Exception:
        return None


def queued_topic(payload, log_context) -> int | None:
    """The topic a queued row targets, read from wherever the row stored it."""
    topic = None
    if payload and bytes(payload[:1]) == MEDIA_SIDECAR_MARKER:
        value = _decode(bytes(payload[1:]))
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], dict):
            kwargs = value[1]
            topic = kwargs.get("message_thread_id")
            if topic is None:
                api_kwargs = kwargs.get("api_kwargs")
                if isinstance(api_kwargs, dict):
                    topic = api_kwargs.get("message_thread_id")
    if topic is None and log_context and bytes(log_context[:1]) == AGGREGATION_MARKER:
        context = _decode(bytes(log_context[1:]))
        if isinstance(context, dict):
            key = context.get("key")
            if isinstance(key, (tuple, list)) and len(key) > 2:
                topic = key[2]
    try:
        return int(str(topic)) if topic is not None else None
    except (TypeError, ValueError):
        return None


def legacy_topic_rows(db: sqlite3.Connection, out) -> None:
    """Rows whose lane says "main chat" but whose request still names a topic.

    Rows written before the topic column existed kept the topic in the request, so
    they share the chat's main lane with the traffic that really targets it.
    Opening the queue repairs them; this only reports how many are waiting for it.
    """
    rows = db.execute(
        "SELECT id, telegram_chat_id, operation, payload, log_context FROM outbound_queue "
        "WHERE delivery_state = 'queued' AND thread_id IS NULL "
        "AND length(payload) <= ? ORDER BY id",
        (MAX_DECODE_BYTES,),
    ).fetchall()
    recoverable: dict[int, int] = {}
    unknown = 0
    samples: list[tuple[int, int, str, int]] = []
    for row_id, chat_id, operation, payload, log_context in rows:
        thread_id = queued_topic(payload, log_context)
        if thread_id is None:
            unknown += 1
            continue
        recoverable[int(chat_id)] = recoverable.get(int(chat_id), 0) + 1
        if len(samples) < 20:
            samples.append((int(row_id), int(chat_id), str(operation), thread_id))
    print(f"   rows with no topic: {len(rows)}", file=out)
    print(f"   recoverable from the request: {len(rows) - unknown}  "
          f"genuinely the main chat: {unknown}", file=out)
    print("   per chat: " + ", ".join(
        f"{chat_id}={count}"
        for chat_id, count in sorted(recoverable.items(), key=lambda item: -item[1])[:20]), file=out)
    print("   sample rows: " + ", ".join(
        f"#{row_id} {operation} -> topic {thread_id}"
        for row_id, _chat, operation, thread_id in samples), file=out)


def lane(db: sqlite3.Connection, chat_id: int, topic: int, limit: int, out) -> None:
    """Every queued row of one destination, in the order the queue would send them."""
    cursor = db.execute(f"""
        SELECT id, priority, operation, COALESCE(delivery_hold, '-') AS hold,
               {NOW_SECONDS} - CAST(created_at AS INTEGER) AS age_s,
               CASE WHEN delivery_hold IS NULL AND (predecessor_id IS NULL OR NOT EXISTS (
                     SELECT 1 FROM outbound_queue p WHERE p.id = outbound_queue.predecessor_id
                       AND p.delivery_state = 'queued' AND p.delivery_hold IS NULL))
                    THEN 'sendable' ELSE 'waiting' END AS election
        FROM outbound_queue
        WHERE delivery_state = 'queued' AND telegram_chat_id = ? AND COALESCE(thread_id, 0) = ?
        ORDER BY priority DESC, id
        LIMIT ?
    """, (chat_id, topic, limit))
    print("   id | priority | operation | hold | age_s | election", file=out)
    for row in cursor.fetchall():
        print("   " + " | ".join(str(value) for value in row), file=out)


def media_footprint(queue_path: Path, out) -> None:
    """Bytes still owed to Telegram: every upload has to come from these files.

    The directory holds one sidecar per referenced attachment and may still contain
    orphans, so this is an upper bound, not an exact queue size.
    """
    directory = Path(queue_path).parent / "outbound-media"
    total = 0
    files = 0
    if directory.is_dir():
        with __import__("os").scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                        files += 1
                except OSError:
                    continue
    print(f"   media sidecars: {files} files, {total} bytes ({total / 1024 ** 2:.0f} MiB)", file=out)


def resolve_data_dir(value) -> Path:
    if value is not None:
        return Path(value).expanduser().resolve()
    if (Path.cwd() / QUEUE_FILENAME).exists():
        return Path.cwd()
    base = Path(__import__("os").environ.get("EFB_DATA_PATH") or Path.home() / ".ehforwarderbot")
    return base / "profiles" / "default" / MODULE_ID


def report(queue_path: Path, tg_path: Path | None = None, out=None,
           lane_target: tuple[int, int] | None = None, limit: int = 25) -> int:
    if out is None:
        out = sys.stdout
    queue_path = Path(queue_path).resolve()
    if not queue_path.exists():
        raise FileNotFoundError(f"No outbound queue at {queue_path}")
    if tg_path is None:
        tg_path = queue_path.parent / TG_FILENAME
    tg_path = Path(tg_path).resolve()
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
        print("\n== media still to upload", file=out)
        media_footprint(queue_path, out)
        if lane_target is not None:
            print(f"\n== lane {lane_target[0]}:{lane_target[1]} in send order", file=out)
            try:
                lane(db, lane_target[0], lane_target[1], limit, out)
            except sqlite3.Error as error:
                print(f"   skipped: {error}", file=out)
        print("\n== rows stuck in the main lane whose request names a topic", file=out)
        try:
            legacy_topic_rows(db, out)
        except sqlite3.Error as error:
            print(f"   skipped: {error}", file=out)
    return 0


def _join_negative_lane(argv: list[str]) -> list[str]:
    """Telegram chat ids are negative, so `--lane -1001:40` looks like an option."""
    joined: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--lane" and index + 1 < len(argv) and argv[index + 1].startswith("-"):
            joined.append(f"--lane={argv[index + 1]}")
            index += 2
            continue
        joined.append(item)
        index += 1
    return joined


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="Channel data directory (defaults to the current directory "
                             "or the default profile)")
    parser.add_argument("--queue", type=Path, default=None, help="Path to outbound-queue.sqlite3")
    parser.add_argument("--tg", type=Path, default=None, help="Path to tgdata.db")
    parser.add_argument("--lane", default=None, metavar="CHAT[:TOPIC]",
                        help="Also list one destination's queued rows in send order")
    parser.add_argument("--limit", type=int, default=25, help="Rows to list for --lane")
    args = parser.parse_args(
        _join_negative_lane(list(sys.argv[1:] if argv is None else argv))
    )
    lane_target = None
    if args.lane:
        chat, _, topic = args.lane.partition(":")
        lane_target = (int(chat), int(topic) if topic else 0)
    try:
        if args.queue is not None:
            return report(args.queue, args.tg, lane_target=lane_target, limit=args.limit)
        directory = resolve_data_dir(args.data_dir)
        return report(directory / QUEUE_FILENAME, args.tg, lane_target=lane_target, limit=args.limit)
    except (OSError, sqlite3.Error) as error:
        parser.exit(1, f"Queue report failed: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
