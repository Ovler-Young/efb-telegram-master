# Database and recovery validation

This document maps the current contracts to implementation and regression-test
sources. It is not a release acceptance result. Earlier database-phase suite
counts and isolated benchmarks do not establish validation of subsequent history,
delivery or provenance changes. CI configuration describes checks to run, not
proof that a particular revision passed them or that production was exercised.

## History preparation and ownership

[`DatabaseManager.replace_history_migration_entries`](../efb_telegram_master/db.py)
reads a finite snapshot into a temporary disk spool before taking write locks.
It inserts staging pages in short transactions, publishes one generation pointer,
and reclaims obsolete entries in batches. Startup reclamation occurs after the
schema transaction. Indexed seeks avoid repeatedly scanning consumed history.

[`OutboundQueue.enqueue_many`](../efb_telegram_master/outbound.py) commits outbound
rows and generation-qualified ownership keys together. The keys outlive completed
queue rows until staging cleanup finishes.
[`ChatBindingManager`'s history processing](../efb_telegram_master/chat_binding.py)
skips entries already owned by the queue on restart. This is an idempotent
ownership handoff, not an exactly-once Telegram delivery guarantee.

[`test_history_runtime_recovery.py`](../tests/unit/test_history_runtime_recovery.py)
covers finite snapshots during live writes, interrupted publication, indexed
paging, ownership handoff interruptions and startup reclamation.
[`test_history_replay.py`](../tests/unit/test_history_replay.py) and
[`test_history_sender.py`](../tests/unit/test_history_sender.py) cover batching
across pages and original senders, plus media acquisition independent of sending.
Normal sends and history text batches are never pinned to an original bot.

## Schema and migration

[`DatabaseManager._check_and_run_migrations` and `_create_lookup_indexes`](../efb_telegram_master/db.py)
add missing nullable columns and indexes within the startup schema transaction.
SQLite uses an IMMEDIATE transaction: first-time index construction delays
startup and competing writers. Existing indexes are reused. The queue separately
adds its missing metadata, ownership table and indexes in
[`OutboundQueue._open` and `_migrate_schema`](../efb_telegram_master/outbound.py).
Recurring history reclamation and queue-media inspection are separate startup
costs. No machine-independent startup duration follows from this code.

[`migrate_db.py`](../efb_telegram_master/migrate_db.py) preserves the production
`master_message_thread_id` column, history generations and publication targets,
and supported actual schemas of `topiciconcache` and `useremojicache`. Its
`_recovery_columns` and `_validate_recovery_extras` accept legacy v1/v2 import
projections only while omitted columns are NULL and omitted tables are empty.
Unsupported generated/hidden columns are rejected rather than lost.

`_backup_queue` copies referenced external attachments into archive-owned
sidecars and rewrites only archived payloads. `_queue_digest` and recovery verify
both original and portable backup representations against the committed import
record. Changed queue metadata or media is rejected. Legacy records without
external hashes can establish a first baseline but cannot detect earlier changes.

[`test_database_safety.py`](../tests/unit/test_database_safety.py) covers transaction
rollback, forced exits around commit, receipt reconstruction, WAL backups,
source/target divergence, thread/cache preservation, legacy projections,
relocated external archives, tampering and missing files. Its
`test_import_recovery_cutover_preserves_published_history_and_queue_ownership`
checks the history state across cutover. Follow
[the migration guide](database-migration.md) for restore and rollback procedures.

## Delivery and file-ID provenance

[`OutboundQueue.record_telegram_completion`](../efb_telegram_master/outbound.py)
commits the primary receipt and separate supplemental request atomically.
[`TelegramBotManager.record_queued_failure`](../efb_telegram_master/bot_manager.py)
distinguishes acquisition-only failures from uncertain sends. Transient
acquisition retries persist their backoff; an ambiguous copy/upload response
still requires confirmation. Supplemental failure never authorizes resending a
confirmed primary message.

`TelegramBotManager`'s receipt encoder/decoder retains the actual author and,
when provided, a separate file-ID issuer. [`MsgLog.file_bot_id`](../efb_telegram_master/db.py)
uses that issuer for acquisition and falls back to the recorded author for legacy
rows. Historically misrecorded owners are not retroactively repaired. New code
reads old receipts; older code cannot decode new issuer-bearing receipts. This
one-way format compatibility does not support rollback.

[`test_delivery_uncertainty.py`](../tests/unit/test_delivery_uncertainty.py) covers
response loss, durable supplemental work, acquisition retries across restart,
confirmation and distinct author/issuer provenance. Credential-dependent tests
in [`tests/integration`](../tests/integration) exercise live Telegram behavior;
their presence is not evidence that they ran successfully for a release.

## Queue recovery and memory boundary

[`_SQLiteBlobReader`](../efb_telegram_master/outbound.py) supplies incremental
payload access for version checks, startup inspection and legacy-media recovery.
Python 3.11+ uses native `blobopen`. Python 3.10 runs the system SQLite reader in
an isolated helper process, preserving the queue process's writer locks when the
helper closes its handles. Unavailable incremental access fails closed.

`OutboundQueue._stream_legacy_pickle` streams recognized inline `BytesIO` media
to sidecars while bounding the retained non-media pickle and accounting for log/
receipt metadata. `recover_legacy_media_payload` holds a reserved writer
transaction until the compact replacement commits. Filenames, MIME metadata and
aliased media references survive conversion. Oversized opaque data is retained;
this is not a blanket conversion of arbitrary bytes into attachments.

Preparation and enqueue failures clean up newly created sidecars only. Partial
decode failure closes opened handles. Invalid payloads receive a durable
`invalid_payload` hold instead of being discarded, allowing later runnable rows
to proceed. Startup skips non-v2 payloads after reading their markers and skips
orphan deletion when an oversized or corrupt v2 payload cannot be inspected.

[`test_restart_memory.py`](../tests/unit/test_restart_memory.py) covers incremental
recovery, startup inspection, oversized opaque-data retention and interrupted
conversion. [`test_outbound.py`](../tests/unit/test_outbound.py) covers partial
preparation/decode cleanup, pickle aliases, filename/MIME preservation and a
competing writer remaining blocked until the recovery transaction commits.
These are regression sources, not a report that final exact-revision CI passed.

The 128 MiB limit bounds encoded queue data, not total RSS. Ordinary dispatch
materializes size-checked payloads and decoding adds allocations. Offline
`migrate_db._backup_queue` and `_queue_digest` still read queue rows directly;
they do not use the runtime incremental legacy converter. Their external-media
file copies and hashes are streamed, but this does not establish bounded memory
for arbitrary inline queue BLOBs during offline migration.

## Running verification

The [test workflow](../.github/workflows/tests.yml) defines a dedicated PostgreSQL
service job, a Peewee matrix and SQLite compatibility checks. Database tests use
isolated schemas; live Telegram checks require credentials and their configured
integration environment. Neither phase-specific CI nor offline checks alone
constitute final release or production acceptance.

For the database/history/delivery contracts, use the existing unit-mode tests
with `TEST_POSTGRES_HOST`, `TEST_POSTGRES_PORT`, `TEST_POSTGRES_DB`,
`TEST_POSTGRES_USER` and `TEST_POSTGRES_PASSWORD` configured for a test server:

```sh
python -m pytest --mode=unit \
  tests/unit/test_database_safety.py \
  tests/unit/test_database_performance.py \
  tests/unit/test_db_migrations.py \
  tests/unit/test_history_runtime_recovery.py \
  tests/unit/test_history_replay.py \
  tests/unit/test_history_sender.py \
  tests/unit/test_delivery_uncertainty.py \
  tests/unit/test_outbound.py \
  tests/unit/test_restart_memory.py
```

Record the actual revision, command, environment and results when running these
checks. Do not infer a passing run from this source map.
