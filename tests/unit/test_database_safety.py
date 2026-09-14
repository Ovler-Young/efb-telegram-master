"""Offline database acceptance tests: real PostgreSQL, no Telegram credentials."""

import json
import os
import pickle
import sqlite3
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from peewee import SqliteDatabase

from efb_telegram_master import db as db_module
from efb_telegram_master import migrate_db
from efb_telegram_master.db import (
    ChatAssoc, DatabaseManager, HistoryMigrationEntry, MsgLog, SlaveChatInfo, TopicAssoc, database,
)
from efb_telegram_master.db_runtime import DataDirectoryLock, connection_scope, current_schema, postgresql_database
from efb_telegram_master.outbound import OutboundQueue

MODELS = migrate_db.MODELS


@pytest.fixture(scope="session")
def postgres_server_config():
    pytest.importorskip("psycopg2")
    from psycopg2.extensions import parse_dsn

    if os.getenv("TEST_POSTGRES_HOST"):
        yield {
            "type": "postgresql", "host": os.environ["TEST_POSTGRES_HOST"],
            "port": int(os.getenv("TEST_POSTGRES_PORT", "5432")),
            "database": os.getenv("TEST_POSTGRES_DB", "postgres"),
            "user": os.getenv("TEST_POSTGRES_USER", "postgres"),
            "password": os.getenv("TEST_POSTGRES_PASSWORD", ""),
        }
        return
    pgserver = pytest.importorskip("pgserver", reason="Set TEST_POSTGRES_HOST or install pgserver for real PostgreSQL acceptance")
    pg = pgserver.get_server(Path(tempfile.mkdtemp(prefix="etm-postgres-test-")))
    try:
        parameters = parse_dsn(pg.get_uri())
        yield {
            "type": "postgresql", "host": parameters["host"],
            "port": int(parameters.get("port", "5432")),
            "database": parameters["dbname"], "user": parameters["user"],
            "password": parameters.get("password", ""),
        }
    finally:
        pg.cleanup()


@pytest.fixture
def postgres_config(postgres_server_config):
    schema = "etm_test_" + uuid.uuid4().hex
    admin = postgresql_database(postgres_server_config)
    with connection_scope(admin):
        admin.execute_sql(f'CREATE SCHEMA "{schema}"')
    try:
        yield dict(postgres_server_config, options=f"-c search_path={schema} -c timezone=UTC")
    finally:
        with connection_scope(admin):
            admin.execute_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close_all()


@pytest.fixture
def sqlite_source(tmp_path):
    directory = tmp_path / "channel"
    directory.mkdir()
    source = SqliteDatabase(str(directory / "tgdata.db"), pragmas={"journal_mode": "wal"})
    with source.bind_ctx(MODELS), source.connection_context():
        source.create_tables(MODELS)
        ChatAssoc.create(id=41, master_uid="-100123456789", slave_uid="slave.chat")
        TopicAssoc.create(id=52, topic_chat_id="-100123456789", message_thread_id="9001", slave_uid="slave.chat")
        SlaveChatInfo.create(
            id=63, slave_channel_id="slave", slave_channel_emoji="🧪", slave_chat_uid="chat",
            slave_chat_name="name", slave_chat_type="GroupChat", pickle=pickle.dumps({"old": True}),
        )
        for key, timestamp in (("z.1", datetime(2020, 1, 2, 3, 4, 5, 123456)), ("ä.2", None), ("中.3", datetime(2021, 7, 8))):
            MsgLog.create(
                master_msg_id=key, master_msg_id_alt="alternate." + key, slave_message_id="slave." + key,
                text="Unicode 中文 🐦\n" + key, slave_origin_uid="slave.chat", slave_member_uid="slave.author",
                msg_type="Text", media_type="Text", sent_to="blueset.telegram", sender_bot_id="1234567890123",
                pickle=pickle.dumps({"target": "z.1"}), time=timestamp,
            )
        HistoryMigrationEntry.create(
            id=74, slave_chat_id="slave.chat", target_chat_id="-100123456789", message_thread_id="9001",
            source_master_msg_id="z.1", formatted_text="historic", source_time=datetime(2020, 1, 2), position=0,
        )
    queue = OutboundQueue(directory)
    with queue.connection:
        queue.connection.executemany(
            "INSERT INTO outbound_queue(priority,telegram_chat_id,operation,payload,created_at,"
            "log_context,delivery_state,completion_receipt,reconcile_after,reconcile_attempts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(0, -100123456789, "send_message", b"payload", 1000.0, b"context", state, receipt, deadline, attempts)
             for state, receipt, deadline, attempts in (("queued", None, 0, 0), ("sent_pending", b"receipt", 9999, 7))],
        )
    queue.close()
    return directory


def source_rows(directory):
    with closing(sqlite3.connect(directory / "tgdata.db")) as source:
        return {model._meta.table_name: list(migrate_db._source_rows(source, model)) for model in MODELS}


@pytest.fixture
def manager_factory(monkeypatch):
    previous = database.obj
    managers = []

    def create(directory, config=None):
        monkeypatch.setattr(db_module.utils, "get_data_path", lambda _: directory)
        manager = DatabaseManager(SimpleNamespace(channel_id="test.database", config={"database": config or {}}))
        managers.append(manager)
        return manager

    yield create
    for manager in reversed(managers):
        manager.stop_worker()
    database.initialize(previous)


def test_real_postgresql_import_preserves_every_field_id_queue_and_next_sequence(sqlite_source, postgres_config, manager_factory):
    before = source_rows(sqlite_source)
    queue_before = migrate_db._queue_digest(sqlite_source / "outbound-queue.sqlite3")
    older_archive = sqlite_source / "tgdata.db.migrated"
    older_archive.write_bytes(b"do not overwrite")
    receipt = migrate_db.migrate(sqlite_source, postgres_config, batch_size=2)
    assert source_rows(sqlite_source) == before
    assert older_archive.read_bytes() == b"do not overwrite"
    assert migrate_db._queue_digest(sqlite_source / "outbound-queue.sqlite3") == queue_before
    archive = Path(receipt["backup_directory"])
    assert source_rows(archive) == before
    assert migrate_db._queue_digest(archive / "outbound-queue.sqlite3") == queue_before
    with pytest.raises(RuntimeError, match="stale SQLite"):
        migrate_db.validate_runtime_cutover(sqlite_source, None)
    manager = manager_factory(sqlite_source, postgres_config)
    target = manager._managed_database
    assert target.is_closed()
    with connection_scope(target), target.atomic():
        for model in MODELS:
            assert migrate_db._target_digest(target, model, 1) == receipt["tables"][model._meta.table_name]
        assert ChatAssoc.create(master_uid="next", slave_uid="next").id == 42
        assert TopicAssoc.create(topic_chat_id="next", message_thread_id="next", slave_uid="next").id == 53
        assert SlaveChatInfo.create(slave_channel_id="next", slave_channel_emoji="", slave_chat_uid="next", slave_chat_name="next", slave_chat_type="PrivateChat").id == 64
        assert HistoryMigrationEntry.create(slave_chat_id="next", target_chat_id="next", source_master_msg_id="next", position=1).id == 75
    assert manager.get_msg_log(master_msg_id="ä.2").time is None
    assert manager.get_msg_log(master_msg_id="z.1").time == datetime(2020, 1, 2, 3, 4, 5, 123456)


def test_source_wal_backup_is_independently_readable(sqlite_source, postgres_config):
    path = sqlite_source / "tgdata.db"
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE msglog SET text = ? WHERE master_msg_id = ?", ("newest WAL data", "z.1"))
        writer.commit()
        assert path.with_name("tgdata.db-wal").stat().st_size > 0
        receipt = migrate_db.migrate(sqlite_source, postgres_config)
        with closing(sqlite3.connect(Path(receipt["backup_directory"]) / "tgdata.db")) as archive:
            assert archive.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert archive.execute("SELECT text FROM msglog WHERE master_msg_id = 'z.1'").fetchone() == ("newest WAL data",)
        assert writer.execute("SELECT COUNT(*) FROM msglog").fetchone() == (3,)
        assert path.exists()


@pytest.mark.parametrize("preexisting", [False, True])
def test_runtime_never_implicitly_imports_sqlite(sqlite_source, postgres_config, manager_factory, preexisting):
    target = postgresql_database(postgres_config)
    if preexisting:
        with connection_scope(target), target.bind_ctx([ChatAssoc]):
            target.create_tables([ChatAssoc])
    with pytest.raises(RuntimeError, match="explicit offline import"):
        manager_factory(sqlite_source, postgres_config)
    with connection_scope(target):
        assert target.get_tables(schema=current_schema(target)) == (["chatassoc"] if preexisting else [])
    target.close_all()
    with DataDirectoryLock(sqlite_source):
        pass  # Failed startup must release ownership.


def test_nonempty_target_rejected_even_when_chatassoc_is_empty(sqlite_source, postgres_config):
    target = postgresql_database(postgres_config)
    with connection_scope(target), target.bind_ctx([ChatAssoc]):
        target.create_tables([ChatAssoc])
    with pytest.raises(RuntimeError, match="not empty"):
        migrate_db.migrate(sqlite_source, postgres_config)
    with connection_scope(target):
        assert target.get_tables(schema=current_schema(target)) == ["chatassoc"]
    target.close_all()
    assert not (sqlite_source / migrate_db.RECEIPT_FILE).exists()


@pytest.mark.parametrize("failure", ["insert", "verify", "backup"])
def test_precommit_failure_rolls_back_and_keeps_sources(sqlite_source, postgres_config, monkeypatch, failure):
    before = source_rows(sqlite_source)
    original = migrate_db._import_rows

    def fail_insert(db, source, batch_size):
        original(db, source, batch_size)
        raise RuntimeError("injected insert failure")

    def fail_backup(*args):
        raise OSError("injected backup failure")

    if failure == "insert":
        monkeypatch.setattr(migrate_db, "_import_rows", fail_insert)
    elif failure == "verify":
        monkeypatch.setattr(migrate_db, "_target_digest", lambda *args: {"bad": True})
    else:
        monkeypatch.setattr(migrate_db, "_backup", fail_backup)
    with pytest.raises((RuntimeError, OSError)):
        migrate_db.migrate(sqlite_source, postgres_config, batch_size=1)
    assert source_rows(sqlite_source) == before
    assert not (sqlite_source / migrate_db.RECEIPT_FILE).exists()
    target = postgresql_database(postgres_config)
    with connection_scope(target):
        assert target.get_tables(schema=current_schema(target)) == []
    target.close_all()


def test_postcommit_receipt_failure_recovers_without_duplicate_rows(sqlite_source, postgres_config, monkeypatch):
    with patch.object(migrate_db, "_write_receipt", side_effect=OSError("injected finalization failure")):
        with pytest.raises(OSError):
            migrate_db.migrate(sqlite_source, postgres_config)
    target = postgresql_database(postgres_config)
    with connection_scope(target):
        committed = migrate_db._read_import(target)
        with pytest.raises(RuntimeError, match="explicit offline import"):
            migrate_db.validate_runtime_cutover(sqlite_source, target)
    target.close_all()
    receipt = migrate_db.migrate(sqlite_source, postgres_config)
    assert receipt["import_id"] == committed["import_id"]
    assert receipt["tables"]["msglog"]["rows"] == 3
    second = migrate_db.migrate(sqlite_source, postgres_config)
    assert second["import_id"] == receipt["import_id"]
    assert len(list((sqlite_source / "database-backups").iterdir())) == 3


@pytest.mark.parametrize("point", ["before_commit", "after_commit"])
def test_process_death_recovery(sqlite_source, postgres_config, point):
    # os._exit deliberately bypasses finally blocks: exercises actual connection
    # loss/transaction rollback rather than only Python exception handling.
    hook = "_target_digest" if point == "before_commit" else "_write_receipt"
    program = (
        "import os,json,sys; from pathlib import Path; from efb_telegram_master import migrate_db as m; "
        f"m.{hook}=lambda *a,**k: os._exit(97); "
        "m.migrate(Path(sys.argv[1]),json.loads(sys.argv[2]),1)"
    )
    result = subprocess.run([sys.executable, "-c", program, str(sqlite_source), json.dumps(postgres_config)], capture_output=True, timeout=30)
    assert result.returncode == 97, result.stderr.decode()
    assert not (sqlite_source / migrate_db.RECEIPT_FILE).exists()
    assert len(source_rows(sqlite_source)["msglog"]) == 3
    receipt = migrate_db.migrate(sqlite_source, postgres_config, batch_size=1)
    assert receipt["tables"]["msglog"]["rows"] == 3


@pytest.mark.parametrize("side", ["source", "target", "queue"])
def test_resume_rejects_divergence(sqlite_source, postgres_config, side):
    migrate_db.migrate(sqlite_source, postgres_config)
    if side == "target":
        target = postgresql_database(postgres_config)
        with connection_scope(target):
            target.execute_sql("UPDATE msglog SET text = 'changed' WHERE master_msg_id = 'z.1'")
        target.close_all()
    else:
        path = sqlite_source / ("tgdata.db" if side == "source" else "outbound-queue.sqlite3")
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("UPDATE msglog SET text = 'changed'" if side == "source" else "UPDATE outbound_queue SET reconcile_after = 1")
            connection.commit()
    with pytest.raises(RuntimeError, match="changed|verification failed"):
        migrate_db.migrate(sqlite_source, postgres_config)


def test_runtime_rejects_source_reuse_and_wrong_receipt(sqlite_source, postgres_config):
    receipt = migrate_db.migrate(sqlite_source, postgres_config)
    target = postgresql_database(postgres_config)
    with connection_scope(target):
        migrate_db.validate_runtime_cutover(sqlite_source, target)
        path = sqlite_source / migrate_db.RECEIPT_FILE
        path.write_text(json.dumps(dict(receipt, import_id="wrong")))
        with pytest.raises(RuntimeError, match="different PostgreSQL import"):
            migrate_db.validate_runtime_cutover(sqlite_source, target)
        path.write_text(json.dumps(receipt))
        with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source:
            source.execute("UPDATE msglog SET text = 'new message after cutover'")
            source.commit()
        with pytest.raises(RuntimeError, match="changed after import"):
            migrate_db.validate_runtime_cutover(sqlite_source, target)
    target.close_all()


@pytest.mark.parametrize("kind", ["table", "column", "nul", "bad_timestamp"])
def test_unsupported_source_data_fails_without_loss(sqlite_source, postgres_config, kind):
    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source:
        if kind == "table":
            source.execute("CREATE TABLE legacy_extra(id INTEGER PRIMARY KEY, value TEXT)")
            source.execute("INSERT INTO legacy_extra VALUES(1, 'must not lose')")
        elif kind == "column":
            source.execute("ALTER TABLE msglog ADD COLUMN important_extra TEXT")
        elif kind == "nul":
            source.execute("UPDATE msglog SET text = ?", ("embedded\0NUL",))
        else:
            source.execute("UPDATE msglog SET time = 'not a timestamp'")
        source.commit()
    with pytest.raises((RuntimeError, ValueError)):
        migrate_db.migrate(sqlite_source, postgres_config)
    assert (sqlite_source / "tgdata.db").exists()
    assert not (sqlite_source / migrate_db.RECEIPT_FILE).exists()


def test_historic_nullable_columns_and_missing_optional_tables(sqlite_source, postgres_config):
    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source:
        for column in ("sender_bot_id", "file_unique_id", "master_msg_id_alt", "pickle", "time"):
            source.execute(f"ALTER TABLE msglog DROP COLUMN {column}")
        source.execute("DROP TABLE topicassoc")
        source.execute("DROP TABLE historymigrationentry")
        source.commit()
    receipt = migrate_db.migrate(sqlite_source, postgres_config)
    assert receipt["tables"]["msglog"]["rows"] == 3
    assert receipt["tables"]["topicassoc"]["rows"] == 0
    target = postgresql_database(postgres_config)
    with connection_scope(target), target.bind_ctx(MODELS):
        row = MsgLog.get_by_id("z.1")
        assert row.time is None and row.sender_bot_id is None and row.pickle is None
    target.close_all()


def test_source_write_fence_and_runtime_ownership(sqlite_source, postgres_config, manager_factory, monkeypatch):
    manager = manager_factory(sqlite_source)
    with pytest.raises(RuntimeError, match="in use"):
        migrate_db.migrate(sqlite_source, postgres_config)
    manager.stop_worker()
    original = migrate_db._import_rows

    def inspect_fences(db, source, batch_size):
        for filename in ("tgdata.db", "outbound-queue.sqlite3"):
            with closing(sqlite3.connect(sqlite_source / filename, timeout=0)) as other:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
        return original(db, source, batch_size)

    monkeypatch.setattr(migrate_db, "_import_rows", inspect_fences)
    migrate_db.migrate(sqlite_source, postgres_config)


def test_postgres_connections_return_after_short_lived_and_nested_calls(tmp_path, postgres_config, manager_factory):
    manager = manager_factory(tmp_path, dict(postgres_config, max_connections=2))
    target = manager._managed_database
    outcomes = []

    def query():
        try:
            manager.get_chat_assoc(master_uid="none")
            outcomes.append("ok")
        except Exception as error:
            outcomes.append(type(error).__name__)

    for _ in range(24):
        thread = threading.Thread(target=query)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert outcomes == ["ok"] * 24
    assert len(target._in_use) == 0
    with connection_scope(target):
        connection = target.connection()
        manager.get_chat_assoc(master_uid="none")
        assert target.connection() is connection
        assert not target.is_closed()
    with pytest.raises(ValueError):
        manager.get_msg_log()
    assert target.is_closed()
    assert len(target._in_use) == 0


def test_large_import_keeps_python_memory_bounded(sqlite_source, postgres_config):
    import tracemalloc

    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source:
        source.executemany(
            "INSERT INTO msglog(master_msg_id,slave_message_id,text,slave_origin_uid,msg_type,sent_to) VALUES(?,?,?,?,?,?)",
            ((f"large.{i:06d}", str(i), "x" * 6144, "large.chat", "Text", "test") for i in range(6000)),
        )
        source.commit()
    tracemalloc.start()
    try:
        receipt = migrate_db.migrate(sqlite_source, postgres_config, batch_size=32)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert receipt["tables"]["msglog"]["rows"] == 6003
    # 36 MiB of source text alone, before dict/list/JSON overhead, cannot fit
    # below this bound if either side is eagerly loaded in full.
    assert peak < 12 * 1024 * 1024


def test_cli_round_trip_without_constructing_a_telegram_bot(sqlite_source, postgres_config, tmp_path):
    config_path = tmp_path / "postgresql.yaml"
    config_path.write_text(json.dumps({"database": postgres_config}))
    result = subprocess.run(
        [sys.executable, "-m", "efb_telegram_master.migrate_db", "--data-dir", str(sqlite_source),
         "--config", str(config_path), "--batch-size", "2"],
        capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    report = json.loads(result.stdout)
    assert report["tables"]["msglog"]["rows"] == 3
    assert "password" not in result.stdout.decode()
    assert (sqlite_source / migrate_db.RECEIPT_FILE).exists()


def test_migrated_runtime_new_reply_edit_reaction_and_backfill(sqlite_source, postgres_config, manager_factory):
    migrate_db.migrate(sqlite_source, postgres_config)
    manager = manager_factory(sqlite_source, postgres_config)
    chat = SimpleNamespace(module_id="slave", uid="chat")
    author = SimpleNamespace(module_id="slave", uid="author")
    message = SimpleNamespace(
        uid="new-source", text="new message", chat=chat, author=author,
        type=SimpleNamespace(name="Text"), type_telegram=SimpleNamespace(value="Text"),
        deliver_to=SimpleNamespace(channel_id="blueset.telegram"),
        file_id=None, file_unique_id=None, mime=None, is_system=False,
        attributes=None, commands=None, substitutions=None, reactions=None, target=None,
    )
    master = SimpleNamespace(chat_id=-100123, message_id=100)
    manager.add_or_update_message_log(message, master, sender_bot_id="bot-1")
    reply = SimpleNamespace(**dict(vars(message), uid="reply-source", text="reply", target=message))
    manager.add_or_update_message_log(reply, SimpleNamespace(chat_id=-100123, message_id=101))
    stored_reply = manager.get_msg_log(master_msg_id="-100123.101")
    assert pickle.loads(stored_reply.pickle)["target"] == "-100123.100"
    message.text = "edited"
    message.reactions = {"👍": [author]}
    alternate = SimpleNamespace(chat_id=-100123, message_id=102)
    for _ in range(2):
        manager.add_or_update_message_log(message, alternate, old_message_id=(-100123, 100), sender_bot_id="bot-1")
    stored = manager.get_msg_log(master_msg_id="-100123.100")
    assert stored.master_msg_id_alt == "-100123.102"
    assert stored.sender_bot_id == "bot-1"
    assert pickle.loads(stored.pickle)["reactions"] == {"👍": ("slave author",)}
    assert len(manager.get_recent_messages("slave chat", limit=2)) == 2
    entries = [{
        "slave_chat_id": "slave chat", "target_chat_id": "-100123", "message_thread_id": None,
        "source_master_msg_id": "-100123.100", "formatted_text": "backfill", "position": 0,
    }]
    manager.replace_history_migration_entries("slave chat", -100123, None, entries)
    assert len(manager.get_history_migration_entries("slave chat", -100123)) == 1
    manager.stop_worker()
    restarted = manager_factory(sqlite_source, postgres_config)
    assert restarted.get_msg_log(slave_msg_id="new-source", slave_origin_uid="slave chat").text == "edited"
    pending = restarted.get_history_migration_entries("slave chat", -100123)
    assert len(pending) == 1
    restarted.delete_history_migration_entry(pending[0].id)
    assert restarted.get_history_migration_entries("slave chat", -100123) == []


def test_cutover_receipt_cannot_be_reused_for_another_target(sqlite_source, postgres_config):
    receipt = migrate_db.migrate(sqlite_source, postgres_config)
    with patch.object(migrate_db, "_read_import", return_value=None):
        with pytest.raises(RuntimeError, match="receipt does not match"):
            migrate_db.migrate(sqlite_source, postgres_config)
    assert json.loads((sqlite_source / migrate_db.RECEIPT_FILE).read_text())["import_id"] == receipt["import_id"]


@pytest.mark.parametrize("invalid", [{}, {"version": 999}, []])
def test_corrupt_import_manifest_cannot_authorize_startup(sqlite_source, postgres_config, invalid):
    migrate_db.migrate(sqlite_source, postgres_config)
    target = postgresql_database(postgres_config)
    with connection_scope(target):
        target.execute_sql(f"UPDATE {migrate_db.IMPORT_TABLE} SET manifest = %s", (json.dumps(invalid),))
        with pytest.raises(RuntimeError, match="corrupt PostgreSQL import record"):
            migrate_db.validate_runtime_cutover(sqlite_source, target)
    target.close_all()
    with pytest.raises(RuntimeError, match="corrupt PostgreSQL import record"):
        migrate_db.migrate(sqlite_source, postgres_config)
    assert (sqlite_source / "tgdata.db").exists()
