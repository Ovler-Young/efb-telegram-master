import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from peewee import SqliteDatabase

from efb_telegram_master.db import DatabaseManager
from efb_telegram_master.models import DATABASE_MODELS, ChatAssoc, HistoryMigrationEntry, SlaveChatInfo, SlaveMessageDelivery, TopicAssoc
from efb_telegram_master.persistence.chat_association_repository import ChatAssociationRepository
from efb_telegram_master.persistence.history_migration_repository import HistoryMigrationRepository
from efb_telegram_master.persistence.schema_migration import DatabaseSchemaMigrator
from efb_telegram_master.persistence.slave_chat_info_repository import SlaveChatInfoRepository
from efb_telegram_master.persistence.slave_message_delivery_repository import SlaveMessageDeliveryRepository
from efb_telegram_master.topic_sync import TopicGroupService
from efb_telegram_master.utils import TelegramChatID, TelegramTopicID


def test_concurrent_association_replacements_leave_one_canonical_row(tmp_path):
    test_database = SqliteDatabase(tmp_path / "association.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    repository = ChatAssociationRepository(test_database)
    with test_database.bind_ctx(DATABASE_MODELS):
        test_database.connect()
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


def test_concurrent_topic_provisioning_creates_one_remote_topic_and_association(tmp_path):
    test_database = SqliteDatabase(tmp_path / "topic-provisioning.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    entered, release_remote_call = threading.Event(), threading.Event()
    remote_calls = []

    def create_forum_topic(**_kwargs):
        remote_calls.append(object())
        entered.set()
        assert release_remote_call.wait(5)
        return SimpleNamespace(message_thread_id=200)

    with test_database.bind_ctx(DATABASE_MODELS):
        test_database.connect()
        try:
            test_database.create_tables([ChatAssoc, TopicAssoc])
            repository = ChatAssociationRepository(test_database)
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


def test_concurrent_slave_chat_info_writes_leave_one_canonical_row(tmp_path):
    test_database = SqliteDatabase(tmp_path / "slave-chat-info.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    repository = SlaveChatInfoRepository(test_database)
    with test_database.bind_ctx(DATABASE_MODELS):
        test_database.connect()
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


def test_concurrent_history_replacements_leave_one_coherent_target(tmp_path):
    test_database = SqliteDatabase(tmp_path / "history.db", pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    repository = HistoryMigrationRepository(test_database)
    with test_database.bind_ctx(DATABASE_MODELS):
        test_database.connect()
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


def test_database_manager_closes_sqlite_when_schema_creation_fails(tmp_path, monkeypatch):
    original_close = SqliteDatabase.close
    closed_databases = []

    def close(instance, *args, **kwargs):
        closed_databases.append(instance)
        return original_close(instance, *args, **kwargs)

    monkeypatch.setattr("efb_telegram_master.persistence.database_initializer.utils.get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(DatabaseSchemaMigrator, "create", lambda _self: (_ for _ in ()).throw(RuntimeError("schema creation failed")))
    monkeypatch.setattr(SqliteDatabase, "close", close)
    with pytest.raises(RuntimeError, match="schema creation failed"):
        DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-failure", config=SimpleNamespace(database={})))

    assert len(closed_databases) == 1
    assert closed_databases[0].is_closed()


def test_database_manager_closes_sqlite_when_post_connect_logging_fails(tmp_path, monkeypatch):
    original_close = SqliteDatabase.close
    logger = Mock()
    closed_databases = []

    def close(instance, *args, **kwargs):
        closed_databases.append(instance)
        return original_close(instance, *args, **kwargs)

    logger.debug.side_effect = lambda message: (_ for _ in ()).throw(RuntimeError("post-connect logging failed")) if message == "Database loaded." else None
    monkeypatch.setattr("efb_telegram_master.persistence.database_initializer.utils.get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(DatabaseManager, "logger", logger)
    monkeypatch.setattr(SqliteDatabase, "close", close)
    with pytest.raises(RuntimeError, match="post-connect logging failed"):
        DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-log-failure", config=SimpleNamespace(database={})))

    assert len(closed_databases) == 1
    assert closed_databases[0].is_closed()


def test_database_manager_does_not_close_sqlite_when_connect_fails(tmp_path, monkeypatch):
    original_close = SqliteDatabase.close
    closed_databases = []

    def connect(instance, *args, **kwargs):
        raise RuntimeError("connect failed")

    def close(instance, *args, **kwargs):
        closed_databases.append(instance)
        return original_close(instance, *args, **kwargs)

    monkeypatch.setattr("efb_telegram_master.persistence.database_initializer.utils.get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(SqliteDatabase, "connect", connect)
    monkeypatch.setattr(SqliteDatabase, "close", close)
    with pytest.raises(RuntimeError, match="connect failed"):
        DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-connect-failure", config=SimpleNamespace(database={})))

    assert closed_databases == []


def test_database_manager_preserves_initialization_error_when_cleanup_logging_fails(tmp_path, monkeypatch):
    logger = Mock()
    close = Mock(side_effect=RuntimeError("cleanup failed"))
    logger.exception.side_effect = RuntimeError("cleanup logging failed")

    monkeypatch.setattr("efb_telegram_master.persistence.database_initializer.utils.get_data_path", lambda _channel_id: tmp_path)
    monkeypatch.setattr(DatabaseManager, "logger", logger)
    monkeypatch.setattr(DatabaseSchemaMigrator, "create", lambda _self: (_ for _ in ()).throw(RuntimeError("schema creation failed")))
    monkeypatch.setattr(SqliteDatabase, "close", close)
    with pytest.raises(RuntimeError, match="schema creation failed"):
        DatabaseManager(SimpleNamespace(channel_id="tests.sqlite-cleanup-logging-failure", config=SimpleNamespace(database={})))

    close.assert_called_once()
    logger.exception.assert_called_once_with("Failed to close database after database initialization failed.")


def test_slave_message_delivery_claim_persists_across_repository_instances(tmp_path):
    test_db = SqliteDatabase(tmp_path / "delivery.db")
    first, restarted = SlaveMessageDeliveryRepository(test_db), SlaveMessageDeliveryRepository(test_db)
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
