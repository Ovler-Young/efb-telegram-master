# SQLite performance and PostgreSQL cutover

## Runtime behavior

The channel has two logical stores. `tgdata.db` holds message logs and associations;
the outbound store consists of `outbound-queue.sqlite3` plus `outbound-media/`.
The SQLite file holds request metadata, compact durable media references and Telegram
completion receipts; queue-owned media bytes live in `outbound-media/`. Selecting
PostgreSQL changes the first store only. Keep both outbound paths in the same channel
data directory.

Startup adds missing nullable historical columns and lookup indexes in one
schema transaction. Existing message IDs, timestamps, duplicate source-message
mappings and association rows are not rewritten or deduplicated. Unknown message
timestamps remain NULL; lookups explicitly keep them last in descending order
and first in ascending order on both backends, preserving SQLite behavior.
PostgreSQL time indexes use the same ordering. On a large
SQLite database the first index build needs disk space and exclusive writer
access; deploy during a maintenance window rather than interrupting the build.

Database connections are borrowed for a database operation and returned on both
success and failure. Nested operations reuse the outer connection. Both backends
use a pool (default 8 connections, 5-second pool acquisition timeout, 300-second
stale timeout). `database.max_connections`, `database.pool_timeout` and
`database.stale_timeout` configure these limits. SQLite retains WAL, a 5-second
busy timeout and the existing synchronous durability setting.

The outbound scheduler selects per-destination head IDs using a covering index.
It does not load upload bytes for every queued item, in-flight destination or
worker/sender that is unavailable. The final rate-limit permit is also checked
before loading a selected message's media. Failed MsgLog reconciliation is limited to 32
receipts per sweep, with persisted exponential delays of 1, 2, 4, 8, 16, 32 and
then 60 seconds. A failed receipt is retained, never resent as a Telegram message
and never discarded just to reduce CPU. Restarting does not reset the delay.

Restart recovery also bounds bytes, not just record counts. Durable log contexts
now store only the inputs needed for MsgLog, not group member caches, local file
buffers, vendor data, or recursively quoted messages. Both historical format 1
and new format 2 are readable. Historical message restoration is log-only: it
must not download, reopen or transcode attachments. Pending contexts are loaded
one at a time; history preparation and resumed history work use batches of 32.

The scheduler waits for enqueue/completion events or the actual retry deadline,
not four full queue scans per second. Automatic receipt recovery yields between
batches for at least 250 ms and at least the preceding batch's work duration.
Fresh live completions still write their own receipt immediately.

The encoded in-flight queue data budget is 128 MiB. New media is streamed into a
queue-owned sidecar file and the SQLite payload stays small; local Bot API `file://`
media that ETM already owns remains an external durable reference. Historical v1
rows may still contain an entire media file inline. An oversized legacy queued row
is quarantined before its BLOB is loaded and blocks only its own destination; other
destinations continue. The row is re-evaluated if the byte budget changes or after
restart. Oversized sent-pending reconciliation state still fails closed because it
contains the only durable Telegram completion receipt. Do not delete such rows or
raise the limit blindly. This budget bounds recovery BLOB loads; it is not a hard
limit on total process RSS, upstream channel caches, or media conversion outside
the queue. During a memory incident, stop the bot and automatic restarts before
diagnosis, keeping both databases, WAL files and `outbound-media/` intact.

Sidecar files are removed after their queue row reaches a terminal state. On startup,
ETM removes queue-owned sidecars that no live v2 row references. If any live v2
payload is corrupt and its references cannot be inspected, orphan cleanup is
intentionally skipped rather than risk deleting still-needed media. Missing media
referenced by a valid live row fails queue startup instead of silently discarding the
row.

## Before migration

Use a dedicated empty PostgreSQL database or schema and install the PostgreSQL
extra for this version of the channel. Peewee 3.19 through 4.x is supported; the
standard PostgreSQL pool avoids version-dependent extension import paths.

Stop the bot, all history workers and **all other writers**, including older
channel versions, external queue consumers and maintenance processes. Do not
leave another host writing to the same PostgreSQL target. The new runtime and
importer share a Unix directory lock; reserved SQLite write locks also prevent
older SQLite writers from changing the snapshot during import. These locks do
not prevent a still-running old bot from making Telegram API calls, so stopping
it remains necessary.

Prepare a private YAML file with the target configuration. The file may be a
full channel configuration, but only its `database` mapping is read. Do not put
the password in the command line or commit the configuration.

```yaml
database:
  type: postgresql
  host: 127.0.0.1
  port: 5432
  database: efb_telegram
  user: efb_telegram
  password: "replace-with-the-database-password"
  max_connections: 8
  pool_timeout: 5
  stale_timeout: 300
  options: "-c timezone=UTC"
```

The target user must be able to create tables, indexes and sequences in the
selected schema. For a non-default schema, create it first and include
`-c search_path=your_schema` in `options`.

Keep an independent backup of the complete channel directory and configuration,
including any files referenced by historical data. Allow space for the original
databases, a new snapshot of both databases on every attempt, PostgreSQL data and
indexes, and PostgreSQL WAL. Snapshots contain private message data and are
placed in a private, uniquely named directory.

## Import and verification

Run this **before** starting the bot with PostgreSQL:

```sh
python -m efb_telegram_master.migrate_db \
  --data-dir /absolute/path/to/the/channel-data-directory \
  --config /private/path/postgresql.yaml \
  --batch-size 500
```

`--data-dir` is the directory containing `tgdata.db`, not the source repository
and not the top-level EFB directory. The command does not change channel
configuration and never constructs a Telegram bot or sends messages.

The importer:

1. Takes ownership of the local directory and fences writes to both SQLite
   stores. It uses SQLite's backup API to create independently readable,
   integrity-checked snapshots, including committed WAL data, and also copies
   `outbound-media/` with a streamed content digest. Existing files, queue media,
   WAL sidecars and any `tgdata.db.migrated` archive are never renamed, deleted
   or overwritten.
2. Imports all five supported application tables into an empty target, preserving
   every known field and primary key. Missing historical nullable fields become
   NULL, not a fabricated current timestamp. Optional topic/history tables may
   be absent. Unknown nonempty tables, unknown columns, invalid timestamps and
   PostgreSQL-incompatible text are rejected rather than silently dropped or
   repaired.
3. Reads source rows incrementally, writes bounded batches (1–1,000 rows), and
   compares ordered row counts and SHA-256 content digests with the target using
   server-side cursors. Unicode ordering, binary fields and microsecond timestamps
   are included. Large BLOBs are not expanded into whole-database JSON or hex
   strings. Sequence positions are reconciled for preserved numeric IDs.
4. Commits rows, indexes, sequence changes and the verified import record in one
   PostgreSQL transaction. A local `.postgresql-cutover.json` receipt is written
   atomically only after commit. The command prints the import ID, per-table
   counts/digests and snapshot directory, never database credentials.

After a successful command, point the channel's `database` configuration at the
same target and start the bot. Keep the original directory and outbound queue.
Startup requires the matching target import record and local cutover receipt;
an existing `chatassoc` table alone no longer counts as a completed migration.
The retained SQLite source is treated as frozen. The receipt blocks accidentally
starting SQLite again in the same directory, and PostgreSQL startup rejects a
source that has changed since cutover. Removing the local source and receipt does
not bypass verification: the imported target still requires its matching receipt.
Every imported table must still exist; a missing table causes startup to fail
rather than silently recreating an empty replacement. Restore the complete target
when recovering an incomplete PostgreSQL restore.
If the import included an outbound queue, that file must still exist at startup;
the runtime refuses to silently replace it with an empty queue. `outbound-media/`
is part of the same durable queue state: valid v2 rows whose referenced media is
missing fail closed rather than being silently dropped. Restore the current queue
and its matching media directory, not a stale pre-send snapshot, when recovering
missing local files.

## Failure and restart behavior

A failure before PostgreSQL commit rolls back the import, including its new
schema. Original SQLite stores and completed backup snapshots remain. Correct
the reported problem and rerun the same command.

A process exit after commit but before the local receipt does **not** trigger a
second import. Rerunning compares the source, queue snapshot and target contents
against the committed import record under target table locks, then recreates
the receipt. It refuses source or target divergence and refuses a receipt from a
different target. Verification is not based on row counts alone.

A preexisting target with no import record is rejected even when its tables are
empty. Use a new empty database/schema; do not delete production target tables
just to satisfy this precondition. Backups use unique names and are intentionally
not auto-pruned. Retain the verified snapshot before manually retiring redundant
attempts.

Re-running the importer is a recovery operation for an unchanged cutover, not an
ongoing synchronizer. Once new PostgreSQL messages or queue state have been
accepted, the old SQLite snapshot is no longer a complete current database.
Do not bypass the receipt or source-change guards to merge histories.

## Rollback boundary

Before the PostgreSQL-backed bot has resumed, rollback can restore the `tgdata.db`
snapshot and the complete outbound snapshot (`outbound-queue.sqlite3` together
with `outbound-media/`) into a fresh channel directory with the original
configuration. Leave the current directory, cutover receipt and PostgreSQL target
intact until the restored copy is verified. This avoids overwriting the only
source or confusing a frozen source with a current queue.

After the bot has resumed, changing `database.type` back to SQLite is not a
lossless rollback: new MsgLog rows, queue removals, retries and Telegram receipts
must first be reconciled. Stop all writers and preserve both current stores;
do not replay a stale outbound snapshot into Telegram.

## Acceptance tests

The tests require no Telegram credentials. CI has a separate `database` job with
a real PostgreSQL 16 service and Peewee 3.19/4.5 coverage. It explicitly runs the
database tests in unit mode with PostgreSQL variables set, so the old
unit/integration selector cannot silently skip them.

With `TEST_POSTGRES_HOST`, `TEST_POSTGRES_PORT`, `TEST_POSTGRES_DB`,
`TEST_POSTGRES_USER` and `TEST_POSTGRES_PASSWORD` pointing at a test server:

```sh
python -m pytest --mode=unit \
  tests/unit/test_database_safety.py \
  tests/unit/test_database_performance.py \
  tests/unit/test_db_migrations.py \
  tests/unit/test_outbound.py \
  tests/unit/test_outbound_queue_runtime_evidence.py
```

Alternatively, on a supported development platform, install the optional test
utility `pgserver` along with `.[tests,tgs,postgresql]`; the acceptance fixtures
start an isolated PostgreSQL server automatically. Every test uses its own target
schema. Coverage includes forced process death before and after commit, active
WAL, old archive preservation, divergent recovery, missing historical columns,
next-ID allocation, pool reuse across short-lived threads, new/reply/edit/reaction
message logs, history backfill persistence, bounded import memory, bounded queue
BLOB reads and retry deadlines across restarts. These are database/queue tests,
not a claim that a production Telegram deployment has been exercised.
