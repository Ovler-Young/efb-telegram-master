import logging
import pickle
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ehforwarderbot import MsgType
from ehforwarderbot.types import MessageID
from peewee import IndexMetadata, Model, PostgresqlDatabase, SqliteDatabase
from prometheus_client import generate_latest

from efb_telegram_master import db as db_module
from efb_telegram_master import utils
from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.models import HistoryMigrationEntry, MsgLog, TopicAssoc, database
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.msglog_repository import MsgLogRepository
from efb_telegram_master.outbound_types import SendReceipt
from efb_telegram_master.slave_message import SlaveMessageService
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID, TelegramTopicID
from tests.support.legacy_outbound_schema import legacy_outbound_models


def test_msglog_schema_has_sender_bot_id(channel):
    assert "sender_bot_id" in {column.name for column in database.get_columns("msglog")}


def test_history_migration_entry_schema_retains_replay_columns_without_msglog_legacy_field():
    test_db = SqliteDatabase(":memory:")

    with test_db.bind_ctx([HistoryMigrationEntry, MsgLog]):
        test_db.create_tables([HistoryMigrationEntry, MsgLog])
        history_columns = {column.name for column in test_db.get_columns("historymigrationentry")}
        msglog_columns = {column.name for column in test_db.get_columns("msglog")}

    assert {
        "id",
        "slave_chat_id",
        "target_chat_id",
        "message_thread_id",
        "source_master_msg_id",
        "formatted_text",
        "media_type",
        "source_time",
        "position",
        "created_at",
    }.issubset(history_columns)
    assert "source_master_msg_id" not in msglog_columns


def test_database_method_metrics_record_bounded_public_operation_labels(channel):
    metrics = Metrics()
    channel.db.set_metrics(metrics)

    assert channel.chat_associations.get_chat_assoc(master_uid="metrics-master") == []
    with pytest.raises(ValueError, match="Only one parameter"):
        channel.msglogs.get_msg_log()

    rendered = generate_latest(metrics.registry).decode()

    assert 'etm_database_method_duration_seconds_count{method="get_chat_assoc"} 1.0' in rendered
    assert 'etm_database_method_failures_total{method="get_msg_log"} 1.0' in rendered
    assert "metrics-master" not in rendered


def test_database_manager_uses_transactional_wal_sqlite(tmp_path, monkeypatch):
    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.sqlite", config={}))
    try:
        assert isinstance(database.obj, SqliteDatabase)
        assert database.obj.pragma("journal_mode").lower() == "wal"
    finally:
        manager.stop_worker()
        database.initialize(original_database)


def test_database_manager_closes_sqlite_when_schema_creation_fails(tmp_path, monkeypatch):
    original_database = database.obj
    original_close = SqliteDatabase.close
    closed_databases = []

    def close(instance, *args, **kwargs):
        closed_databases.append(instance)
        return original_close(instance, *args, **kwargs)

    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(DatabaseManager, "_create", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("schema creation failed"))))
    monkeypatch.setattr(SqliteDatabase, "close", close)
    try:
        with pytest.raises(RuntimeError, match="schema creation failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-failure", config={}))

        assert len(closed_databases) == 1
        assert database.is_closed()
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


def test_database_manager_closes_sqlite_when_post_connect_logging_fails(tmp_path, monkeypatch):
    original_database = database.obj
    original_close = SqliteDatabase.close
    logger = Mock()
    closed_databases = []

    def close(instance, *args, **kwargs):
        closed_databases.append(instance)
        return original_close(instance, *args, **kwargs)

    logger.debug.side_effect = lambda message: (_ for _ in ()).throw(RuntimeError("post-connect logging failed")) if message == "Database loaded." else None
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(DatabaseManager, "logger", logger)
    monkeypatch.setattr(SqliteDatabase, "close", close)
    try:
        with pytest.raises(RuntimeError, match="post-connect logging failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-log-failure", config={}))

        assert len(closed_databases) == 1
        assert database.is_closed()
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


def test_database_manager_does_not_close_sqlite_when_connect_fails(tmp_path, monkeypatch):
    original_database = database.obj
    original_connect = SqliteDatabase.connect
    original_close = SqliteDatabase.close
    closed_databases = []

    def connect(instance, *args, **kwargs):
        raise RuntimeError("connect failed")

    def close(instance, *args, **kwargs):
        closed_databases.append(instance)
        return original_close(instance, *args, **kwargs)

    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(SqliteDatabase, "connect", connect)
    monkeypatch.setattr(SqliteDatabase, "close", close)
    try:
        with pytest.raises(RuntimeError, match="connect failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-connect-failure", config={}))

        assert closed_databases == []
    finally:
        monkeypatch.setattr(SqliteDatabase, "connect", original_connect)
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


def test_database_manager_preserves_initialization_error_when_cleanup_logging_fails(tmp_path, monkeypatch):
    original_database = database.obj
    logger = Mock()
    cleanup = Mock(side_effect=RuntimeError("cleanup failed"))
    logger.exception.side_effect = RuntimeError("cleanup logging failed")

    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(DatabaseManager, "logger", logger)
    monkeypatch.setattr(DatabaseManager, "_create", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("schema creation failed"))))
    monkeypatch.setattr(DatabaseManager, "stop_worker", cleanup)
    try:
        with pytest.raises(RuntimeError, match="schema creation failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-cleanup-logging-failure", config={}))

        cleanup.assert_called_once_with()
        logger.exception.assert_called_once_with("Failed to close database after database initialization failed.")
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


def test_database_manager_stops_and_closes_postgresql_pool_when_retirement_fails(monkeypatch):
    original_database = database.obj
    pool = Mock()
    pooled_database = patch("playhouse.postgres_ext.PooledPostgresqlExtDatabase", return_value=pool)
    pooled_database.start()
    pool.connect.return_value = True
    pool.is_closed.return_value = False
    monkeypatch.setattr(DatabaseManager, "_create", staticmethod(lambda: None))
    monkeypatch.setattr(DatabaseManager, "_retire_legacy_outbound_tables", lambda _self: (_ for _ in ()).throw(RuntimeError("retirement failed")))
    try:
        with pytest.raises(RuntimeError, match="retirement failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-failure", config={"database": {"type": "postgresql"}}))

        pool.connect.assert_called_once_with()
        pool.stop.assert_called_once_with()
        pool.close.assert_called_once_with()
    finally:
        pooled_database.stop()
        database.initialize(original_database)


def test_startup_retires_legacy_outbound_tables_after_current_schema_creation(tmp_path, monkeypatch, caplog):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        workflow, task = legacy_outbound_models(raw_db)
        raw_db.create_tables([workflow, task])
        workflow.create()
        task.create(source_key="source", target_chat_id=1, operation="send_message", payload="secret", workflow_id=1)
        task.create(source_key="source", target_chat_id=1, operation="send_message", payload="secret", workflow_id=1, step_index=1)
        assert {
            column.name: DatabaseManager._legacy_default_category(raw_db, "outboundworkflow", column.name, DatabaseManager._legacy_column_type(column.data_type), column.primary_key, column.default)
            for column in raw_db.get_columns("outboundworkflow")
        } == {
            "id": "auto_pk",
            "state": "none",
            "result_task_id": "none",
            "error_class": "none",
            "created_at": "current_timestamp",
            "completed_at": "none",
        }
        assert {
            column.name: DatabaseManager._legacy_default_category(raw_db, "outboundtask", column.name, DatabaseManager._legacy_column_type(column.data_type), column.primary_key, column.default)
            for column in raw_db.get_columns("outboundtask")
        } == {
            **{column_name: "none" for column_name, *_details in DatabaseManager._LEGACY_OUTBOUND_COLUMNS["outboundtask"]},
            "id": "auto_pk",
            "accepted_at": "current_timestamp",
        }
        assert DatabaseManager._legacy_outbound_schema_error(raw_db, "outboundworkflow") is None
        assert DatabaseManager._legacy_outbound_schema_error(raw_db, "outboundtask") is None
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    with caplog.at_level(logging.WARNING, logger="efb_telegram_master.db"):
        first_manager = DatabaseManager(SimpleNamespace(channel_id="tests.legacy", config={}))
    second_manager = None
    try:
        table_names = set(database.get_tables())
        assert not set(DatabaseManager._LEGACY_OUTBOUND_TABLES) & table_names
        assert {"chatassoc", "msglog", "historymigrationentry", "msglogingestionscan"}.issubset(table_names)
        first_manager.stop_worker()
        second_manager = DatabaseManager(SimpleNamespace(channel_id="tests.legacy", config={}))
    finally:
        if second_manager is not None:
            second_manager.stop_worker()
        else:
            first_manager.stop_worker()
        database.initialize(original_database)

    assert "Discarding obsolete durable outbound queue rows without resumption: workflows=1 tasks=2" in caplog.text


@pytest.mark.parametrize(
    ("table_name", "column_name", "data_type", "primary_key", "default", "expected"),
    (
        ("outboundworkflow", "id", "integer", True, None, "auto_pk"),
        ("outboundworkflow", "id", "integer", True, "nextval('outboundworkflow_id_seq'::regclass)", "auto_pk"),
        ("outboundtask", "id", "integer", True, "nextval('public.outboundtask_id_seq'::regclass)", "auto_pk"),
        ("outboundtask", "id", "integer", True, 'nextval(\'"public"."outboundtask_id_seq"\'::regclass)', "auto_pk"),
        ("outboundtask", "id", "integer", True, " ( NEXTVAL ( '\"outboundtask_id_seq\"' :: REGCLASS ) ) ", "auto_pk"),
        ("outboundtask", "id", "integer", True, "nextval('outboundworkflow_id_seq'::regclass)", "invalid:nextval('outboundworkflow_id_seq'::regclass)"),
        ("outboundtask", "id", "integer", True, "nextval('evil.outboundtask_id_seq'::regclass)", "invalid:nextval('evil.outboundtask_id_seq'::regclass)"),
        ("outboundtask", "id", "integer", True, "nextval('outboundtask_id_seq'::text::regclass)", "invalid:nextval('outboundtask_id_seq'::text::regclass)"),
        ("outboundtask", "state", "text", False, "nextval('outboundtask_id_seq'::regclass)", "invalid:nextval('outboundtask_id_seq'::regclass)"),
        ("outboundtask", "id", "integer", True, "generated by default as identity", "invalid:generated by default as identity"),
    ),
)
def test_legacy_auto_primary_key_default_categories(table_name, column_name, data_type, primary_key, default, expected):
    backend = SqliteDatabase(":memory:") if default is None else db_module.PostgresqlDatabase("tests")
    assert DatabaseManager._legacy_default_category(backend, table_name, column_name, data_type, primary_key, default) == expected


def test_postgresql_legacy_outbound_index_fixture_excludes_only_the_primary_key_index(monkeypatch):
    backend = PostgresqlDatabase("tests")
    introspected_indexes = (
        IndexMetadata("outboundtask_pkey", "CREATE UNIQUE INDEX outboundtask_pkey ON outboundtask (id)", ["id"], True, "outboundtask"),
        IndexMetadata(
            "outboundtask_workflow_id_step_index",
            "CREATE UNIQUE INDEX outboundtask_workflow_id_step_index ON outboundtask (workflow_id, step_index)",
            ["workflow_id", "step_index"],
            True,
            "outboundtask",
        ),
        IndexMetadata(
            "outboundtask_source_key_priority_accepted_at_id",
            "CREATE INDEX outboundtask_source_key_priority_accepted_at_id ON outboundtask (source_key, priority, accepted_at, id)",
            ["source_key", "priority", "accepted_at", "id"],
            False,
            "outboundtask",
        ),
        IndexMetadata("outboundtask_state_available_at", "CREATE INDEX outboundtask_state_available_at ON outboundtask (state, available_at)", ["state", "available_at"], False, "outboundtask"),
        IndexMetadata("outboundtask_workflow_id", "CREATE INDEX outboundtask_workflow_id ON outboundtask (workflow_id)", ["workflow_id"], False, "outboundtask"),
    )
    monkeypatch.setattr(backend, "get_indexes", lambda _table_name: introspected_indexes)

    assert set(DatabaseManager._legacy_outbound_task_indexes(backend)) == set(DatabaseManager._LEGACY_OUTBOUND_TASK_INDEXES)

    monkeypatch.setattr(backend, "get_indexes", lambda _table_name: (*introspected_indexes, IndexMetadata("outboundtask_unexpected", "", ["state"], False, "outboundtask")))

    assert set(DatabaseManager._legacy_outbound_task_indexes(backend)) != set(DatabaseManager._LEGACY_OUTBOUND_TASK_INDEXES)


@pytest.mark.parametrize("failure", ("count", "drop"))
def test_startup_aborts_when_legacy_outbound_table_retirement_fails(tmp_path, monkeypatch, failure):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        workflow, task = legacy_outbound_models(raw_db)
        raw_db.create_tables([workflow, task])
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    if failure == "count":
        monkeypatch.setattr(db_module.fn, "COUNT", lambda *_args: (_ for _ in ()).throw(RuntimeError("count failed")))
    else:

        class LegacyOutboundTable(Model):
            class Meta:
                database = database
                table_name = "outboundworkflow"

        monkeypatch.setattr(DatabaseManager, "_legacy_table_model", staticmethod(lambda _table_name, _database: LegacyOutboundTable))
        monkeypatch.setattr(SqliteDatabase, "drop_tables", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("drop failed")))

    try:
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            DatabaseManager(SimpleNamespace(channel_id=f"tests.legacy-{failure}", config={}))
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


@pytest.mark.parametrize("legacy_table", DatabaseManager._LEGACY_OUTBOUND_TABLES)
def test_startup_aborts_without_dropping_partial_historical_schema(tmp_path, monkeypatch, legacy_table):
    raw_db = SqliteDatabase(tmp_path / "tgdata.db")
    raw_db.connect()
    try:
        workflow, task = legacy_outbound_models(raw_db)
        raw_db.create_tables([workflow if legacy_table == "outboundworkflow" else task])
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        with pytest.raises(RuntimeError, match="partial-schema collision"):
            DatabaseManager(SimpleNamespace(channel_id=f"tests.partial-{legacy_table}", config={}))
        collision_db = SqliteDatabase(tmp_path / "tgdata.db")
        collision_db.connect()
        try:
            assert legacy_table in collision_db.get_tables()
        finally:
            collision_db.close()
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


def test_startup_aborts_without_dropping_same_named_schema_collision(tmp_path, monkeypatch):
    raw_db = SqliteDatabase(tmp_path / "tgdata.db")
    raw_db.connect()
    try:
        raw_db.execute_sql("CREATE TABLE outboundworkflow (id INTEGER PRIMARY KEY, unrelated TEXT)")
        _workflow, task = legacy_outbound_models(raw_db)
        raw_db.create_tables([task])
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        with pytest.raises(RuntimeError, match="Legacy outbound schema collision"):
            DatabaseManager(SimpleNamespace(channel_id="tests.collision", config={}))
        collision_db = SqliteDatabase(tmp_path / "tgdata.db")
        collision_db.connect()
        try:
            assert "outboundworkflow" in collision_db.get_tables()
        finally:
            collision_db.close()
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    (
        ("outboundworkflow", "created_at"),
        ("outboundtask", "state"),
    ),
)
def test_startup_refuses_legacy_tables_with_default_collisions(tmp_path, monkeypatch, table_name, column_name):
    raw_db = SqliteDatabase(tmp_path / "tgdata.db")
    raw_db.connect()
    try:
        workflow, task = legacy_outbound_models(raw_db)
        raw_db.create_tables([workflow, task])
        table_sql = raw_db.execute_sql("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()[0]
        index_sql = [row[0] for row in raw_db.execute_sql("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", (table_name,)).fetchall()]
        raw_db.execute_sql(f'DROP TABLE "{table_name}"')
        expected_default = "DEFAULT CURRENT_TIMESTAMP" if column_name == "created_at" else ""
        changed_sql = (
            table_sql.replace(f'"{column_name}" DATETIME NOT NULL {expected_default}', f"\"{column_name}\" DATETIME NOT NULL DEFAULT 'unexpected'")
            if column_name == "created_at"
            else table_sql.replace(f'"{column_name}" TEXT NOT NULL', f"\"{column_name}\" TEXT NOT NULL DEFAULT 'unexpected'")
        )
        raw_db.execute_sql(changed_sql)
        for statement in index_sql:
            raw_db.execute_sql(statement)
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        with pytest.raises(RuntimeError, match="schema collision"):
            DatabaseManager(SimpleNamespace(channel_id=f"tests.defaults-{table_name}", config={}))
        collision_db = SqliteDatabase(tmp_path / "tgdata.db")
        collision_db.connect()
        try:
            assert set(DatabaseManager._LEGACY_OUTBOUND_TABLES).issubset(collision_db.get_tables())
        finally:
            collision_db.close()
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


def test_startup_refuses_altered_accepted_at_default_without_dropping_legacy_schema(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        workflow, task = legacy_outbound_models(raw_db)
        raw_db.create_tables([workflow, task])
        workflow.create()
        task.create(source_key="source", target_chat_id=1, operation="send_message", payload="secret", workflow_id=1)
        task_sql = raw_db.execute_sql("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", ("outboundtask",)).fetchone()[0]
        index_sql = [row[0] for row in raw_db.execute_sql("SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL", ("outboundtask",)).fetchall()]
        raw_db.execute_sql("DROP TABLE outboundtask")
        raw_db.execute_sql(task_sql.replace("DEFAULT CURRENT_TIMESTAMP", "DEFAULT 'unexpected'"))
        for statement in index_sql:
            raw_db.execute_sql(statement)
        task.create(source_key="source", target_chat_id=1, operation="send_message", payload="secret", workflow_id=1)
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    try:
        with pytest.raises(RuntimeError, match="schema collision"):
            DatabaseManager(SimpleNamespace(channel_id="tests.accepted-at-default", config={}))
        collision_db = SqliteDatabase(database_path)
        collision_db.connect()
        try:
            assert set(DatabaseManager._LEGACY_OUTBOUND_TABLES).issubset(collision_db.get_tables())
            assert collision_db.execute_sql("SELECT COUNT(*) FROM outboundworkflow").fetchone()[0] == 1
            assert collision_db.execute_sql("SELECT COUNT(*) FROM outboundtask").fetchone()[0] == 1
            assert {index.name for index in collision_db.get_indexes("outboundtask")} == {
                "outboundtask_source_key_priority_accepted_at_id",
                "outboundtask_state_available_at",
                "outboundtask_workflow_id",
                "outboundtask_workflow_id_step_index",
            }
        finally:
            collision_db.close()
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


def test_sqlite_legacy_retirement_rolls_back_when_workflow_drop_fails(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        workflow, task = legacy_outbound_models(raw_db)
        raw_db.create_tables([workflow, task])
        workflow.create()
        task.create(source_key="source", target_chat_id=1, operation="send_message", payload="secret", workflow_id=1)
    finally:
        raw_db.close()

    original_database = database.obj
    original_drop_tables = SqliteDatabase.drop_tables
    drop_calls = 0

    def fail_workflow_drop(instance, models, **kwargs):
        nonlocal drop_calls
        drop_calls += 1
        if drop_calls == 2:
            raise RuntimeError("workflow drop failed")
        return original_drop_tables(instance, models, **kwargs)

    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(SqliteDatabase, "drop_tables", fail_workflow_drop)
    try:
        with pytest.raises(RuntimeError, match="workflow drop failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.rollback", config={}))
        restored_db = SqliteDatabase(database_path)
        restored_db.connect()
        try:
            assert set(DatabaseManager._LEGACY_OUTBOUND_TABLES).issubset(restored_db.get_tables())
            assert restored_db.execute_sql("SELECT COUNT(*) FROM outboundworkflow").fetchone()[0] == 1
            assert restored_db.execute_sql("SELECT COUNT(*) FROM outboundtask").fetchone()[0] == 1
            assert DatabaseManager._legacy_outbound_schema_error(restored_db, "outboundtask") is None
        finally:
            restored_db.close()
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


def test_topic_assoc_table_exists_and_round_trips(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(11111)
    thread_id = TelegramTopicID(22222)

    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)
    assoc = channel.chat_associations.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    assert isinstance(assoc, TopicAssoc)
    assert channel.chat_associations.get_topic_thread_id(slave_uid, topic_chat_id) == thread_id
    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)


def test_topic_assoc_is_replaced_for_same_slave_and_thread(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(33333)

    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)
    channel.chat_associations.add_topic_assoc(topic_chat_id, TelegramTopicID(44444), slave_uid)
    channel.chat_associations.add_topic_assoc(topic_chat_id, TelegramTopicID(55555), slave_uid)

    assert channel.chat_associations.get_topic_thread_id(slave_uid, topic_chat_id) == TelegramTopicID(55555)
    assert TopicAssoc.select().where(TopicAssoc.slave_uid == slave_uid).count() == 1
    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)


def test_remove_chat_assoc_removes_topic_assoc(channel):
    channel.chat_associations.add_chat_assoc("master-topic-cleanup", "slave-topic-cleanup", multiple_slave=True)
    channel.chat_associations.add_topic_assoc(TelegramChatID(66666), TelegramTopicID(77777), "slave-topic-cleanup")

    channel.chat_associations.remove_chat_assoc(master_uid="master-topic-cleanup")

    assert channel.chat_associations.get_topic_thread_id("slave-topic-cleanup", TelegramChatID(66666)) is None


def test_slave_delivery_receipt_persists_sender_bot_id():
    test_db = SqliteDatabase(":memory:")
    repository = MsgLogRepository()
    author = SimpleNamespace(module_id="tests.slave", uid="author")
    chat = SimpleNamespace(module_id="tests.slave", uid="chat", get_member=lambda _uid: author)
    message = SimpleNamespace(
        uid=MessageID("receipt-provenance-message"),
        chat=chat,
        author=author,
        text="receipt provenance",
        type=MsgType.Text,
        target=None,
        commands=None,
        reactions={},
        is_system=False,
        attributes=None,
        substitutions=None,
        deliver_to=SimpleNamespace(channel_id="tests.master"),
    )
    receipt = SendReceipt(
        SimpleNamespace(chat=SimpleNamespace(id=123456), chat_id=123456, message_id=654321),
        sender_bot_id="777",
    )
    processor = object.__new__(SlaveMessageService)
    processor.logger = SimpleNamespace(debug=lambda *_args: None, warning=lambda *_args: None)
    processor.msglogs = repository
    processor.chat_manager = SimpleNamespace(update_chat_obj=lambda value: value)
    processor.router = SimpleNamespace(resolve_reply=lambda *_args: None)
    processor.commands = SimpleNamespace(register_command=lambda *_args: None)
    processor.text_delivery = SimpleNamespace(text=lambda *_args: receipt)
    processor._pending_slave_messages = set()
    processor._pending_slave_messages_lock = threading.Lock()

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        processor.dispatch_message(message, "", None, 123456, None)

        stored = repository.get_msg_log(master_msg_id="123456.654321")
        assert stored is not None and stored.sender_bot_id == "777"


def test_build_etm_msg_restores_sender_bot_id(channel, slave):
    chat = slave.chat_with_alias
    etm_msg = ETMMsg(
        uid=MessageID("restored-message"),
        chat=channel.chat_manager.update_chat_obj(chat),
        author=channel.chat_manager.get_or_enrol_member(chat, chat.other),
        text="restored",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=channel,
    )

    channel.msglogs.add_or_update_message_log(etm_msg, SimpleNamespace(chat_id=4444, message_id=5555), sender_bot_id="888")
    row = channel.msglogs.get_msg_log(master_msg_id="4444.5555")
    assert row is not None and row.build_etm_msg(channel.chat_manager).sender_bot_id == "888"
    row.delete_instance()


def _reaction_message(reactor, reactions):
    return SimpleNamespace(
        uid=MessageID("reaction-message"),
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.mocks.slave", uid="author"),
        text="message",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
        file_id=None,
        file_unique_id=None,
        mime=None,
        is_system=False,
        attributes=None,
        commands=None,
        substitutions=None,
        target=None,
        sender_bot_id=None,
        reactions=reactions,
    )


def test_reaction_alternate_updates_one_canonical_row_and_clears_retraction():
    test_db = SqliteDatabase(":memory:")
    manager = MsgLogRepository()
    reactor = SimpleNamespace(module_id="tests.mocks.slave", uid="reactor")
    message = _reaction_message(reactor, {})

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=10))
        message.reactions = {"R0": [reactor]}
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=11), old_message_id=(TelegramChatID(100), TelegramMessageID(10)), sender_bot_id="777")
        row = MsgLog.get()
        assert (MsgLog.select().count(), row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == (1, "100.10", "100.11", "777")
        assert pickle.loads(bytes(row.pickle))["reactions"] == {"R0": ("tests.mocks.slave reactor",)}

        message.reactions = {}
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=11), old_message_id=(TelegramChatID(100), TelegramMessageID(11)), sender_bot_id="777")
        row = MsgLog.get()
        assert (MsgLog.select().count(), row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id, row.pickle) == (1, "100.10", "100.11", "777", None)


def test_reaction_alternate_db_failures_preserve_then_update_canonical_row():
    test_db = SqliteDatabase(":memory:")
    manager = MsgLogRepository()
    reactor = SimpleNamespace(module_id="tests.mocks.slave", uid="reactor")
    message = _reaction_message(reactor, {"NEW": [reactor]})

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        for initial_alt, old_id, failed_id, success_id in ((None, 10, 11, 12), ("100.11", 11, 12, 13)):
            MsgLog.delete().execute()
            MsgLog.create(
                master_msg_id="100.10",
                master_msg_id_alt=initial_alt,
                slave_message_id="reaction-message",
                text="old",
                slave_origin_uid="tests.mocks.slave chat",
                slave_member_uid="tests.mocks.slave author",
                msg_type=MsgType.Text.name,
                sent_to="tests.mocks.slave",
                sender_bot_id="700",
                pickle=pickle.dumps({"reactions": {"OLD": ("tests.mocks.slave reactor",)}}),
            )
            with patch.object(MsgLog, "save", side_effect=RuntimeError("db failed")):
                with pytest.raises(RuntimeError, match="db failed"):
                    manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=failed_id), old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="800")
            row = MsgLog.get()
            assert (row.master_msg_id_alt, row.sender_bot_id, pickle.loads(bytes(row.pickle))["reactions"]) == (initial_alt, "700", {"OLD": ("tests.mocks.slave reactor",)})

            manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=success_id), old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="900")
            row = MsgLog.get()
            assert (MsgLog.select().count(), row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == (1, "100.10", f"100.{success_id}", "900")
