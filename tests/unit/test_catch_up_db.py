"""Catch-up import acceptance tests: real PostgreSQL, no Telegram credentials."""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing

import pytest
from peewee import SqliteDatabase

from efb_telegram_master import catch_up_db, migrate_db
from efb_telegram_master.db import ChatAssoc, DatabaseManager
from efb_telegram_master.db_runtime import connection_scope, postgresql_database
from tests.unit.test_database_safety import _as_an_earlier_import, manager_factory, sqlite_source  # noqa: F401

MODELS = migrate_db.MODELS


def _add_message(connection, key, time, text="text"):
    connection.execute(
        "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, msg_type, sent_to, time) "
        "VALUES (?, ?, ?, 'slave.chat', 'Text', 'blueset.telegram', ?)", (key, "slave." + key, text, time),
    )


def _catch_up_pair(sqlite_source, config, tmp_path):
    """Import a source as an earlier version did; return that file's copy and a data directory to catch up."""
    indexes = SqliteDatabase(str(sqlite_source / "tgdata.db"))
    DatabaseManager._create_lookup_indexes(indexes)
    indexes.close()
    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, source:
        # The newest message is 中.3 (2021-07-08): the 7-day window starts at its rowid 3.
        _add_message(source, "recent.4", "2021-07-05 00:00:00")
        _add_message(source, "old.5", "2021-06-01 00:00:00")
        source.execute("INSERT INTO msglogmember (master_msg_id, slave_origin_uid, slave_message_id) "
                       "VALUES ('z.1', 'slave.chat', 'member.1')")
    _as_an_earlier_import(sqlite_source, config)
    base = tmp_path / "base" / "tgdata.db"
    base.parent.mkdir()
    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, closing(sqlite3.connect(base)) as copy:
        source.backup(copy)
        copy.execute("PRAGMA journal_mode=DELETE")
    directory = tmp_path / "live"
    shutil.copytree(sqlite_source, directory, ignore=shutil.ignore_patterns(
        "tgdata.db*", "database-backups", migrate_db.RECEIPT_FILE))
    shutil.copyfile(base, directory / "tgdata.db")
    return base, directory


def _files(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            for path in directory.rglob("*") if path.name != migrate_db.RECEIPT_FILE}


def _catch_up(directory, config, base, **kwargs):
    """Run a catch-up and check that neither SQLite file nor anything beside them changed."""
    before = _files(base.parent), _files(directory)
    summary = catch_up_db.catch_up(directory, config, base, **kwargs)
    assert (_files(base.parent), _files(directory)) == before
    return summary


def _pg_state(config):
    target = postgresql_database(config)
    try:
        with connection_scope(target), target.atomic():
            return (target.execute_sql(f"SELECT manifest FROM {migrate_db.IMPORT_TABLE}").fetchone()[0],
                    {model._meta.table_name: migrate_db._target_digest(target, model, 100) for model in MODELS})
    finally:
        target.close_all()


def _pg_equals(path, config):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
        expected = {model._meta.table_name: migrate_db._digest(migrate_db._source_rows(source, model)) for model in MODELS}
    return _pg_state(config)[1] == expected


def _unchanged(tables):
    return {table: dict.fromkeys(counts, 0) for table, counts in tables.items()}


def test_catch_up_live_then_final_brings_the_import_up_to_date(sqlite_source, postgres_config, tmp_path, manager_factory):
    base, directory = _catch_up_pair(sqlite_source, postgres_config, tmp_path)
    imported = json.loads(_pg_state(postgres_config)[0])["import_id"]
    live = directory / "tgdata.db"
    with closing(sqlite3.connect(live)) as newer, newer:
        newer.execute("UPDATE msglog SET text = 'edited' WHERE master_msg_id = 'recent.4'")
        _add_message(newer, "new.6", "2021-07-09 00:00:00", "NUL \x00 text")
        newer.execute("INSERT INTO msglogmember (master_msg_id, slave_origin_uid, slave_message_id) "
                      "VALUES ('new.6', 'slave.chat', 'member.6')")
        newer.execute("DELETE FROM chatassoc WHERE id = 41")
        newer.execute("INSERT INTO chatassoc (id, master_uid, slave_uid) VALUES (41, '-100999', 'reused.id')")
        newer.execute("INSERT INTO chatassoc (id, master_uid, slave_uid) VALUES (42, '-100998', 'inserted')")
        newer.execute("DELETE FROM topicassoc")

    summary = _catch_up(directory, postgres_config, base)
    assert summary["import_id"] == imported
    assert summary["window_start_rowid"] == 3
    assert summary["tables"] == {
        "msglog": {"inserted": 1, "updated": 1}, "msglogmember": {"inserted": 1},
        "chatassoc": {"deleted": 1, "replaced": 2}, "topicassoc": {"deleted": 1, "replaced": 0},
        "slavechatinfo": {"deleted": 0, "replaced": 0}, "historymigrationentry": {"deleted": 0, "replaced": 0},
        "historymigrationtarget": {"deleted": 0, "replaced": 0},
    }
    assert _pg_equals(live, postgres_config)
    state = _pg_state(postgres_config)
    assert _catch_up(directory, postgres_config, base)["tables"] == _unchanged(summary["tables"])
    assert _pg_state(postgres_config) == state

    # Edited back after the live run: only the record of the earlier update finds it.
    with closing(sqlite3.connect(live)) as newer, newer:
        newer.execute("UPDATE msglog SET text = 'text' WHERE master_msg_id = 'recent.4'")
    summary = _catch_up(directory, postgres_config, base, final=True)
    assert summary["tables"] == dict(_unchanged(summary["tables"]), msglog={"inserted": 0, "updated": 1})
    assert summary["import_id"] != imported
    assert _pg_equals(live, postgres_config)
    state = _pg_state(postgres_config)
    rerun = _catch_up(directory, postgres_config, base, final=True)
    assert rerun["tables"] == _unchanged(summary["tables"]) and rerun["import_id"] == summary["import_id"]
    assert _pg_state(postgres_config) == state
    record = json.loads(state[0])
    assert record["queue"] is None and {value["sha256"] for value in record["tables"].values()} == {""}

    target = postgresql_database(postgres_config)
    with connection_scope(target):
        migrate_db.validate_runtime_cutover(directory, target)
    target.close_all()
    migrate_db.migrate(directory, postgres_config, queue_only=True)
    manager = manager_factory(directory, postgres_config)
    assert manager.get_msg_log(master_msg_id="new.6").text == "NUL � text"
    assert ChatAssoc.select().where(ChatAssoc.id == 41).get().slave_uid == "reused.id"


def test_catch_up_live_run_reads_one_snapshot_of_a_file_in_use(sqlite_source, postgres_config, tmp_path):
    base, directory = _catch_up_pair(sqlite_source, postgres_config, tmp_path)
    live = directory / "tgdata.db"
    writer = sqlite3.connect(live, isolation_level=None)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        _add_message(writer, "committed.6", "2021-07-09 00:00:00")
        writer.execute("BEGIN")
        _add_message(writer, "uncommitted.7", "2021-07-09 00:00:01")
        wal = live.with_name("tgdata.db-wal")
        before = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (live, wal)]
        assert wal.stat().st_size

        assert catch_up_db.catch_up(directory, postgres_config, base)["tables"]["msglog"] == {"inserted": 1, "updated": 0}
        assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in (live, wal)] == before
    finally:
        writer.rollback()
        writer.close()
    assert _pg_equals(live, postgres_config)


def test_catch_up_retries_a_run_refused_after_its_first_write(sqlite_source, postgres_config, tmp_path):
    base, directory = _catch_up_pair(sqlite_source, postgres_config, tmp_path)
    live = directory / "tgdata.db"
    catch_up_db.catch_up(directory, postgres_config, base)
    with closing(sqlite3.connect(live)) as newer, newer:
        _add_message(newer, "new.6", "2021-07-09 00:00:00")
    receipt = directory / migrate_db.RECEIPT_FILE
    receipt.write_text("{}")
    before = _pg_state(postgres_config)
    with pytest.raises(RuntimeError, match="cutover receipt for an earlier state"):
        catch_up_db.catch_up(directory, postgres_config, base, final=True)
    assert _pg_state(postgres_config)[1] == before[1]
    receipt.unlink()
    assert catch_up_db.catch_up(directory, postgres_config, base, final=True)["tables"]["msglog"] == {
        "inserted": 1, "updated": 0}
    assert _pg_equals(live, postgres_config)


@pytest.mark.parametrize(("change", "error"), [
    ("wal", "-wal is not empty"),
    ("deleted_old_row", "Rows were deleted or renumbered"),
    ("deleted_newest_row", "Rows were deleted or renumbered"),
    ("target_row", "row counts differ from the import record"),
    ("target_column", "columns of chatassoc differ from the import record"),
    ("target_write_after_live_run", "PostgreSQL was written since the last catch-up"),
])
def test_catch_up_refuses_before_writing(sqlite_source, postgres_config, tmp_path, change, error):
    base, directory = _catch_up_pair(sqlite_source, postgres_config, tmp_path)
    live = directory / "tgdata.db"
    writer = sqlite3.connect(live, isolation_level=None)
    target = postgresql_database(postgres_config)
    try:
        if change == "wal":
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            _add_message(writer, "new.6", "2021-07-09 00:00:00")
        elif change == "deleted_old_row":
            writer.execute("DELETE FROM msglog WHERE master_msg_id = 'z.1'")
        elif change == "deleted_newest_row":
            writer.execute("DELETE FROM msglog WHERE rowid = (SELECT max(rowid) FROM msglog)")
        elif change == "target_write_after_live_run":
            catch_up_db.catch_up(directory, postgres_config, base)
        with connection_scope(target):
            if change == "target_row":
                target.execute_sql("INSERT INTO msglogmember VALUES ('out.of', 'band', 'row')")
            elif change == "target_column":
                target.execute_sql("ALTER TABLE chatassoc ADD COLUMN extra TEXT")
            elif change == "target_write_after_live_run":
                target.execute_sql("UPDATE chatassoc SET slave_uid = slave_uid")
                target.execute_sql("SELECT pg_stat_force_next_flush()")
        before = _pg_state(postgres_config)
        with pytest.raises(RuntimeError, match=error):
            catch_up_db.catch_up(directory, postgres_config, base, final=True)
        assert _pg_state(postgres_config) == before
        assert not (directory / migrate_db.RECEIPT_FILE).exists()
    finally:
        target.close_all()
        writer.close()
