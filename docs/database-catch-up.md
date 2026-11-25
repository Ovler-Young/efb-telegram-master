# Catching up an earlier import

This page continues [database-migration.md](database-migration.md).

An import made from a copy of `tgdata.db` while the bot kept running on SQLite can be
brought up to date with the newer file instead of importing it again. The base is
the SQLite file the target was imported from. Keep it unchanged until the cutover. The
catch-up needs PostgreSQL 15 or later, for `pg_stat_force_next_flush()`; an older server is
refused before anything is written.

```sh
# With the bot still running on SQLite:
python -m efb_telegram_master.catch_up_db --data-dir /absolute/path/to/the/channel-data-directory \
  --config /private/path/postgresql.yaml --catch-up /path/to/the/base/tgdata.db
# After stopping the bot and all other writers:
python -m efb_telegram_master.catch_up_db --data-dir /absolute/path/to/the/channel-data-directory \
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
verify it: the plain `migrate_db` import refuses with "Source changed since the
committed import".
