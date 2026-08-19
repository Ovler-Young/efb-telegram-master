import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ehforwarderbot import MsgType
from ehforwarderbot.types import MessageID
from peewee import IndexMetadata, IntegrityError, Model, PostgresqlDatabase, SqliteDatabase
from prometheus_client import generate_latest

from efb_telegram_master import db as db_module
from efb_telegram_master import utils
from efb_telegram_master.chat_association_repository import ChatAssociationRepository
from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.history_migration_repository import HistoryMigrationRepository
from efb_telegram_master.legacy_outbound_retirement import LegacyOutboundRetirement
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.models import UTC_LEASE_CLOCK, ChatAssoc, HistoryMigrationEntry, MsgLog, MsgLogIngestionScan, SlaveChatInfo, SlaveMessageDelivery, TopicAssoc, database
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.msglog_repository import MsgLogRepository
from efb_telegram_master.outbound_types import SendReceipt
from efb_telegram_master.slave_chat_info_repository import SlaveChatInfoRepository
from efb_telegram_master.slave_message import SlaveMessageService
from efb_telegram_master.slave_message_delivery_repository import SlaveMessageDeliveryRepository
from efb_telegram_master.topic_sync import TopicGroupService
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID, TelegramTopicID
from tests.support.legacy_outbound_schema import create_legacy_historic_identity_source, create_legacy_outbound_schema


def test_database_manager_repositories_remain_bound_to_their_own_database(tmp_path, monkeypatch):
    original_database = database.obj
    (tmp_path / "tests.first").mkdir()
    (tmp_path / "tests.second").mkdir()
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda channel_id: tmp_path / channel_id)
    first_manager = DatabaseManager(SimpleNamespace(channel_id="tests.first", config={}))
    second_manager = DatabaseManager(SimpleNamespace(channel_id="tests.second", config={}))
    try:
        first_manager.chat_associations.add_chat_assoc("master-one", "slave-one")
        second_manager.chat_associations.add_chat_assoc("master-two", "slave-two")

        assert first_manager.chat_associations.get_chat_assoc(master_uid="master-one") == ["slave-one"]
        assert first_manager.chat_associations.get_chat_assoc(master_uid="master-two") == []
        assert second_manager.chat_associations.get_chat_assoc(master_uid="master-one") == []
        assert second_manager.chat_associations.get_chat_assoc(master_uid="master-two") == ["slave-two"]
    finally:
        second_manager.stop_worker()
        first_manager.stop_worker()
        database.initialize(original_database)


def test_startup_migrates_pre_migration_four_sqlite_rows_without_loss(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        raw_db.execute_sql(
            "CREATE TABLE msglog (master_msg_id TEXT PRIMARY KEY, master_msg_id_alt TEXT, slave_message_id TEXT NOT NULL, "
            "text TEXT NOT NULL, slave_origin_uid TEXT NOT NULL, slave_origin_display_name TEXT, slave_member_uid TEXT, "
            "slave_member_display_name TEXT, media_type TEXT, mime TEXT, file_id TEXT, file_unique_id TEXT, msg_type TEXT NOT NULL, "
            "pickle BLOB, sent_to TEXT NOT NULL, time DATETIME)"
        )
        raw_db.execute_sql(
            "CREATE TABLE slavechatinfo (id INTEGER PRIMARY KEY, slave_channel_id TEXT NOT NULL, slave_channel_emoji TEXT NOT NULL, slave_chat_uid TEXT NOT NULL, "
            "slave_chat_name TEXT NOT NULL, slave_chat_alias TEXT, slave_chat_type TEXT NOT NULL)"
        )
        raw_db.execute_sql(
            "INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, msg_type, sent_to) VALUES (?, ?, ?, ?, ?, ?)",
            ("100.1", "legacy-message", "legacy text", "tests.slave chat", "Text", "tests.master"),
        )
        raw_db.execute_sql(
            "INSERT INTO slavechatinfo (slave_channel_id, slave_channel_emoji, slave_chat_uid, slave_chat_name, slave_chat_type) VALUES (?, ?, ?, ?, ?)",
            ("tests.slave", "x", "tests.slave chat", "Legacy chat", "group"),
        )
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.pre-migration-four", config={}))
    try:
        msglog_columns = {column.name for column in database.get_columns("msglog")}
        slave_info_columns = {column.name for column in database.get_columns("slavechatinfo")}
        msglog = MsgLog.get_by_id("100.1")
        slave_info = SlaveChatInfo.get(SlaveChatInfo.slave_chat_uid == "tests.slave chat")
    finally:
        manager.stop_worker()
        database.initialize(original_database)

    assert {"file_id", "media_type", "mime", "master_msg_id_alt", "pickle", "file_unique_id", "sender_bot_id", "provenance"}.issubset(msglog_columns)
    assert {"pickle", "slave_chat_group_id"}.issubset(slave_info_columns)
    assert (msglog.slave_message_id, msglog.text, msglog.provenance) == ("legacy-message", "legacy text", "live")
    assert (slave_info.slave_chat_name, slave_info.slave_chat_group_id) == ("Legacy chat", None)


def test_historic_schema_migration_serializes_sqlite_startups(tmp_path):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        raw_db.execute_sql(
            "CREATE TABLE msglog (master_msg_id TEXT PRIMARY KEY, slave_message_id TEXT NOT NULL, text TEXT NOT NULL, slave_origin_uid TEXT NOT NULL, msg_type TEXT NOT NULL, sent_to TEXT NOT NULL)"
        )
    finally:
        raw_db.close()
    errors = []

    def migrate() -> None:
        connection = SqliteDatabase(database_path, pragmas={"busy_timeout": 5000})
        try:
            connection.connect()
            DatabaseManager._ensure_historic_schema_columns(connection)
        except BaseException as error:
            errors.append(error)
        finally:
            connection.close()

    first, second = threading.Thread(target=migrate), threading.Thread(target=migrate)
    first.start()
    second.start()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    check_db = SqliteDatabase(database_path)
    check_db.connect()
    try:
        assert {"provenance", "time"}.issubset({column.name for column in check_db.get_columns("msglog")})
        assert DatabaseManager._MSGLOG_REPLAY_SOURCE_INDEX in {index.name for index in check_db.get_indexes("msglog")}
    finally:
        check_db.close()


def test_association_schema_upgrade_deduplicates_rows_and_enforces_canonical_identity(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        create_legacy_historic_identity_source(raw_db)
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.association-upgrade", config={}))
    try:
        assert [(row.master_uid, row.slave_uid) for row in ChatAssoc.select()] == [("master-new", "slave-a")]
        assert [(row.topic_chat_id, row.message_thread_id, row.slave_uid) for row in TopicAssoc.select()] == [("101", "201", "slave-b")]
        assert [row.source_master_msg_id for row in HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.id)] == ["10.2", "10.4"]
        assert {
            DatabaseManager._CHAT_ASSOC_SLAVE_INDEX,
            DatabaseManager._TOPIC_ASSOC_SLAVE_INDEX,
            DatabaseManager._TOPIC_ASSOC_TOPIC_THREAD_INDEX,
        }.issubset({index.name for index in database.get_indexes("topicassoc")} | {index.name for index in database.get_indexes("chatassoc")})
        assert {
            DatabaseManager._HISTORY_TARGET_POSITION_WITHOUT_THREAD_INDEX,
            DatabaseManager._HISTORY_TARGET_POSITION_WITH_THREAD_INDEX,
        }.issubset({index.name for index in database.get_indexes("historymigrationentry")})
        with pytest.raises(IntegrityError):
            ChatAssoc.create(master_uid="master-other", slave_uid="slave-a")
        with pytest.raises(IntegrityError):
            TopicAssoc.create(topic_chat_id="101", message_thread_id="201", slave_uid="slave-c")
        with pytest.raises(IntegrityError):
            HistoryMigrationEntry.create(slave_chat_id="slave-a", target_chat_id="100", source_master_msg_id="10.5", position=0)
        with pytest.raises(IntegrityError):
            HistoryMigrationEntry.create(slave_chat_id="slave-a", target_chat_id="100", message_thread_id="200", source_master_msg_id="10.6", position=0)
    finally:
        manager.stop_worker()
        database.initialize(original_database)


def test_sqlite_import_snapshot_canonicalizes_legacy_historic_identities_without_mutating_source(tmp_path):
    source_db = SqliteDatabase(tmp_path / "tgdata.db")
    models = (ChatAssoc, TopicAssoc, HistoryMigrationEntry)
    source_db.connect()
    try:
        create_legacy_historic_identity_source(source_db)
        with source_db.bind_ctx(models):
            snapshot = DatabaseManager._sqlite_source_snapshot(source_db, models)

        rows_by_model = {projection.model: [dict(zip(projection.column_names, row)) for row in projection.rows] for projection in snapshot.projections}
        assert rows_by_model[ChatAssoc] == [{"id": 2, "master_uid": "master-new", "slave_uid": "slave-a"}]
        assert rows_by_model[TopicAssoc] == [{"id": 3, "topic_chat_id": "101", "message_thread_id": "201", "slave_uid": "slave-b"}]
        assert [row["source_master_msg_id"] for row in rows_by_model[HistoryMigrationEntry]] == ["10.2", "10.4"]
        assert source_db.execute_sql("SELECT COUNT(*) FROM chatassoc").fetchone() == (2,)
        assert source_db.execute_sql("SELECT COUNT(*) FROM topicassoc").fetchone() == (3,)
        assert source_db.execute_sql("SELECT COUNT(*) FROM historymigrationentry").fetchone() == (4,)
    finally:
        source_db.close()


def test_sqlite_import_snapshot_omits_missing_ingestion_rescan_requested_column(tmp_path):
    source_db = SqliteDatabase(tmp_path / "tgdata.db")
    source_db.connect()
    try:
        source_db.execute_sql(
            "CREATE TABLE msglogingestionscan ("
            "id INTEGER PRIMARY KEY, source_chat_id TEXT NOT NULL UNIQUE, scan_boundary INTEGER NOT NULL, cursor INTEGER NOT NULL, "
            "existing_streak INTEGER NOT NULL DEFAULT 0, scanned_count INTEGER NOT NULL DEFAULT 0, inserted_count INTEGER NOT NULL DEFAULT 0, "
            "existing_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires_at DATETIME, "
            "status TEXT NOT NULL DEFAULT 'pending', error TEXT, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        source_db.execute_sql(
            "INSERT INTO msglogingestionscan (source_chat_id, scan_boundary, cursor, existing_streak, scanned_count, inserted_count, "
            "existing_count, skipped_count, lease_owner, status, error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("100", 500, 0, 500, 500, 5, 495, 0, None, "complete", None, "2020-01-02 03:04:05", "2021-02-03 04:05:06"),
        )
        with source_db.bind_ctx([MsgLogIngestionScan]):
            snapshot = DatabaseManager._sqlite_source_snapshot(source_db, (MsgLogIngestionScan,))
    finally:
        source_db.close()

    projection = snapshot.projections[0]
    row = dict(zip(projection.column_names, projection.rows[0]))
    assert projection.model is MsgLogIngestionScan
    assert set(projection.column_names) == {field.column_name for field in MsgLogIngestionScan._meta.sorted_fields} - {"rescan_requested"}
    assert {key: row[key] for key in row if key not in {"created_at", "updated_at"}} == {
        "id": 1,
        "source_chat_id": "100",
        "scan_boundary": 500,
        "cursor": 0,
        "existing_streak": 500,
        "scanned_count": 500,
        "inserted_count": 5,
        "existing_count": 495,
        "skipped_count": 0,
        "lease_owner": None,
        "lease_expires_at": None,
        "lease_clock": None,
        "status": "complete",
        "error": None,
    }
    assert row["created_at"] == datetime(2020, 1, 2, 3, 4, 5)
    assert row["updated_at"] == datetime(2021, 2, 3, 4, 5, 6)


def test_sqlite_import_snapshot_injects_missing_delivery_lease_clock(tmp_path):
    source_db = SqliteDatabase(tmp_path / "tgdata.db")
    source_db.connect()
    try:
        source_db.execute_sql(
            "CREATE TABLE slavemessagedelivery ("
            "id INTEGER PRIMARY KEY, slave_origin_uid TEXT NOT NULL, slave_message_id TEXT NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'pending', lease_expires_at DATETIME, owner_token TEXT, "
            "UNIQUE(slave_origin_uid, slave_message_id))"
        )
        source_db.execute_sql(
            "INSERT INTO slavemessagedelivery (slave_origin_uid, slave_message_id, state, lease_expires_at, owner_token) VALUES (?, ?, ?, ?, ?)",
            ("tests.slave chat", "message", "pending", "2020-01-02 03:04:05", "owner"),
        )
        with source_db.bind_ctx([SlaveMessageDelivery]):
            snapshot = DatabaseManager._sqlite_source_snapshot(source_db, (SlaveMessageDelivery,))
    finally:
        source_db.close()

    projection = snapshot.projections[0]
    row = dict(zip(projection.column_names, projection.rows[0]))
    assert projection.model is SlaveMessageDelivery
    assert set(projection.column_names) == {field.column_name for field in SlaveMessageDelivery._meta.sorted_fields}
    assert row == {
        "id": 1,
        "slave_origin_uid": "tests.slave chat",
        "slave_message_id": "message",
        "state": "pending",
        "lease_expires_at": datetime(2020, 1, 2, 3, 4, 5),
        "owner_token": "owner",
        "lease_clock": None,
    }


def test_concurrent_association_replacements_leave_one_canonical_row(tmp_path):
    original_database = database.obj
    test_database = SqliteDatabase(tmp_path / "association.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    database.initialize(test_database)
    test_database.connect()
    repository = ChatAssociationRepository()
    try:
        test_database.create_tables([ChatAssoc, TopicAssoc, HistoryMigrationEntry])
        HistoryMigrationEntry.create(slave_chat_id="slave-a", target_chat_id="100", source_master_msg_id="100.1", position=0)

        def replace(index):
            test_database.connect(reuse_if_open=True)
            try:
                repository.add_chat_assoc(f"master-{index}", "slave-a")
                repository.add_topic_assoc(TelegramChatID(100 + index), TelegramTopicID(200 + index), "slave-a")
            finally:
                test_database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(replace, range(2)))

        assert ChatAssoc.select().where(ChatAssoc.slave_uid == "slave-a").count() == 1
        assert TopicAssoc.select().where(TopicAssoc.slave_uid == "slave-a").count() == 1
        assert TopicAssoc.select().count() == 1
        assert HistoryMigrationEntry.select().where(HistoryMigrationEntry.slave_chat_id == "slave-a").count() == 0
    finally:
        if not test_database.is_closed():
            test_database.close()
        database.initialize(original_database)


def test_concurrent_topic_provisioning_creates_one_remote_topic_and_association(tmp_path):
    original_database = database.obj
    test_database = SqliteDatabase(tmp_path / "topic-provisioning.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    database.initialize(test_database)
    test_database.connect()
    entered, release_remote_call = threading.Event(), threading.Event()
    remote_calls = []

    def create_forum_topic(**_kwargs):
        remote_calls.append(object())
        entered.set()
        assert release_remote_call.wait(5)
        return SimpleNamespace(message_thread_id=200)

    try:
        test_database.create_tables([ChatAssoc, TopicAssoc])
        repository = ChatAssociationRepository()
        service_kwargs = (
            None,
            SimpleNamespace(create_forum_topic=create_forum_topic),
            repository,
            SimpleNamespace(get_chat=lambda *_args, **_kwargs: SimpleNamespace(chat_title="Chat")),
            SimpleNamespace(schedule_for_association=lambda _chat_id: None),
            "tests.channel",
            lambda value: value,
            lambda one, _many, _count: one,
            Mock(),
        )
        first, second = TopicGroupService(*service_kwargs), TopicGroupService(*service_kwargs)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_result = executor.submit(first.create_topic, "tests.slave chat", TelegramChatID(100))
            assert entered.wait(5)
            second_result = executor.submit(second.create_topic, "tests.slave chat", TelegramChatID(100))
            release_remote_call.set()
            assert first_result.result(5) == TelegramTopicID(200)
            assert second_result.result(5) == TelegramTopicID(200)
        assert len(remote_calls) == 1
        assert TopicAssoc.select().where(TopicAssoc.slave_uid == "tests.slave chat").count() == 1
    finally:
        if not test_database.is_closed():
            test_database.close()
        database.initialize(original_database)


def test_slave_chat_info_schema_upgrade_deduplicates_null_and_group_identities(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        raw_db.execute_sql(
            "CREATE TABLE slavechatinfo (id INTEGER PRIMARY KEY, slave_channel_id TEXT NOT NULL, slave_channel_emoji TEXT NOT NULL, "
            "slave_chat_uid TEXT NOT NULL, slave_chat_group_id TEXT, slave_chat_name TEXT NOT NULL, slave_chat_alias TEXT, "
            "slave_chat_type TEXT NOT NULL, pickle BLOB)"
        )
        raw_db.execute_sql(
            "INSERT INTO slavechatinfo VALUES "
            "(1, 'tests.slave', 'a', 'chat', NULL, 'old', NULL, 'group', NULL), "
            "(2, 'tests.slave', 'b', 'chat', NULL, 'new', NULL, 'group', NULL), "
            "(3, 'tests.slave', 'c', 'chat', 'group', 'old group', NULL, 'group', NULL), "
            "(4, 'tests.slave', 'd', 'chat', 'group', 'new group', NULL, 'group', NULL)"
        )
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.slave-chat-info-upgrade", config={}))
    try:
        assert [(row.slave_chat_group_id, row.slave_chat_name) for row in SlaveChatInfo.select().order_by(SlaveChatInfo.id)] == [(None, "new"), ("group", "new group")]
        indexes = {index.name for index in database.get_indexes("slavechatinfo")}
        assert {
            DatabaseManager._SLAVE_CHAT_INFO_IDENTITY_WITHOUT_GROUP_INDEX,
            DatabaseManager._SLAVE_CHAT_INFO_IDENTITY_WITH_GROUP_INDEX,
        }.issubset(indexes)
        with pytest.raises(IntegrityError):
            SlaveChatInfo.create(slave_channel_id="tests.slave", slave_channel_emoji="x", slave_chat_uid="chat", slave_chat_name="duplicate", slave_chat_type="group")
    finally:
        manager.stop_worker()
        database.initialize(original_database)


def test_concurrent_slave_chat_info_writes_leave_one_canonical_row(tmp_path):
    original_database = database.obj
    test_database = SqliteDatabase(tmp_path / "slave-chat-info.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    database.initialize(test_database)
    test_database.connect()
    repository = SlaveChatInfoRepository()
    try:
        test_database.create_tables([SlaveChatInfo])

        def write(index):
            test_database.connect(reuse_if_open=True)
            try:
                return repository.set_slave_chat_info(
                    SimpleNamespace(module_id="tests.slave", channel_emoji="x", uid="chat", chat=None, name=f"chat-{index}", alias=None, chat_type_name="group", pickle=None)
                )
            finally:
                test_database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(write, range(2)))
        assert SlaveChatInfo.select().where((SlaveChatInfo.slave_channel_id == "tests.slave") & (SlaveChatInfo.slave_chat_uid == "chat")).count() == 1
    finally:
        if not test_database.is_closed():
            test_database.close()
        database.initialize(original_database)


def test_concurrent_history_replacements_leave_one_coherent_target(tmp_path):
    original_database = database.obj
    test_database = SqliteDatabase(tmp_path / "history.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    database.initialize(test_database)
    test_database.connect()
    repository = HistoryMigrationRepository()
    try:
        test_database.create_tables([HistoryMigrationEntry])

        def replace(source_prefix):
            test_database.connect(reuse_if_open=True)
            try:
                return repository.replace_entries(
                    "slave-a",
                    100,
                    None,
                    [
                        {"slave_chat_id": "slave-a", "target_chat_id": "100", "source_master_msg_id": f"{source_prefix}.1", "position": 0},
                        {"slave_chat_id": "slave-a", "target_chat_id": "100", "source_master_msg_id": f"{source_prefix}.2", "position": 1},
                    ],
                )
            finally:
                test_database.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(replace, ("10", "20"))) == [2, 2]

        source_ids = [row.source_master_msg_id for row in HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.position)]
        assert source_ids in (["10.1", "10.2"], ["20.1", "20.2"])
    finally:
        if not test_database.is_closed():
            test_database.close()
        database.initialize(original_database)


def test_database_method_metrics_record_bounded_public_operation_labels(channel):
    metrics = Metrics()
    channel.db.set_metrics(metrics)

    assert channel.chat_associations.get_chat_assoc(master_uid="metrics-master") == []
    with pytest.raises(ValueError, match="Only one parameter"):
        channel.msglogs.get_msg_log()
    assert channel.history_migrations.get_entries_page("metrics-slave", 12345, None, None, 1) == []
    assert channel.msglogs.get_recent_message_page("metrics-slave", None, 1) == []

    rendered = generate_latest(metrics.registry).decode()

    assert 'etm_database_method_duration_seconds_count{method="get_chat_assoc"} 1.0' in rendered
    assert 'etm_database_method_failures_total{method="get_msg_log"} 1.0' in rendered
    assert 'etm_database_method_duration_seconds_count{method="get_history_migration_entry_page"} 1.0' in rendered
    assert 'etm_database_method_duration_seconds_count{method="get_recent_msglog_page"} 1.0' in rendered
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
    monkeypatch.setattr(LegacyOutboundRetirement, "retire_tables", lambda _self: (_ for _ in ()).throw(RuntimeError("retirement failed")))
    try:
        with pytest.raises(RuntimeError, match="retirement failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.postgresql-failure", config={"database": {"type": "postgresql"}}))

        pool.connect.assert_called_once_with()
        pool.stop.assert_called_once_with()
        pool.close.assert_called_once_with()
    finally:
        pooled_database.stop()
        database.initialize(original_database)


def test_startup_preserves_non_empty_legacy_outbound_tables_and_retires_empty_ones(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        workflow, task = create_legacy_outbound_schema(raw_db)
        workflow.create()
        task.create(
            source_key="source",
            target_chat_id=1,
            operation="send_message",
            payload="secret",
            workflow_id=1,
        )
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    with pytest.raises(RuntimeError, match="automatic replay is disabled"):
        DatabaseManager(SimpleNamespace(channel_id="tests.legacy", config={}))
    assert database.is_closed()
    preserved_db = SqliteDatabase(database_path)
    preserved_db.connect()
    try:
        assert set(LegacyOutboundRetirement.TABLES).issubset(preserved_db.get_tables())
        assert preserved_db.execute_sql("SELECT COUNT(*) FROM outboundworkflow").fetchone()[0] == 1
        assert preserved_db.execute_sql("SELECT COUNT(*) FROM outboundtask").fetchone()[0] == 1
        preserved_db.execute_sql("DELETE FROM outboundtask")
        preserved_db.execute_sql("DELETE FROM outboundworkflow")
    finally:
        preserved_db.close()

    manager = None
    try:
        manager = DatabaseManager(SimpleNamespace(channel_id="tests.legacy", config={}))
        table_names = set(database.get_tables())
        assert not set(LegacyOutboundRetirement.TABLES) & table_names
        assert {"chatassoc", "msglog", "historymigrationentry", "msglogingestionscan"}.issubset(table_names)
    finally:
        if manager is not None:
            manager.stop_worker()
        database.initialize(original_database)


def test_legacy_outbound_retirement_directly_retires_empty_schema(tmp_path):
    raw_db = SqliteDatabase(tmp_path / "tgdata.db")
    raw_db.connect()
    try:
        create_legacy_outbound_schema(raw_db)

        LegacyOutboundRetirement(raw_db).retire_tables()

        assert not set(LegacyOutboundRetirement.TABLES) & set(raw_db.get_tables())
    finally:
        raw_db.close()


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
    assert LegacyOutboundRetirement._default_category(backend, table_name, column_name, data_type, primary_key, default) == expected


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

    assert set(LegacyOutboundRetirement.task_indexes(backend)) == set(LegacyOutboundRetirement._TASK_INDEXES)

    monkeypatch.setattr(backend, "get_indexes", lambda _table_name: (*introspected_indexes, IndexMetadata("outboundtask_unexpected", "", ["state"], False, "outboundtask")))

    assert set(LegacyOutboundRetirement.task_indexes(backend)) != set(LegacyOutboundRetirement._TASK_INDEXES)


def test_startup_aborts_when_legacy_outbound_table_retirement_fails(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        create_legacy_outbound_schema(raw_db)
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)

    class LegacyOutboundTable(Model):
        class Meta:
            database = database
            table_name = "outboundworkflow"

    monkeypatch.setattr(LegacyOutboundRetirement, "_table_model", staticmethod(lambda _table_name, _database: LegacyOutboundTable))
    monkeypatch.setattr(SqliteDatabase, "drop_tables", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("drop failed")))

    try:
        with pytest.raises(RuntimeError, match="drop failed"):
            DatabaseManager(SimpleNamespace(channel_id="tests.legacy-drop", config={}))
    finally:
        if not database.is_closed():
            database.close()
        database.initialize(original_database)


@pytest.mark.parametrize("legacy_table", LegacyOutboundRetirement.TABLES)
def test_startup_aborts_without_dropping_partial_historical_schema(tmp_path, monkeypatch, legacy_table):
    raw_db = SqliteDatabase(tmp_path / "tgdata.db")
    raw_db.connect()
    try:
        create_legacy_outbound_schema(raw_db, tables=(legacy_table,))
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
        create_legacy_outbound_schema(raw_db, tables=("outboundtask",))
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
        workflow, task = create_legacy_outbound_schema(raw_db)
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
            assert set(LegacyOutboundRetirement.TABLES).issubset(collision_db.get_tables())
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
        workflow, task = create_legacy_outbound_schema(raw_db)
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
            assert set(LegacyOutboundRetirement.TABLES).issubset(collision_db.get_tables())
            assert collision_db.execute_sql("SELECT COUNT(*) FROM outboundworkflow").fetchone()[0] == 0
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
        workflow, task = create_legacy_outbound_schema(raw_db)
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
            assert set(LegacyOutboundRetirement.TABLES).issubset(restored_db.get_tables())
            assert restored_db.execute_sql("SELECT COUNT(*) FROM outboundworkflow").fetchone()[0] == 0
            assert restored_db.execute_sql("SELECT COUNT(*) FROM outboundtask").fetchone()[0] == 0
            assert LegacyOutboundRetirement.schema_error(restored_db, "outboundtask") is None
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

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        processor.dispatch_message(message, "", None, 123456, None)

        stored = repository.get_msg_log(master_msg_id="123456.654321")
        assert stored is not None and stored.sender_bot_id == "777"


def test_slave_message_delivery_claim_persists_across_repository_instances(tmp_path):
    test_db = SqliteDatabase(tmp_path / "delivery.db")
    first, restarted = SlaveMessageDeliveryRepository(), SlaveMessageDeliveryRepository()
    with test_db.bind_ctx([SlaveMessageDelivery]):
        test_db.connect()
        try:
            test_db.create_tables([SlaveMessageDelivery])
            first_token = first.claim("tests.slave chat", "message")
            assert first_token is not None
            assert restarted.claim("tests.slave chat", "message") is None
            assert restarted.complete("tests.slave chat", "message", first_token)
            assert first.claim("tests.slave chat", "message", lease_seconds=0) is None

            stale_token = first.claim("tests.slave chat", "retry", lease_seconds=-1)
            assert stale_token is not None
            assert first.renew("tests.slave chat", "retry", stale_token)
            assert restarted.claim("tests.slave chat", "retry") is None
            assert not restarted.renew("tests.slave chat", "retry", "wrong-owner")
            assert first.release("tests.slave chat", "retry", stale_token)
            stale_token = first.claim("tests.slave chat", "retry", lease_seconds=-1)
            assert stale_token is not None
            replacement_token = restarted.claim("tests.slave chat", "retry")
            assert replacement_token is not None and replacement_token != stale_token
            assert not first.complete("tests.slave chat", "retry", stale_token)
            assert not first.release("tests.slave chat", "retry", stale_token)
            assert restarted.complete("tests.slave chat", "retry", replacement_token)
        finally:
            test_db.close()


def test_slave_message_delivery_schema_upgrade_adds_owner_token(tmp_path, monkeypatch):
    database_path = tmp_path / "tgdata.db"
    raw_db = SqliteDatabase(database_path)
    raw_db.connect()
    try:
        raw_db.execute_sql(
            "CREATE TABLE slavemessagedelivery (id INTEGER PRIMARY KEY, slave_origin_uid TEXT NOT NULL, slave_message_id TEXT NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'pending', lease_expires_at DATETIME, UNIQUE(slave_origin_uid, slave_message_id))"
        )
        raw_db.execute_sql(
            "INSERT INTO slavemessagedelivery (slave_origin_uid, slave_message_id, state) VALUES ('tests.slave chat', 'message', 'delivered'), ('tests.slave chat', 'pending-message', 'pending')"
        )
    finally:
        raw_db.close()

    original_database = database.obj
    monkeypatch.setattr(db_module.utils, "get_data_path", lambda _channel_id: tmp_path)
    manager = DatabaseManager(SimpleNamespace(channel_id="tests.delivery-owner-token-upgrade", config={}))
    try:
        row = SlaveMessageDelivery.get((SlaveMessageDelivery.slave_origin_uid == "tests.slave chat") & (SlaveMessageDelivery.slave_message_id == "message"))
        assert row.state == "delivered"
        assert row.owner_token is None
        assert row.lease_clock is None
        assert {"owner_token", "lease_clock"}.issubset({column.name for column in database.get_columns("slavemessagedelivery")})
        owner_token = SlaveMessageDeliveryRepository().claim("tests.slave chat", "pending-message")
        assert owner_token is not None
        pending_row = SlaveMessageDelivery.get((SlaveMessageDelivery.slave_origin_uid == "tests.slave chat") & (SlaveMessageDelivery.slave_message_id == "pending-message"))
        assert pending_row.owner_token == owner_token
        assert pending_row.lease_clock == UTC_LEASE_CLOCK
    finally:
        manager.stop_worker()
        database.initialize(original_database)


def test_message_reconstructor_restores_sender_bot_id(channel, slave):
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
    assert row is not None and channel.message_reconstructor.build(row).sender_bot_id == "888"
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
            with patch.object(MsgLog, "insert", wraps=MsgLog.insert) as insert:
                with patch("peewee.ModelInsert.execute", side_effect=RuntimeError("db failed")) as execute:
                    with pytest.raises(RuntimeError, match="db failed"):
                        manager.add_or_update_message_log(
                            message, SimpleNamespace(chat_id=100, message_id=failed_id), old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="800"
                        )
            assert insert.call_count == 1
            assert execute.call_count == 1
            row = MsgLog.get()
            assert (row.master_msg_id_alt, row.sender_bot_id, pickle.loads(bytes(row.pickle))["reactions"]) == (initial_alt, "700", {"OLD": ("tests.mocks.slave reactor",)})

            manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=success_id), old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="900")
            row = MsgLog.get()
            assert (MsgLog.select().count(), row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == (1, "100.10", f"100.{success_id}", "900")
