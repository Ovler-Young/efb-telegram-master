"""Offline database acceptance tests: real PostgreSQL, no Telegram credentials."""

import json
import os
import pickle
import shutil
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
from efb_telegram_master.outbound import OutboundQueue, QueueRequest

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
    (queue.media_dir / "media-fixture.bin").write_bytes(b"durable media fixture")
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


def test_queue_digest_legacy_manifest_accepts_only_empty_sidecar_state():
    legacy = {"sha256": "queue"}
    assert migrate_db._queue_digest_matches({"sha256": "queue", "media": None}, legacy)
    assert migrate_db._queue_digest_matches(
        {"sha256": "queue", "media": {"files": 0, "bytes": 0, "sha256": "empty"}}, legacy
    )
    assert not migrate_db._queue_digest_matches(
        {"sha256": "queue", "media": {"files": 1, "bytes": 1, "sha256": "media"}}, legacy
    )
    assert not migrate_db._queue_digest_matches({"sha256": "changed", "media": None}, legacy)


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


def test_imported_target_cannot_be_treated_as_native_when_local_files_are_missing(sqlite_source, postgres_config, manager_factory):
    migrate_db.migrate(sqlite_source, postgres_config)
    (sqlite_source / "tgdata.db").unlink()
    (sqlite_source / migrate_db.RECEIPT_FILE).unlink()
    with pytest.raises(RuntimeError, match="explicit offline import"):
        manager_factory(sqlite_source, postgres_config)
    assert not (sqlite_source / "tgdata.db").exists()


@pytest.mark.parametrize("table", ["msglog", "historymigrationtarget"])
def test_runtime_does_not_recreate_missing_imported_tables(sqlite_source, postgres_config, manager_factory, table):
    migrate_db.migrate(sqlite_source, postgres_config)
    target = postgresql_database(postgres_config)
    try:
        with connection_scope(target):
            target.execute_sql(f'DROP TABLE "{table}"')
        with pytest.raises(RuntimeError, match="Imported PostgreSQL tables are missing"):
            manager_factory(sqlite_source, postgres_config)
        with connection_scope(target):
            assert table not in target.get_tables(schema=current_schema(target))
    finally:
        target.close_all()


def test_runtime_requires_the_retained_outbound_queue(sqlite_source, postgres_config, manager_factory):
    migrate_db.migrate(sqlite_source, postgres_config)
    queue_path = sqlite_source / "outbound-queue.sqlite3"
    queue_path.unlink()
    with pytest.raises(RuntimeError, match="outbound queue"):
        manager_factory(sqlite_source, postgres_config)
    assert not queue_path.exists()  # Never silently create an empty replacement.


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

    def fail_insert(db, source, batch_size, models=MODELS):
        original(db, source, batch_size, models)
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


@pytest.mark.parametrize("side", ["source", "target", "queue", "queue-media"])
def test_resume_rejects_divergence(sqlite_source, postgres_config, side):
    migrate_db.migrate(sqlite_source, postgres_config)
    if side == "target":
        target = postgresql_database(postgres_config)
        with connection_scope(target):
            target.execute_sql("UPDATE msglog SET text = 'changed' WHERE master_msg_id = 'z.1'")
        target.close_all()
    elif side == "queue-media":
        media_file = next((sqlite_source / "outbound-media").iterdir())
        media_file.write_bytes(b"changed media")
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

    def inspect_fences(db, source, batch_size, models=MODELS):
        for filename in ("tgdata.db", "outbound-queue.sqlite3"):
            with closing(sqlite3.connect(sqlite_source / filename, timeout=0)) as other:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute("BEGIN IMMEDIATE")
        return original(db, source, batch_size, models)

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


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_null_timestamp_order_is_consistent_across_backends(sqlite_source, postgres_config, manager_factory, backend):
    if backend == "postgresql":
        migrate_db.migrate(sqlite_source, postgres_config)
    manager = manager_factory(sqlite_source, postgres_config if backend == "postgresql" else None)
    assert manager.get_last_message("slave.chat").master_msg_id == "中.3"
    assert [row.master_msg_id for row in manager.get_recent_messages("slave.chat", limit=0)] == ["ä.2", "z.1", "中.3"]
    with connection_scope(manager._managed_database):
        for key, origin, source_id, timestamp in (
            ("null-copy", "slave.chat", "slave.z.1", None),
            ("42.1", "unknown-time", "one", None),
            ("42.2", "known-time", "two", datetime(2020, 1, 1)),
        ):
            MsgLog.create(master_msg_id=key, slave_message_id=source_id, text="test", slave_origin_uid=origin,
                          msg_type="Text", sent_to="test", time=timestamp)
    assert manager.get_msg_log(slave_msg_id="slave.z.1", slave_origin_uid="slave.chat").master_msg_id == "z.1"
    assert manager.get_recent_slave_chats(42, limit=1) == ["known-time"]
    expected = [row.master_msg_id for row in manager.get_recent_messages("slave.chat", limit=0)]
    seen = []
    after = None
    for _ in range(len(expected) + 1):
        page = manager.get_recent_messages("slave.chat", limit=1, after=after)
        if not page:
            break
        seen.append(page[0].master_msg_id)
        after = page[0].time, page[0].master_msg_id
    assert seen == expected


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
@pytest.mark.parametrize("legacy", [False, True])
def test_real_log_reconciliation_preserves_message_fields_without_media_io(
    sqlite_source, postgres_config, manager_factory, backend, legacy,
):
    import logging
    from telegram import Chat, Message as TelegramMessage
    from ehforwarderbot.message import Substitutions
    from efb_telegram_master.bot_manager import QueuedDbLogContext, TelegramBotManager
    from efb_telegram_master.message import ETMMsg
    from efb_telegram_master.outbound import SenderSelection
    from tests.unit.test_restart_memory import message_with_group

    if backend == "postgresql":
        migrate_db.migrate(sqlite_source, postgres_config)
    manager = manager_factory(sqlite_source, postgres_config if backend == "postgresql" else None)
    target = message_with_group()
    target.uid = "target-message"
    manager.add_or_update_message_log(target, SimpleNamespace(chat_id=123, message_id=100))
    message = message_with_group(32768)
    message.target = target
    message.file_id = "metadata-only-no-download"
    message.is_system = True
    message.substitutions = Substitutions({(0, 7): message.author})
    message.reactions = {"👍": tuple(message.chat.members)}
    manager.add_or_update_message_log(message, SimpleNamespace(chat_id=123, message_id=101), sender_bot_id="aux-123")
    original = manager.get_msg_log(master_msg_id="123.101")
    context = (b"\x01" + pickle.dumps((message, None), protocol=5) if legacy else
               TelegramBotManager._encode_queued_log_context(QueuedDbLogContext(message, None)))
    receipt = TelegramMessage(message_id=102, date=datetime(2020, 1, 1), chat=Chat(123, "private"), text=message.text)
    row = SimpleNamespace(
        id=1, log_context=context,
        completion_receipt=TelegramBotManager.encode_queued_completion_receipt(receipt, SenderSelection(None, "aux-123")),
    )
    adapter = object.__new__(TelegramBotManager)
    adapter.channel = SimpleNamespace(db=manager)
    adapter.logger = logging.getLogger("tests.real-reconciliation")
    adapter._queued_completion_callbacks = {}
    adapter._queued_db_log_context_lock = threading.Lock()
    with patch.object(ETMMsg, "_load_file", side_effect=AssertionError("unexpected media I/O")):
        assert adapter.reconcile_queued_delivery(row)
        assert adapter.reconcile_queued_delivery(row)  # Repeated receipt is still idempotent.
    recovered = manager.get_msg_log(master_msg_id="123.102")
    for field in MsgLog._meta.sorted_fields:
        if field.name not in ("master_msg_id", "time"):
            assert getattr(recovered, field.name) == getattr(original, field.name), field.name


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_durable_reaction_reply_updates_the_canonical_mapping(
    sqlite_source, postgres_config, manager_factory, backend,
):
    import logging
    from efb_telegram_master.slave_message import SlaveMessageProcessor
    from tests.unit.test_restart_memory import message_with_group

    if backend == "postgresql":
        migrate_db.migrate(sqlite_source, postgres_config)
    manager = manager_factory(sqlite_source, postgres_config if backend == "postgresql" else None)
    message = message_with_group()
    manager.add_or_update_message_log(message, SimpleNamespace(chat_id=123, message_id=101))
    processor = object.__new__(SlaveMessageProcessor)
    processor.logger = logging.getLogger("tests.canonical-reaction")
    processor.chat_manager = SimpleNamespace(update_chat_obj=lambda chat: chat)
    destinations = []

    def send(msg, destination, thread_id, template, reactions, edit_id, target_id, markup, silent, *, on_db_complete):
        context = processor._make_send_kwargs(msg, edit_id, on_complete=on_db_complete)["_queued_db_log_context"]
        destinations.append(context.old_msg_id)
        manager.add_or_update_message_log(
            context.etm_msg, SimpleNamespace(chat_id=123, message_id=102), context.old_msg_id,
        )
        return SimpleNamespace(durable_db_logged=True)

    processor.slave_message_text = send
    processor.dispatch_message(
        message, "", None, 123, None,
        database_old_msg_id=(123, 101), target_msg_id_override=101,
    )
    assert destinations == [(123, 101)]
    canonical = manager.get_msg_log(master_msg_id="123.101")
    assert canonical.master_msg_id_alt == "123.102"
    assert manager.get_msg_log(master_msg_id="123.102") is None
    assert processor._make_send_kwargs(message, None, on_complete=None)["_queued_db_log_context"].old_msg_id is None


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


def test_import_preserves_production_message_column_and_discovered_caches(sqlite_source, postgres_config):
    # Cache shapes are discovered, not asserted as production DDL: that DDL is
    # available only in the source database at migration time.
    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, source:
        source.execute("UPDATE msglog SET master_message_thread_id = '9001' WHERE master_msg_id = 'z.1'")
        source.execute("CREATE TABLE topiciconcache (chat_id INTEGER PRIMARY KEY, icon TEXT NOT NULL, data BLOB)")
        source.execute("CREATE TABLE useremojicache (user_id TEXT NOT NULL, emoji_id TEXT NOT NULL, "
                       "label TEXT, PRIMARY KEY (user_id, emoji_id))")
        source.executemany("INSERT INTO topiciconcache VALUES (?, ?, ?)",
                           [(2**40 + i, f"icon-{i}", b"\x00\xff" if i % 2 else None) for i in range(4107)])
        source.executemany("INSERT INTO useremojicache VALUES (?, ?, ?)",
                           [(f"user-{i}", "emoji", "中文" if i % 2 else None) for i in range(1074)])
    receipt = migrate_db.migrate(sqlite_source, postgres_config, batch_size=128)
    assert receipt["tables"]["topiciconcache"]["rows"] == 4107
    assert receipt["tables"]["useremojicache"]["rows"] == 1074
    target = postgresql_database(postgres_config)
    with connection_scope(target):
        assert target.execute_sql("SELECT master_message_thread_id FROM msglog WHERE master_msg_id = 'z.1'").fetchone() == ("9001",)
        assert target.execute_sql("SELECT chat_id, icon, data FROM topiciconcache ORDER BY chat_id DESC LIMIT 1").fetchone()[:2] == (2**40 + 4106, "icon-4106")
        migrate_db.validate_runtime_cutover(sqlite_source, target)
    target.close_all()
    assert migrate_db.migrate(sqlite_source, postgres_config)["import_id"] == receipt["import_id"]


def test_committed_v1_manifest_recovers_with_original_column_projection(sqlite_source, postgres_config, tmp_path):
    external = tmp_path / "legacy-upload.bin"
    external.write_bytes(b"legacy external bytes")
    external_queue_payload(sqlite_source, external)
    receipt = migrate_db.migrate(sqlite_source, postgres_config)
    target = postgresql_database(postgres_config)
    legacy = {key: value for key, value in receipt.items() if key not in ("columns", "backup_queue", "source_fingerprint")}
    legacy["version"] = 1
    legacy["queue"] = {key: value for key, value in legacy["queue"].items() if key != "external"}
    # The exact 1720ad5 MsgLog projection, including sender_bot_id before time.
    columns = [
        "master_msg_id", "master_msg_id_alt", "slave_message_id", "text", "slave_origin_uid",
        "slave_origin_display_name", "slave_member_uid", "slave_member_display_name", "media_type",
        "mime", "file_id", "file_unique_id", "msg_type", "pickle", "sent_to", "sender_bot_id", "time",
    ]
    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, source:
        legacy["tables"]["msglog"] = migrate_db._digest(migrate_db._source_rows(source, MsgLog, columns))
        legacy["tables"].pop("historymigrationtarget")
        legacy["tables"]["historymigrationentry"] = migrate_db._digest(migrate_db._source_rows(
            source, HistoryMigrationEntry, migrate_db.V1_COLUMNS["historymigrationentry"],
        ))
        source.execute("CREATE TABLE topiciconcache (id INTEGER PRIMARY KEY, icon TEXT)")
        source.execute("CREATE TABLE useremojicache (user_id TEXT, emoji TEXT)")
    with connection_scope(target):
        target.execute_sql("ALTER TABLE msglog DROP COLUMN master_message_thread_id")
        target.execute_sql("ALTER TABLE historymigrationentry DROP COLUMN generation")
        target.execute_sql("DROP TABLE historymigrationtarget")
        target.execute_sql(f"UPDATE {migrate_db.IMPORT_TABLE} SET manifest = %s", (json.dumps(legacy),))
    target.close_all()
    (sqlite_source / migrate_db.RECEIPT_FILE).unlink()
    recovered = migrate_db.migrate(sqlite_source, postgres_config)
    assert recovered["import_id"] == receipt["import_id"]
    assert recovered["version"] == 1
    assert recovered["tables"] == legacy["tables"]
    assert "external" in recovered["queue"]
    assert migrate_db.migrate(sqlite_source, postgres_config)["queue"] == recovered["queue"]
    external.write_bytes(b"changed external bytes")
    with pytest.raises(RuntimeError, match="Source changed"):
        migrate_db.migrate(sqlite_source, postgres_config)
    external.write_bytes(b"legacy external bytes")
    with connection_scope(target):
        migrate_db.validate_runtime_cutover(sqlite_source, target)
        assert not (migrate_db.CACHE_TABLES & set(target.get_tables(schema=current_schema(target))))
    target.close_all()
    for table in sorted(migrate_db.CACHE_TABLES):
        with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, source:
            assert source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (0,)
            source.execute(f'INSERT INTO "{table}" VALUES (?, ?)', (1, "new data"))
        with pytest.raises(RuntimeError, match="Unrecorded source cache.*no longer empty"):
            migrate_db.migrate(sqlite_source, postgres_config)
        with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, source:
            assert source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (1,)
            source.execute(f'DELETE FROM "{table}"')
    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, source:
        source.execute("UPDATE msglog SET master_message_thread_id = 'new'")
    with pytest.raises(RuntimeError, match="unrecorded data in msglog.master_message_thread_id"):
        migrate_db.migrate(sqlite_source, postgres_config)


def external_queue_payload(directory, external):
    from efb_telegram_master.outbound import _StoredMediaSnapshot
    from telegram import InputMediaDocument

    reference = _StoredMediaSnapshot(None, external.name, external_uri=external.as_uri(), cleanup_external=True)
    payload = OutboundQueue.encode_payload((), {"media": [InputMediaDocument(reference)]})
    with closing(sqlite3.connect(directory / "outbound-queue.sqlite3")) as queue, queue:
        queue.execute("UPDATE outbound_queue SET operation = 'send_media_group', payload = ? WHERE id = 1", (payload,))
    return payload


def test_external_queue_backup_restores_elsewhere_without_original(sqlite_source, tmp_path):
    import tracemalloc

    external = tmp_path / "source upload.bin"
    chunk = b"x" * (1024 * 1024)
    with external.open("wb") as writer:
        for _ in range(24):
            writer.write(chunk)
    original_payload = external_queue_payload(sqlite_source, external)
    (sqlite_source / "outbound-media" / "external-1").write_bytes(b"existing sidecar")
    before = migrate_db._queue_digest(sqlite_source / "outbound-queue.sqlite3")
    archive = tmp_path / "backup"
    archive.mkdir()
    tracemalloc.start()
    try:
        assert migrate_db._backup_queue(sqlite_source / "outbound-queue.sqlite3", archive) == before
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024
    backup_digest = migrate_db._queue_digest(archive / "outbound-queue.sqlite3")
    second_archive = tmp_path / "backup-again"
    second_archive.mkdir()
    assert migrate_db._backup_queue(sqlite_source / "outbound-queue.sqlite3", second_archive) == before
    assert migrate_db._queue_digest(second_archive / "outbound-queue.sqlite3") == backup_digest
    assert (second_archive / "outbound-media" / "external-1").read_bytes() == b"existing sidecar"
    assert migrate_db._queue_digest(sqlite_source / "outbound-queue.sqlite3") == before
    with closing(sqlite3.connect(sqlite_source / "outbound-queue.sqlite3")) as queue:
        assert queue.execute("SELECT payload FROM outbound_queue WHERE id = 1").fetchone()[0] == original_payload
    assert external.stat().st_size == 24 * len(chunk)
    restored = tmp_path / "restored"
    shutil.copytree(archive, restored)
    external.unlink()
    shutil.rmtree(sqlite_source)
    assert migrate_db._queue_digest(restored / "outbound-queue.sqlite3") == backup_digest
    queue = OutboundQueue(restored)
    try:
        payload = queue.connection.execute("SELECT payload FROM outbound_queue WHERE id = 1").fetchone()[0]
        _, kwargs = queue.decode_payload(payload)
        stream = kwargs["media"][0].media
        try:
            for _ in range(24):
                assert stream.read(len(chunk)) == chunk
            assert stream.read(1) == b""
        finally:
            stream.close()
        queue.cleanup_payload_media(payload)
        assert not list(queue.media_dir.glob("external-*"))
    finally:
        queue.close()


def test_external_queue_change_after_commit_prevents_recovery(sqlite_source, postgres_config, tmp_path):
    external = tmp_path / "upload.bin"
    external.write_bytes(b"before")
    external_queue_payload(sqlite_source, external)
    migrate_db.migrate(sqlite_source, postgres_config)
    external.write_bytes(b"after!")
    with pytest.raises(RuntimeError, match="Source changed"):
        migrate_db.migrate(sqlite_source, postgres_config)


def test_external_queue_change_during_import_rolls_back(sqlite_source, postgres_config, tmp_path, monkeypatch):
    external = tmp_path / "upload.bin"
    external.write_bytes(b"before")
    external_queue_payload(sqlite_source, external)
    original = migrate_db._import_rows

    def change_external(*args):
        result = original(*args)
        external.write_bytes(b"after!")
        return result

    monkeypatch.setattr(migrate_db, "_import_rows", change_external)
    with pytest.raises(RuntimeError, match="changed during import"):
        migrate_db.migrate(sqlite_source, postgres_config)
    target = postgresql_database(postgres_config)
    with connection_scope(target):
        assert target.get_tables(schema=current_schema(target)) == []
    target.close_all()
    assert not (sqlite_source / migrate_db.RECEIPT_FILE).exists()


def test_external_queue_digest_detects_changed_and_missing_files(sqlite_source, tmp_path):
    external = tmp_path / "upload.bin"
    external.write_bytes(b"before")
    external_queue_payload(sqlite_source, external)
    queue = sqlite_source / "outbound-queue.sqlite3"
    before = migrate_db._queue_digest(queue)
    external.write_bytes(b"after!")
    after = migrate_db._queue_digest(queue)
    assert before["sha256"] == after["sha256"]
    assert before["external"] != after["external"]
    assert not migrate_db._queue_digest_matches(after, before)
    legacy = {key: value for key, value in before.items() if key != "external"}
    assert migrate_db._queue_digest_matches(before, legacy, legacy_external=True)
    assert not migrate_db._queue_digest_matches(before, legacy)
    external.unlink()
    archive = tmp_path / "backup"
    archive.mkdir()
    with pytest.raises(RuntimeError, match="External queued media is missing"):
        migrate_db._backup_queue(queue, archive)


@pytest.mark.parametrize("altered", [None, "media", "queue"])
def test_migrate_recovers_relocated_external_archive(sqlite_source, postgres_config, tmp_path, altered):
    external = tmp_path / "external.bin"
    external.write_bytes(b"original media")
    external_queue_payload(sqlite_source, external)
    committed = migrate_db.migrate(sqlite_source, postgres_config)
    restored = tmp_path / "restored"
    shutil.copytree(committed["backup_directory"], restored)
    retried = migrate_db.migrate(sqlite_source, postgres_config)
    second_restore = tmp_path / "restored-again"
    shutil.copytree(retried["backup_directory"], second_restore)
    assert retried["import_id"] == committed["import_id"]
    assert retried["backup_queue"] == committed["backup_queue"]
    external.unlink()
    shutil.rmtree(sqlite_source)
    assert migrate_db._queue_digest(restored / "outbound-queue.sqlite3") == committed["backup_queue"]
    if altered == "media":
        next((restored / "outbound-media").glob("external-*")).write_bytes(b"modified media")
    elif altered == "queue":
        with closing(sqlite3.connect(restored / "outbound-queue.sqlite3")) as queue, queue:
            queue.execute("UPDATE outbound_queue SET created_at = created_at + 1 WHERE id = 1")
    before = migrate_db._queue_digest(restored / "outbound-queue.sqlite3")
    if altered:
        with pytest.raises(RuntimeError, match="Source changed"):
            migrate_db.migrate(restored, postgres_config)
        assert not (restored / migrate_db.RECEIPT_FILE).exists()
    for directory in ((second_restore,) if altered else (restored, second_restore, restored)):
        recovered = migrate_db.migrate(directory, postgres_config)
        assert recovered["import_id"] == committed["import_id"]
        assert recovered["tables"] == committed["tables"]
        assert recovered["backup_queue"] == committed["backup_queue"]
        target = postgresql_database(postgres_config)
        with connection_scope(target):
            migrate_db.validate_runtime_cutover(directory, target)
        target.close_all()
    assert migrate_db._queue_digest(restored / "outbound-queue.sqlite3") == before


def test_v1_cache_discovery_preserves_only_empty_unrecorded_caches(sqlite_source):
    manifest = {"version": 1, "tables": {model._meta.table_name: {} for model in MODELS}}
    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, source:
        # Empty schemas accepted by v1 need no type conversion on recovery.
        source.execute("CREATE TABLE topiciconcache (icon NUMERIC)")
        source.execute("CREATE TABLE useremojicache (emoji TEXT)")
        assert migrate_db._cache_models(source, manifest) == ()
        migrate_db._validate_source(source, MODELS)
        for table in sorted(migrate_db.CACHE_TABLES):
            source.execute(f'INSERT INTO "{table}" VALUES (?)', (1,))
            with pytest.raises(RuntimeError, match="Unrecorded source cache.*no longer empty"):
                migrate_db._cache_models(source, manifest)
            assert source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone() == (1,)
            source.execute(f'DELETE FROM "{table}"')


def test_import_recovery_cutover_preserves_published_history_and_queue_ownership(
    sqlite_source, postgres_config, manager_factory,
):
    manager = manager_factory(sqlite_source)
    manager.replace_history_migration_entries("slave.chat", -100123456789, 9001, [
        dict(slave_chat_id="slave.chat", target_chat_id="-100123456789", message_thread_id="9001",
             source_master_msg_id="z.1", formatted_text=text, position=position)
        for position, text in enumerate(("first", "second"))
    ])
    pending = manager.get_history_migration_entries("slave.chat", -100123456789, 9001)
    keys = {entry.ownership_key for entry in pending}
    with connection_scope(manager._managed_database):
        # A formatter interrupted before publication leaves a separate generation.
        abandoned = HistoryMigrationEntry.create(
            slave_chat_id="slave.chat", target_chat_id="-100123456789", message_thread_id="9001",
            source_master_msg_id="z.1", formatted_text="unpublished", position=0, generation="interrupted",
        )
    manager.stop_worker()
    queue = OutboundQueue(sqlite_source)

    def send_message(chat_id, text):
        raise AssertionError("Offline import must not send Telegram messages")

    try:
        queue.enqueue_many(
            [QueueRequest("send_message", (), {"chat_id": -100123456789, "text": "first"})],
            lambda _: send_message, history_keys=[pending[0].ownership_key],
        )
    finally:
        queue.close()
    before = source_rows(sqlite_source)
    committed = migrate_db.migrate(sqlite_source, postgres_config, batch_size=1)
    assert committed["tables"]["historymigrationtarget"]["rows"] == 1
    assert committed["tables"]["historymigrationentry"]["rows"] == 3
    (sqlite_source / migrate_db.RECEIPT_FILE).unlink()
    recovered = migrate_db.migrate(sqlite_source, postgres_config, batch_size=1)
    assert recovered["import_id"] == committed["import_id"]
    assert recovered["tables"] == committed["tables"]
    assert source_rows(sqlite_source) == before
    restarted = manager_factory(sqlite_source, postgres_config)
    assert restarted.get_next_history_migration_target().ownership_key == pending[0].ownership_key
    assert {entry.ownership_key for entry in restarted.get_history_migration_entries(
        "slave.chat", -100123456789, 9001,
    )} == keys
    with connection_scope(restarted._managed_database):
        assert not HistoryMigrationEntry.select().where(HistoryMigrationEntry.id == abandoned.id).exists()
    queue = OutboundQueue(sqlite_source)
    try:
        assert queue.owned_history_entries(keys) == {pending[0].ownership_key}
    finally:
        queue.close()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("added_fields", [False, True])
def test_old_five_table_import_recovers_and_initializes_runtime_indexes(
    sqlite_source, postgres_config, manager_factory, version, added_fields,
):
    receipt = migrate_db.migrate(sqlite_source, postgres_config)
    columns = {name: list(fields) for name, fields in migrate_db.V1_COLUMNS.items()}
    if version == 2:
        columns["msglog"] = receipt["columns"]["msglog"]
    with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, source:
        tables = {model._meta.table_name: migrate_db._digest(migrate_db._source_rows(
            source, model, columns[model._meta.table_name],
        )) for model in migrate_db.CORE_MODELS}
        if not added_fields:
            source.execute("DROP TABLE historymigrationtarget")
            source.execute("ALTER TABLE historymigrationentry DROP COLUMN generation")
            if version == 1:
                source.execute("ALTER TABLE msglog DROP COLUMN master_message_thread_id")
    manifest = dict(receipt, version=version, tables=tables)
    if version == 1:
        manifest.pop("columns")
    else:
        manifest["columns"] = columns
    target = postgresql_database(postgres_config)
    try:
        with connection_scope(target):
            target.execute_sql("DROP TABLE historymigrationtarget")
            target.execute_sql("ALTER TABLE historymigrationentry DROP COLUMN generation")
            if version == 1:
                target.execute_sql("ALTER TABLE msglog DROP COLUMN master_message_thread_id")
            target.execute_sql(f"UPDATE {migrate_db.IMPORT_TABLE} SET manifest = %s", (json.dumps(manifest),))
        (sqlite_source / migrate_db.RECEIPT_FILE).unlink()
        recovered = migrate_db.migrate(sqlite_source, postgres_config)
        assert recovered["tables"] == tables
        runtime = manager_factory(sqlite_source, postgres_config)
        assert runtime.get_next_history_migration_target().ownership_key == "legacy:74"
        with connection_scope(runtime._managed_database):
            indexes = {index.name for index in runtime._managed_database.get_indexes("historymigrationentry")}
            assert {"history_generation_id", "history_target_generation_position"} <= indexes
        runtime.stop_worker()
        # Startup adds nullable fields and an empty target table; those must not
        # invalidate the committed historical projection on an offline retry.
        assert migrate_db.migrate(sqlite_source, postgres_config)["tables"] == tables
    finally:
        target.close_all()


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("side", ["Source", "Target"])
def test_old_projection_rejects_unrecorded_generation_or_publication(sqlite_source, tmp_path, version, side):
    # Exercise both-side projection checks without requiring a PostgreSQL server.
    target = SqliteDatabase(str(tmp_path / "target.db"))
    columns = {name: list(fields) for name, fields in migrate_db.V1_COLUMNS.items()}
    manifest = {"version": version, "tables": {name: {} for name in columns}, "columns": columns}
    try:
        with target.bind_ctx(MODELS), target.connection_context():
            target.create_tables(MODELS)
            with closing(sqlite3.connect(sqlite_source / "tgdata.db")) as source, source:
                for model in MODELS:
                    rows = list(migrate_db._source_rows(source, model))
                    if rows:
                        model.insert_many(rows, fields=model._meta.sorted_fields).execute()
                projection = migrate_db._recovery_columns(MODELS, manifest)
                migrate_db._validate_recovery_extras(source, target, projection)
                execute = source.execute if side == "Source" else target.execute_sql
                execute("UPDATE historymigrationentry SET generation = 'published'")
                with pytest.raises(RuntimeError, match=f"{side} has unrecorded data in historymigrationentry.generation"):
                    migrate_db._validate_recovery_extras(source, target, projection)
                execute("UPDATE historymigrationentry SET generation = NULL")
                execute("INSERT INTO historymigrationtarget (slave_chat_id, target_chat_id, message_thread_id, generation) "
                        "VALUES ('slave.chat', '-100123456789', '9001', 'published')")
                with pytest.raises(RuntimeError, match=f"{side} has unrecorded data in historymigrationtarget"):
                    migrate_db._validate_recovery_extras(source, target, projection)
    finally:
        target.close()
