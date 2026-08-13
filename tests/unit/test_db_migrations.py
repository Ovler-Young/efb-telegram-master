from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot import MsgType
from ehforwarderbot.types import MessageID
from peewee import SQL, AutoField, BigIntegerField, BooleanField, DateTimeField, IntegerField, Model, PostgresqlDatabase, SqliteDatabase, TextField

from efb_telegram_master import db as db_module
from efb_telegram_master.db import DatabaseManager, MsgLog, database
from efb_telegram_master.msg_type import TGMsgType

_LEGACY_INGESTION_SCAN_SCHEMA = """
CREATE TABLE msglogingestionscan (
    id INTEGER NOT NULL PRIMARY KEY,
    source_chat_id TEXT NOT NULL UNIQUE,
    scan_boundary INTEGER NOT NULL,
    cursor INTEGER NOT NULL,
    existing_streak INTEGER NOT NULL,
    scanned_count INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL,
    existing_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at DATETIME,
    status TEXT NOT NULL,
    error TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""

_LEGACY_MSGLOG_SCHEMA = """
CREATE TABLE msglog (
    master_msg_id TEXT NOT NULL PRIMARY KEY,
    master_msg_id_alt TEXT,
    slave_message_id TEXT NOT NULL,
    text TEXT NOT NULL,
    slave_origin_uid TEXT NOT NULL,
    slave_origin_display_name TEXT,
    slave_member_uid TEXT,
    slave_member_display_name TEXT,
    media_type TEXT,
    mime TEXT,
    file_id TEXT,
    file_unique_id TEXT,
    msg_type TEXT NOT NULL,
    pickle BLOB,
    sent_to TEXT NOT NULL,
    sender_bot_id TEXT,
    time DATETIME
)
"""


def _legacy_outbound_models(test_database):
    class BaseModel(Model):
        class Meta:
            database = test_database

    class OutboundWorkflow(BaseModel):
        id = AutoField()
        state = TextField(default="active")
        result_task_id = BigIntegerField(null=True)
        error_class = TextField(null=True)
        created_at = DateTimeField(constraints=[SQL("DEFAULT CURRENT_TIMESTAMP")])
        completed_at = DateTimeField(null=True)

    class OutboundTask(BaseModel):
        id = AutoField()
        source_key = TextField()
        slave_id = TextField(null=True)
        priority = BooleanField(default=False)
        target_chat_id = BigIntegerField()
        message_thread_id = BigIntegerField(null=True)
        operation = TextField()
        payload = TextField()
        media_ref = TextField(null=True)
        workflow_id = BigIntegerField(index=True)
        step_index = IntegerField(default=0)
        depends_on_task_id = BigIntegerField(null=True)
        run_condition = TextField(default="always")
        result_payload = TextField(null=True)
        log_payload = TextField(null=True)
        required_sender_bot_id = TextField(null=True)
        state = TextField(default="queued")
        available_at = DateTimeField(null=True)
        lease_owner = TextField(null=True)
        lease_until = DateTimeField(null=True)
        lease_heartbeat_at = DateTimeField(null=True)
        submitted_at = DateTimeField(null=True)
        attempt_count = IntegerField(default=0)
        accepted_at = DateTimeField(constraints=[SQL("DEFAULT CURRENT_TIMESTAMP")])
        error_class = TextField(null=True)
        last_error = TextField(null=True)

        class Meta:
            indexes = (
                (("source_key", "priority", "accepted_at", "id"), False),
                (("state", "available_at"), False),
                (("workflow_id", "step_index"), True),
            )

    return OutboundWorkflow, OutboundTask


def test_fresh_database_defines_msglog_provenance(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.fresh", config={}))
    try:
        msglog_columns = {column.name for column in database.get_columns("msglog")}
        tables = set(database.get_tables())
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert "provenance" in msglog_columns
    assert "msglogingestionscan" not in tables


def test_startup_adds_provenance_to_legacy_sqlite_msglog_and_is_idempotent(tmp_path, monkeypatch):
    original_database = database.obj
    database_path = tmp_path / "tgdata.db"
    legacy_database = SqliteDatabase(database_path)
    legacy_database.connect()
    legacy_database.execute_sql(_LEGACY_MSGLOG_SCHEMA)
    legacy_database.execute_sql(
        "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, msg_type, sent_to) VALUES (?, ?, ?, ?, ?, ?)",
        ("100.1", "legacy-message", "legacy text", "tests.slave chat", "Text", "tests.master"),
    )
    legacy_database.close()
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)

    first_manager = DatabaseManager(SimpleNamespace(channel_id="tests.legacy-msglog", config={}))
    second_manager = None
    try:
        first_manager.stop_worker()
        second_manager = DatabaseManager(SimpleNamespace(channel_id="tests.legacy-msglog", config={}))
        columns = {column.name for column in database.get_columns("msglog")}
        row = database.execute_sql("SELECT provenance FROM msglog WHERE master_msg_id = ?", ("100.1",)).fetchone()
    finally:
        if second_manager is not None:
            second_manager.stop_worker()
        database.initialize(original_database)

    assert "provenance" in columns
    assert row == ("live",)


def test_msglog_provenance_migration_locks_postgresql_before_altering(monkeypatch):
    original_database = database.obj
    postgresql_database = PostgresqlDatabase("tests")
    columns = [SimpleNamespace(name="master_msg_id")]
    statements = []

    monkeypatch.setattr(postgresql_database, "get_binary_type", lambda: bytes)
    monkeypatch.setattr(postgresql_database, "atomic", lambda: nullcontext())
    monkeypatch.setattr(postgresql_database, "get_columns", lambda _table: columns)

    def execute_sql(statement):
        statements.append(statement)
        if statement.startswith("ALTER TABLE"):
            columns.append(SimpleNamespace(name="provenance"))

    monkeypatch.setattr(postgresql_database, "execute_sql", execute_sql)
    database.initialize(postgresql_database)
    try:
        DatabaseManager._ensure_msglog_provenance()
        DatabaseManager._ensure_msglog_provenance()
    finally:
        database.initialize(original_database)

    assert statements == [
        'LOCK TABLE "msglog" IN ACCESS EXCLUSIVE MODE',
        'ALTER TABLE "msglog" ADD COLUMN "provenance" TEXT NOT NULL DEFAULT \'live\'',
        'LOCK TABLE "msglog" IN ACCESS EXCLUSIVE MODE',
    ]


def test_startup_retires_the_exact_legacy_msglog_ingestion_scan_table(tmp_path, monkeypatch):
    original_database = database.obj
    database_path = tmp_path / "tgdata.db"
    legacy_database = SqliteDatabase(database_path)
    legacy_database.connect()
    legacy_database.execute_sql(_LEGACY_INGESTION_SCAN_SCHEMA)
    legacy_database.close()
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)

    manager = DatabaseManager(SimpleNamespace(channel_id="tests.legacy", config={}))
    try:
        tables = set(database.get_tables())
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert "msglogingestionscan" not in tables


def test_startup_keeps_a_same_name_table_with_a_different_schema_signature(tmp_path, monkeypatch):
    original_database = database.obj
    database_path = tmp_path / "tgdata.db"
    collision_database = SqliteDatabase(database_path)
    collision_database.connect()
    collision_database.execute_sql(_LEGACY_INGESTION_SCAN_SCHEMA.replace("id INTEGER NOT NULL PRIMARY KEY", "id TEXT NOT NULL PRIMARY KEY", 1))
    collision_database.close()
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)

    manager = DatabaseManager(SimpleNamespace(channel_id="tests.collision", config={}))
    try:
        tables = set(database.get_tables())
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert "msglogingestionscan" in tables


def test_startup_retires_historical_outbound_tables_in_task_before_workflow_order(tmp_path, monkeypatch):
    original_database = database.obj
    database_path = tmp_path / "tgdata.db"
    legacy_database = SqliteDatabase(database_path)
    legacy_database.connect()
    try:
        workflow, task = _legacy_outbound_models(legacy_database)
        legacy_database.create_tables([workflow, task])
    finally:
        legacy_database.close()
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    original_execute_sql = SqliteDatabase.execute_sql
    drops = []

    def record_drop(database_instance, statement, parameters=None):
        if statement.startswith('DROP TABLE "outbound'):
            drops.append(statement)
        return original_execute_sql(database_instance, statement, parameters)

    monkeypatch.setattr(SqliteDatabase, "execute_sql", record_drop)

    manager = DatabaseManager(SimpleNamespace(channel_id="tests.legacy-outbound", config={}))
    try:
        tables = set(database.get_tables())
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert "outboundworkflow" not in tables
    assert "outboundtask" not in tables
    assert drops == ['DROP TABLE "outboundtask"', 'DROP TABLE "outboundworkflow"']


def test_startup_preserves_historical_outbound_name_collision(tmp_path, monkeypatch):
    original_database = database.obj
    database_path = tmp_path / "tgdata.db"
    collision_database = SqliteDatabase(database_path)
    collision_database.connect()
    try:
        collision_database.execute_sql("CREATE TABLE outboundworkflow (id INTEGER PRIMARY KEY, unrelated TEXT)")
        _workflow, task = _legacy_outbound_models(collision_database)
        collision_database.create_tables([task])
    finally:
        collision_database.close()
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)

    try:
        with pytest.raises(RuntimeError, match="Legacy outbound schema collision"):
            DatabaseManager(SimpleNamespace(channel_id="tests.outbound-collision", config={}))
        collision_database = SqliteDatabase(database_path)
        collision_database.connect()
        try:
            assert {"outboundworkflow", "outboundtask"}.issubset(collision_database.get_tables())
        finally:
            collision_database.close()
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


def test_postgresql_legacy_outbound_collision_acquires_the_startup_lock(monkeypatch):
    original_database = database.obj
    postgresql_database = PostgresqlDatabase("tests")
    statements = []
    monkeypatch.setattr(postgresql_database, "get_binary_type", lambda: bytes)
    monkeypatch.setattr(postgresql_database, "atomic", lambda: nullcontext())
    monkeypatch.setattr(postgresql_database, "get_tables", lambda: ["outboundworkflow"])
    monkeypatch.setattr(postgresql_database, "execute_sql", lambda statement, parameters=None: statements.append((statement, parameters)))
    database.initialize(postgresql_database)
    try:
        with pytest.raises(RuntimeError, match="partial-schema collision"):
            DatabaseManager._retire_legacy_outbound_tables()
    finally:
        database.initialize(original_database)

    assert statements == [("SELECT pg_advisory_xact_lock(%s)", (DatabaseManager._LEGACY_OUTBOUND_LOCK_KEY,))]


def test_legacy_scan_schema_normalization_accepts_postgresql_names_and_rejects_lookalikes():
    postgresql_id = SimpleNamespace(name="id", data_type="integer", null=False, primary_key=True)
    postgresql_timestamp = SimpleNamespace(name="created_at", data_type="timestamp without time zone", null=False, primary_key=False)
    lookalike_id = SimpleNamespace(name="id", data_type="text", null=False, primary_key=True)

    assert DatabaseManager._legacy_msglog_ingestion_scan_column_signature(postgresql_id) == ("id", "integer", False, True)
    assert DatabaseManager._legacy_msglog_ingestion_scan_column_signature(postgresql_timestamp) == ("created_at", "datetime", False, False)
    assert DatabaseManager._legacy_msglog_ingestion_scan_column_signature(lookalike_id) != ("id", "integer", False, True)


def test_ingested_msglog_persistence_is_idempotent():
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    manager = object.__new__(DatabaseManager)
    manager.channel = SimpleNamespace(channel_id="tests")
    try:
        test_db.create_tables([MsgLog])
        content = SimpleNamespace(
            text="ingested",
            media_type="Text",
            mime=None,
            msg_type="Text",
            time=datetime(2026, 8, 4),
        )
        assert manager.persist_ingested_msglog(100, 500, "tests.slave target", content) == "inserted"
        assert manager.persist_ingested_msglog(100, 500, "tests.slave target", content) == "existing"
        row = MsgLog.get_by_id("100.500")
    finally:
        test_db.close()
        database.initialize(original_database)

    assert row.provenance == "mtproto_ingested"
    assert row.slave_message_id == "mtproto-ingested:100.500"


def test_live_message_overwrites_synthetic_provenance():
    test_db = SqliteDatabase(":memory:")
    manager = object.__new__(DatabaseManager)
    manager.logger = Mock()
    message = SimpleNamespace(
        uid=MessageID("live-message"),
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.slave", uid="author"),
        text="live text",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests.master"),
        file_id=None,
        file_unique_id=None,
        mime=None,
        is_system=False,
        attributes=None,
        commands=None,
        substitutions=None,
        target=None,
        sender_bot_id=None,
        reactions={},
    )
    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        MsgLog.create(
            master_msg_id="100.1",
            slave_message_id="mtproto-ingested:100.1",
            text="ingested",
            slave_origin_uid="tests.slave stale",
            slave_member_uid="tests.slave __self__",
            msg_type="Text",
            sent_to="tests.master",
            provenance="mtproto_ingested",
        )
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=1))
        row = MsgLog.get_by_id("100.1")

    assert (row.provenance, row.slave_message_id, row.text) == ("live", "live-message", "live text")
