# SQLite performance and PostgreSQL cutover

## Runtime behavior

The channel has two logical stores. The first holds message logs and associations
(`tgdata.db` on SQLite). The outbound store holds request metadata, compact durable
media references and Telegram completion receipts, and its media bytes live in
`outbound-media/`. The `database` setting decides where both stores are kept; there is
no separate setting and no mixed mode:

- On SQLite the outbound rows are in `outbound-queue.sqlite3`, next to `tgdata.db`.
- On PostgreSQL they are in three tables of the same schema as the message tables:
  `outbound_queue`, `history_ownership` and `source_cancellations`. Receipts and holds
  are columns of `outbound_queue`. The startup schema transaction creates them.

Media bytes are never stored in PostgreSQL. `outbound-media/` stays a directory in the
channel data directory on both backends, so keep it with that directory and back it up
with the database.

Startup adds missing nullable historical columns and lookup indexes in one
schema transaction. Existing message IDs, timestamps, duplicate source-message
mappings and association rows are not rewritten or deduplicated. Unknown message
timestamps remain NULL; lookups explicitly keep them last in descending order
and first in ascending order on both backends, preserving SQLite behavior.
PostgreSQL time indexes use the same ordering.

The one-time startup work depends on which objects are missing: nullable columns
(including `msglog.master_message_thread_id` and history `generation`), the
`historymigrationtarget` table, MsgLog source/chat/time/history/alternate-ID
indexes, history-generation/target/cleanup indexes, association indexes and the
`slavechatinfo` identity lookup index. SQLite runs this schema work inside an
IMMEDIATE transaction; other writers wait while missing indexes are built.
Existing indexes are reused via `CREATE INDEX IF NOT EXISTS`. On SQLite the outbound
store separately adds missing receipt/retry/hold columns, `history_ownership`, and
its scheduling and reconciliation indexes without rebuilding the queue table. On
PostgreSQL the queue tables and their indexes are created in the startup schema
transaction, complete. The queue itself never creates them: if one is missing, queue
startup fails instead of opening an empty queue that could drop pending sends.

These schema/index builds are distinct from recurring startup work: inactive
history staging is scanned and reclaimed in pages of 256 after the schema
transaction, and queue startup inspects media references and removes orphans.
A first index build can delay startup on a large store; allow it to finish
with other writers stopped. The code provides no fixed duration.

Database connections are borrowed for a database operation and returned on both
success and failure. Nested operations reuse the outer connection. Both backends
use a pool (default 8 connections, 5-second pool acquisition timeout, 300-second
stale timeout). `database.max_connections`, `database.pool_timeout` and
`database.stale_timeout` configure these limits. SQLite retains WAL, a 5-second
busy timeout and the existing synchronous durability setting. The outbound queue does
not use the pool: on PostgreSQL it keeps one dedicated session for its whole life, with
a 5-second lock timeout.

The outbound scheduler selects per-destination head IDs using a covering index.
It does not load upload bytes for every queued item, in-flight destination or
worker/sender that is unavailable. The final rate-limit permit is also checked
before loading a selected message's media. Failed MsgLog reconciliation is limited to 32
receipts per sweep, with persisted exponential delays of 1, 2, 4, 8, 16, 32 and
then 60 seconds. A failed receipt is retained, never resent as a Telegram message
and never discarded just to reduce CPU. Restarting does not reset the delay.

Durable log contexts store only the inputs needed for MsgLog, not group member
caches, local file buffers, vendor data, or recursively quoted messages. Both
historical format 1 and new format 2 are readable. Historical message restoration
is log-only: it must not download, reopen or transcode attachments. Pending
contexts are loaded one at a time; history preparation and resumed history work
use batches of 32.

The scheduler waits for enqueue/completion events or the actual retry deadline,
not four full queue scans per second. Automatic receipt recovery yields between
batches for at least 250 ms and at least the preceding batch's work duration.
Fresh live completions still write their own receipt immediately.

The encoded in-flight queue data budget is 128 MiB. New media is streamed into
queue-owned sidecars; local Bot API `file://` media that ETM already owns can
remain an external durable reference. Runtime payload-version checks and startup
media inspection use incremental BLOB access. On SQLite, Python 3.11+ uses native
`sqlite3.Connection.blobopen`; Python 3.10 uses an isolated helper process with a
system SQLite library. The helper does not load a second SQLite library into the
queue process, so closing its handles cannot release the queue's writer locks.
On PostgreSQL the same reads fetch the payload with `substring()` in 1 MiB chunks on
the queue's own session; the payload column is created with `STORAGE EXTERNAL`, so a
chunk does not decompress the whole value, and no helper process is involved.
If incremental access is unavailable, recovery fails rather than falling back to
an unbounded payload read.

Oversized legacy v1 media is converted on demand before dispatch. The converter
reads pickle opcodes incrementally and streams recognized inline `BytesIO` media
to sidecars, preserving filenames and media metadata. Non-media values and log/
receipt metadata must fit the encoded budget; oversized opaque data is retained
for offline recovery. A reserved writer transaction (on PostgreSQL, a transaction
that locks the row) protects the original row until the compact v2 replacement
commits. This conversion is distinct from the
first-start schema/index upgrade; startup does not bulk-rewrite every v1 row.

Oversized non-v1 queued rows are quarantined. Payloads that fail decoding are
retained with `delivery_hold='invalid_payload'`; later runnable rows, including
those for the same destination, can continue. Failed preparation or conversion
removes only newly created sidecars, preserving preexisting media and external
files. Partial decode failure closes handles already opened. Oversized
sent-pending reconciliation state fails closed because it contains the durable
Telegram completion receipt. Do not delete retained rows or raise the limit
blindly.

The encoded budget is not a total-process RSS limit: ordinary dispatch still
materializes size-checked payloads, and decoded objects, upstream caches and
media conversion add allocations. The offline migration backup/digest routines
also read queue rows directly; runtime incremental recovery is not a streaming
contract for those separate routines. During a memory incident, stop the bot and
automatic restarts before diagnosis, keeping both databases, any SQLite WAL files
and `outbound-media/` intact.

Sidecar files are removed after their queue row reaches a terminal state. One
queue owner holds `.outbound-queue.lock` for its lifetime, covering startup
cleanup and sidecar publication; a second owner fails before inspecting or
reclaiming media. Do not remove that lock file while an owner is running. On
PostgreSQL the queue also holds a session-level advisory lock for its schema, so a
second ETM that uses the same schema, from another data directory or another host, is
refused with the same "already in use by another owner" error; the lock ends with the
queue's session.

On PostgreSQL the queue does not reconnect. If the server restarts or drops the queue's
session (for example when a database container is updated or restarted), queue calls
fail and the outbound worker stops sending: it logs `Outbound recovery stopped; durable
rows retained` and sends nothing until ETM is restarted. Nothing queued is lost. Rows
stay in PostgreSQL, and a send that was in flight stays held for `/confirm_send`, as
after any restart. Restart ETM after a database restart, and avoid automatic restarts or
image updates of the database container.

PostgreSQL text cannot contain NUL characters, which SQLite stores. A message whose
slave chat ID contains one cannot be queued on PostgreSQL: its enqueue fails with
`Unable to commit queued Telegram call.` The queue move refuses a queue row, in any
text column, that contains one and names the row (see below).

Startup inspects payload markers incrementally and skips known v1 payloads.
An unknown payload version, or any live v2 payload exceeding the inspection
budget or failing to decode, disables orphan cleanup to preserve potentially
referenced files. Otherwise, unreferenced queue-owned sidecars are removed.
Missing media referenced by a valid inspected row fails queue startup instead
of silently discarding the row.

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
indexes (the queue rows included), and PostgreSQL WAL. Snapshots contain private message data and are
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
   `outbound-media/` with a streamed content digest. Referenced external local
   attachments are copied into archive-owned sidecars; only the archived queue
   payloads are rewritten to reference those copies. Original external files
   and live queue references are unchanged. Missing attachments abort backup,
   and content hashes detect changes during copying or import. Existing files,
   queue media, WAL sidecars and any `tgdata.db.migrated` archive are never
   renamed, deleted or overwritten.
2. Imports the six application tables, including published history targets,
   into an empty target, preserving known fields and primary keys, including
   `msglog.master_message_thread_id`. Missing historical nullable fields become
   NULL, not a fabricated current timestamp; optional topic/history tables may
   be absent. The known `topiciconcache` and `useremojicache` tables are also
   imported when present, using their actual supported column types, nullability
   and simple primary keys, including schemas without an integer `id`. This is
   not arbitrary SQLite DDL conversion. Unknown nonempty tables, unsupported
   columns/types, non-text values in text columns, generated or hidden columns,
   cache foreign keys, invalid timestamps and text containing U+0000 in any
   column other than the message and chat text columns below are rejected.
   PostgreSQL text cannot hold U+0000. In `msglog.text`,
   `historymigrationentry.formatted_text`, `slavechatinfo.slave_chat_name` and
   `slavechatinfo.slave_chat_alias`, each U+0000 is stored as U+FFFD, one for
   one, both by the importer and by the running bot. This is lossy on the
   PostgreSQL copy: the original text stays in the retained SQLite file and in
   the importer's backup. Text the bot logs later on PostgreSQL has no such
   original. The import logs, per column, how many values were replaced, never
   their content.
3. Reads source rows incrementally, writes bounded batches (1–1,000 rows), and
   compares ordered row counts and SHA-256 content digests with the target using
   server-side cursors. Unicode ordering, binary fields and microsecond timestamps
   are included. Large BLOBs are not expanded into whole-database JSON or hex
   strings. Sequence positions are reconciled for preserved numeric IDs.
4. Commits rows, indexes, sequence changes and the verified import record in one
   PostgreSQL transaction. A local `.postgresql-cutover.json` receipt is written
   atomically only after commit. The command prints the import ID, per-table
   counts/digests and snapshot directory, never database credentials.
5. Moves the outbound queue in a second PostgreSQL transaction, in the same run
   (see "Moving the outbound queue" below).

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
If the import included an outbound queue, that file must still exist at startup, also
after the queue moved into PostgreSQL: it stays in place as a frozen copy that is never
resumed, and the runtime refuses to silently replace it with an empty queue.
`outbound-media/` is part of the same durable queue state: valid v2 rows whose
referenced media is missing fail closed rather than being silently dropped. Restore the
current queue and its matching media directory, not a stale pre-send snapshot, when
recovering missing local files.

## Moving the outbound queue

With PostgreSQL configured the queue lives in PostgreSQL too, so the importer moves it
after the data step, in a second PostgreSQL transaction of the same run and the same
downtime. It needs `outbound-queue.sqlite3` in the data directory. If there is none,
there is nothing to move and PostgreSQL startup creates an empty queue. The move needs
PostgreSQL 12 or later, for the `sha256()` function and the floating-point output its
digests compare. The queue step:

1. Fences the SQLite queue and snapshots it, with `outbound-media/`, into the backup
   directory of the run. That snapshot is the rollback copy.
2. Reads the **live** queue file read-only, not the snapshot. Its rows keep naming the
   live `outbound-media/`, which the PostgreSQL queue keeps using.
3. Validates before writing and refuses, naming the row, anything PostgreSQL would
   change: a value of the wrong type or out of range, a NUL character in text, or a
   value over 256 MiB. It also refuses unknown columns and nonempty unknown tables. A
   queue written by an older version that lacks later columns gets the defaults that
   version's own upgrade would add.
4. Requires empty queue tables, or the tables of its own verified earlier move, and
   copies the rows with their IDs, in batches limited by their bytes rather than by row
   count. It sets the ID sequence beyond every ID the SQLite queue ever issued, deleted
   ones included, because an aggregation context or an ordering link may still name one.
5. Compares row counts and SHA-256 digests with the source, checks again that the live
   queue and its media are unchanged, then commits the rows and the queue import record
   (`etm_queue_import`) in one transaction. The step refuses while a bot holds the
   queue's owner lock.
6. After the commit it writes `.outbound-queue-postgresql.json` in the data directory,
   atomically. The SQLite queue file and `outbound-media/` are left in place and are never
   renamed, deleted or rewritten.

Startup on PostgreSQL then requires the queue import record, all three queue tables and
that file with the matching import ID. Without the file it stops with a message saying the
move is unfinished; run the same command again to verify and finish it.

### An install that already runs on PostgreSQL

Versions without the queue move kept the outbound queue in `outbound-queue.sqlite3` even
with PostgreSQL configured. After upgrading such an install, whether its data was imported
earlier or it started on PostgreSQL with a leftover queue file, PostgreSQL startup finds the
file and no queue import record and refuses to start. Startup never moves the queue by
itself, so an automatic restart policy repeats the refusal until the queue is moved. Stop
the bot and all other writers, then run:

```sh
python -m efb_telegram_master.migrate_db \
  --data-dir /absolute/path/to/the/channel-data-directory \
  --config /private/path/postgresql.yaml \
  --queue-only
```

On an install whose queue kept running after an earlier data import, only `--queue-only`
works: the plain command compares the recorded queue state, which no longer matches, and
reports that the source changed. `--queue-only` moves just the queue and does not need
`tgdata.db`. If `tgdata.db` is present, the directory's cutover receipt must name the
target's import. Without one (`tgdata.db` was never imported, or the import did not
finish) it refuses and asks for the plain command, which verifies or moves the data and
the queue together. With another import's receipt, as when the configuration names another
install's target, it refuses as the plain command does and writes nothing. An install with
no queue file starts with an empty queue.

### Older versions after the queue move

**Never start an older version on this data after the queue move.** Older versions do not
know the PostgreSQL queue. With the PostgreSQL configuration they reopen the retained
`outbound-queue.sqlite3` and send again what the PostgreSQL queue has already sent.

Nothing stops an older version on any install: this warning is the only protection.

- A current version configured for SQLite in a directory whose queue moved refuses to open
  the retained SQLite queue, and an imported install is also blocked by its cutover receipt.

## Catching up an earlier import

An import made from a copy of `tgdata.db` while the bot kept running on SQLite can be
brought up to date with the newer file instead of importing it again. The base is
the SQLite file the target was imported from. Keep it unchanged until the cutover. The
catch-up needs PostgreSQL 15 or later, for `pg_stat_force_next_flush()`; an older server is
refused before anything is written.

```sh
# With the bot still running on SQLite:
python -m efb_telegram_master.migrate_db --data-dir /absolute/path/to/the/channel-data-directory \
  --config /private/path/postgresql.yaml --catch-up /path/to/the/base/tgdata.db
# After stopping the bot and all other writers:
python -m efb_telegram_master.migrate_db --data-dir /absolute/path/to/the/channel-data-directory \
  --config /private/path/postgresql.yaml --catch-up /path/to/the/base/tgdata.db --final
python -m efb_telegram_master.migrate_db --data-dir /absolute/path/to/the/channel-data-directory \
  --config /private/path/postgresql.yaml --queue-only
```

What it compares with the base file:

- `msglog`: the rows from the first one whose time is at most `--window-days` (default 7)
  before the base file's newest message, found per chat through the history index; every
  row added after the base file; and the rows that earlier runs updated, which the import
  record lists, so an edit that was undone later is applied too. Changed rows are
  written with an upsert that leaves equal rows alone.
- `msglogmember`: only the rows added after the base file. The bot never updates or
  deletes them.
- The other tables are compared whole and replaced whole when they differ. Id sequences
  are reset as on import.

**The window is an assumption.** A change to a message older than the window is not seen,
for example a user editing old history or a queued edit held for longer than the window.

Neither SQLite file is written, and a rerun with the same files writes nothing. A run
without `--final` on a WAL-mode `tgdata.db` that no process has open leaves an empty
`tgdata.db-wal` and a `tgdata.db-shm` beside it.

- Without `--final`, `tgdata.db` is read while the bot runs: read-only, in one read
  transaction, so the run sees one consistent state of the file. Its WAL is expected. The
  run commits the changes and the catch-up keys of the import record, keeps the import ID
  and writes no receipt. After the commit it records PostgreSQL's row counts and write
  counters as a baseline for the next run.
- `--final` needs the bot stopped. It takes the data directory lock and reads `tgdata.db`
  as an immutable file. Rows, read-back of the written rows, row-count checks and the
  rewritten import record are one PostgreSQL transaction. The record gets a new import
  ID, the final row counts, empty digests and no queue. The run then writes
  `.postgresql-cutover.json` with the fingerprint of `tgdata.db`, after its connections
  to the file are closed. Move the queue with `--queue-only` and start the bot.

Each run refuses before writing anything when:

- `tgdata.db-wal` is not empty in the final run;
- `msglog` or `msglogmember` of `tgdata.db` has a different number of rows than the base
  file plus the added rows, or a highest rowid below the base file's: rows were deleted or
  the file was rebuilt, for example by `VACUUM`. Import the file in full instead;
- PostgreSQL's columns differ from the import record, or its row counts differ from the
  record (first run) or from the last run's baseline, or its write counters changed since
  that run: something else wrote PostgreSQL. Write counters are statistics. A write
  committed a few seconds before a run may not be counted yet, and a server crash resets
  them, so that the next run refuses. A run that fails after its first write is rolled
  back and records the new counters as its baseline, so a retry is not refused; a run
  killed mid-write (the process dies) cannot, and the next run refuses;
- the base file is not the file the target was imported from, or not the one earlier
  runs used.

The import record has no digests after `--final`, so the plain importer can no longer
verify it: rerun without `--catch-up`, it refuses with "Source changed since the
committed import".

## Failure and restart behavior

A failure before PostgreSQL commit rolls back the import, including its new
schema. Original SQLite stores and completed backup snapshots remain. Correct
the reported problem and rerun the same command.

A failure in the queue step rolls back its rows and its import record; the SQLite queue,
its media and the snapshot remain. Rerun the same command: it verifies the data import
again, which still matches because nothing ran in between, and retries the queue step. A
process exit after the queue commit but before `.outbound-queue-postgresql.json` is
written is finished by the same rerun, which verifies the PostgreSQL rows against the
queue import record and writes the file; startup refuses until it exists. A rerun is
refused once the live queue file or its media differ from what was moved, for example after
the bot has run on the moved queue. Divergent queues are never merged.

A process exit after commit but before the local receipt does **not** trigger a
second import. Rerunning compares the source, queue snapshot and target contents
against the committed import record under target table locks, then recreates
the receipt. It refuses source or target divergence and refuses a receipt from a
different target. Verification is not based on row counts alone.

Recovery accepts legacy v1 and v2 import records, and a v3 record written by an earlier
build, which it treats as v2. V1 uses its fixed historical column projection; v2 uses the
recorded projection. Added columns omitted by an
old projection must remain NULL and omitted tables must remain empty on both
sides. Nonempty unrecorded caches or history publications are rejected. If a
legacy record lacks external-file hashes, recovery verifies the currently
available files and establishes the first content baseline with a warning; it
cannot detect changes predating that baseline.

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
source or confusing a frozen source with a current queue. Use a revision that
understands the retained queue receipts: newer code reads older receipt formats,
but older code cannot read receipts carrying a separate file-ID issuer. Restoring
SQLite does not itself make an older executable compatible.

After the queue step has committed, the retained `outbound-queue.sqlite3` and the live
`outbound-media/` are not a rollback source. The PostgreSQL queue keeps deleting media
files that the frozen rows still name, and those rows would be sent again. Roll back only
from the archived copies of the queue and of `outbound-media/` in the run's backup
directory (`database-backups/sqlite-*`), restored into a fresh channel directory, and never
start an older version against the live data.

For an unchanged PostgreSQL cutover whose local directory must be relocated:

1. Keep all writers stopped. Copy the complete verified archive into a fresh
   channel directory, including `tgdata.db`, `outbound-queue.sqlite3`,
   `outbound-media/` and `manifest.json`. External attachment copies are already
   referenced by portable storage names; the original absolute paths are not
   required for those archived queue rows. After the queue move, the queue that runs is
   the PostgreSQL copy of the live rows, which keep their original `file://` references:
   those external media files must exist at the same paths on the relocated host.
2. Run the same importer command with the new `--data-dir` and the original
   PostgreSQL target configuration. Do not simply copy the old local cutover
   receipt: source file fingerprints change when files are relocated.
3. The importer verifies the unchanged target and source contents and accepts
   the recorded portable `backup_queue` digest, then writes a matching local
   receipt. Modified queue metadata or attachment content is rejected. Start
   the bot only after verification succeeds.

The recorded original and portable queue digests cover different representations
of the same backup. They are integrity checks against the committed import
record, not authorization to use a stale queue after Telegram work has resumed.

After the bot has resumed, changing `database.type` back to SQLite is not a
lossless rollback: new MsgLog rows, queue removals, retries and Telegram receipts
must first be reconciled. A moved queue is not moved back by any tool, and the
queue move cannot be repeated once the live media no longer match the record. Stop all writers and preserve both current stores;
do not replay a stale outbound snapshot into Telegram.

## Acceptance tests

The tests require no Telegram credentials. CI has a separate `database` job with
a real PostgreSQL 16 service and Peewee 3.19/4.5 coverage. It explicitly runs the
database tests in unit mode with PostgreSQL variables set, so the old
unit/integration selector cannot silently skip them. The queue tests run on SQLite and,
when the PostgreSQL variables are set, on PostgreSQL too (a `make_queue` fixture in
`tests/unit/conftest.py` builds the queue on the backend under test), apart from tests that
need a SQLite-only feature such as WAL or the Python 3.10 BLOB helper.

With `TEST_POSTGRES_HOST`, `TEST_POSTGRES_PORT`, `TEST_POSTGRES_DB`,
`TEST_POSTGRES_USER` and `TEST_POSTGRES_PASSWORD` pointing at a test server:

```sh
python -m pytest --mode=unit \
  tests/unit/test_database_safety.py \
  tests/unit/test_database_performance.py \
  tests/unit/test_db_migrations.py \
  tests/unit/test_outbound.py \
  tests/unit/test_outbound_queue_runtime_evidence.py \
  tests/unit/test_restart_memory.py
```

The CI job's full file list is in `.github/workflows/tests.yml`; it adds the delivery,
history, scheduler, aggregation and queue report tests to the files above.

Alternatively, on a supported development platform, install the optional test
utility `pgserver` along with `.[tests,tgs,postgresql]`; the acceptance fixtures
start an isolated PostgreSQL server automatically. Every test uses its own target
schema. Coverage includes forced process death before and after commit, active
WAL, old archive preservation, divergent recovery, missing historical columns,
next-ID allocation, pool reuse across short-lived threads, new/reply/edit/reaction
message logs, history backfill persistence, incremental import verification,
scheduler byte checks, incremental legacy-media recovery, startup BLOB inspection,
retry deadlines across restarts, the queue move (every row state, receipts, holds,
cancellations, old column sets, refused sources, a changed source, a crash after the
commit, an upgrade install refused until moved) and the single queue owner. These are database/queue tests, not a claim
that a production Telegram deployment has been exercised or that a particular
release revision has passed CI.
