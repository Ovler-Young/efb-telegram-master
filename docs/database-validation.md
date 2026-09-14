# Database change validation — 2026-09-14

Base: `673fb3c85bc5244437d2c762e74fb6e79f9617d9`.
Branch: `fix/sqlite-performance-safe-postgres`.

## Executed acceptance matrix

All three runs used a real, isolated PostgreSQL 16.2 server. Each acceptance test
created a separate schema; no production database was accessed.

| Python | Peewee | Full unit suite | Database availability |
| --- | --- | --- | --- |
| 3.10.20 | 3.19.0 | 406 passed, 68 skipped | PostgreSQL configured |
| 3.12.3 | 4.5.1 | 406 passed, 68 skipped | PostgreSQL configured |
| 3.14.6 | 4.5.1 | 406 passed, 68 skipped | PostgreSQL configured |

The 68 skips require Telegram credentials in the existing test suite. They are
not migration-test skips and are not represented as passing live Telegram tests.
The old dedicated PostgreSQL legacy-table test ran with PostgreSQL variables set.
The suite emitted the existing upstream `pkg_resources` deprecation warning.

The new database suites cover real transaction rollback, forced process exit
before and after commit, receipt reconstruction without duplicate inserts, active
WAL backups, old archive preservation, source/target/queue divergence, invalid
manifests, unsupported historical data, optional nullable column projection,
primary-key preservation and subsequent sequence allocation, connection return
across 24 short-lived threads, nested connection ownership, new/reply/edit/reaction
log operations and restartable history backfill.

The large-import test streams 6,003 messages, including 6,000 additional rows with
6,144-character text, using batches of 32; Python peak allocation must remain
below 12 MiB. This catches whole-table Python materialization on both source and
verification paths. It does not assert a machine-independent import throughput.

Queue tests enforce bounded BLOB loading, priority/FIFO preservation, metadata-only
selection while workers or senders are unavailable, bounded fair reconciliation,
persisted capped exponential deadlines across restarts, no resend of accepted
Telegram receipts, and fail-stop retention when retry persistence itself fails.

## Same-process queue benchmark

Python 3.12, SQLite 3.45.1, temporary WAL-backed file, 2,000 queued records,
20 destinations, 64 KiB payload per record (125 MiB total). The original `heads`
method was extracted directly from the base commit and run against the same
queue as the new methods. Timings are medians of seven calls; memory is Python
peak allocation measured separately. All versions returned identical 20 head IDs.

| Method | Median wall time | Median CPU time | Python peak allocation |
| --- | ---: | ---: | ---: |
| Original full-queue heads | 61.320 ms | 61.314 ms | 125.448 MiB |
| New heads with selected payloads | 1.094 ms | 1.092 ms | 1.257 MiB |
| New scheduler metadata heads | 0.312 ms | 0.310 ms | 0.006 MiB |

The metadata-only call is followed by loading the specific payload when it can
actually be dispatched. These are isolated queue-method measurements, not a
production CPU reduction or end-to-end Telegram throughput claim.

## Static and configuration checks

`mypy -p efb_telegram_master --ignore-missing-imports` passed on Python 3.10 and
3.14 for all 53 source files. The same package check also passed on Python 3.12.
Ruff's fatal/error/unused-name checks (`--isolated --select F,E9`) passed for
changed implementation and new test modules. `git diff --check` and workflow YAML
parsing passed. Existing unrelated legacy style was not reformatted.

CI now includes a separate PostgreSQL service job with a Peewee 3.19/4.5 matrix.
It runs independently of Telegram credentials and explicitly selects the database
unit tests, closing the previous mode/environment-variable coverage gap.

## Deployment boundary

This branch contains the implementation and offline acceptance tests. No live
Telegram credentials or production `tgdata.db` were available in the workspace;
no production database, outbound queue, configuration, running bot or existing
pull request was changed. Follow `database-migration.md` for the explicit offline
cutover and rollback boundary.
