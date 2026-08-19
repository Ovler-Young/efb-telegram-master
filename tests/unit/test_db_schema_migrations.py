import threading
from types import SimpleNamespace

import pytest
from peewee import IntegrityError, SqliteDatabase

from efb_telegram_master import db as db_module
from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.models import UTC_LEASE_CLOCK, ChatAssoc, HistoryMigrationEntry, MsgLog, SlaveChatInfo, SlaveMessageDelivery, TopicAssoc, database
from efb_telegram_master.persistence.slave_message_delivery_repository import SlaveMessageDeliveryRepository
from tests.support.legacy_outbound_schema import create_legacy_historic_identity_source


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
