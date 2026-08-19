from types import SimpleNamespace
from unittest.mock import Mock

from peewee import SqliteDatabase

from efb_telegram_master.history_replay import HistoryReplayWorker, history_location_text
from efb_telegram_master.models import DATABASE_MODELS, HistoryMigrationEntry
from efb_telegram_master.persistence.history_migration_repository import HistoryMigrationRepository
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID


def test_empty_history_backfill_enqueues_one_location_notice_in_the_target_topic():
    bot = SimpleNamespace(send_message=Mock())
    worker = HistoryReplayWorker(bot, Mock(), Mock(), Mock(), Mock())
    worker.queue_entries = Mock(return_value=0)
    storage_key = (TelegramChatID(1044903212), TelegramMessageID(456))

    worker._queue_and_process("tests.slave chat", -100500, TelegramMessageID(789), storage_key)

    bot.send_message.assert_called_once_with(
        chat_id=-100500,
        text="This chat was previously linked. History messages are not migrated.",
        disable_notification=True,
        message_thread_id=TelegramMessageID(789),
    )


def test_history_location_text_keeps_supergroup_history_urls():
    assert history_location_text(lambda message: message, (TelegramChatID(-1001234567890), TelegramMessageID(458))).endswith("https://t.me/c/1234567890/458")


def test_history_migration_dispatches_persisted_entries_through_telegram_api():
    test_database = SqliteDatabase(":memory:")
    with test_database.bind_ctx(DATABASE_MODELS):
        test_database.connect()
        manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(), copy_message=Mock()), Mock(), HistoryMigrationRepository(test_database), Mock(), Mock())
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.create(
            slave_chat_id="tests.mocks.slave.chat",
            target_chat_id="12345",
            source_master_msg_id="10.20",
            formatted_text="first\n",
            position=0,
        )
        HistoryMigrationEntry.create(
            slave_chat_id="tests.mocks.slave.chat",
            target_chat_id="12345",
            source_master_msg_id="10.21",
            formatted_text=None,
            position=1,
        )

        assert manager.process_target(manager.history_migrations.get_next_target()) is True

        manager.bot.send_message.assert_called_once_with(chat_id=12345, text="first\n", parse_mode="Markdown", disable_notification=True)
        manager.bot.copy_message.assert_called_once_with(chat_id=12345, from_chat_id=10, message_id=21, disable_notification=True)
        assert HistoryMigrationEntry.select().count() == 0

        failed_entry = HistoryMigrationEntry.create(
            slave_chat_id="tests.mocks.slave.chat",
            target_chat_id="12345",
            source_master_msg_id="10.22",
            formatted_text="failed\n",
            position=0,
        )
        manager.bot.send_message.side_effect = RuntimeError("Telegram failed")

        assert manager.process_target(failed_entry) is False
        assert HistoryMigrationEntry.select().where(HistoryMigrationEntry.id == failed_entry.id).exists()

        manager.bot.send_message.side_effect = None

        assert manager.process_target(failed_entry) is True
        assert not HistoryMigrationEntry.select().where(HistoryMigrationEntry.id == failed_entry.id).exists()
        test_database.close()


def test_pending_history_migrations_keep_the_failed_entry_and_remaining_boundary():
    test_database = SqliteDatabase(":memory:")
    with test_database.bind_ctx(DATABASE_MODELS):
        test_database.connect()
        manager = HistoryReplayWorker(
            SimpleNamespace(send_message=Mock(side_effect=[None, RuntimeError("Telegram failed")]), copy_message=Mock()), Mock(), HistoryMigrationRepository(test_database), Mock(), Mock()
        )
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.insert_many(
            [
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.20", "formatted_text": "first\n", "position": 0},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.21", "formatted_text": "second\n", "position": 1},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.22", "formatted_text": None, "position": 2},
            ]
        ).execute()

        manager.process_pending()

        assert [entry.source_master_msg_id for entry in HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.position)] == ["10.21", "10.22"]
        manager.bot.copy_message.assert_not_called()
        test_database.close()


def test_history_migration_deletes_zero_call_entry_without_queueing():
    manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(), copy_message=Mock()), Mock(), Mock(), Mock(), Mock())
    entry = SimpleNamespace(
        id=8,
        slave_chat_id="tests.mocks.slave.chat",
        target_chat_id="12345",
        message_thread_id=None,
        source_master_msg_id="",
        formatted_text="",
        position=0,
    )
    manager.history_migrations = SimpleNamespace(
        get_entries_page=Mock(side_effect=[[entry], []]),
        delete_entry=Mock(),
    )
    processed = manager.process_target(entry)

    assert processed is True
    manager.bot.send_message.assert_not_called()
    manager.bot.copy_message.assert_not_called()
    manager.history_migrations.delete_entry.assert_called_once_with(8)
    manager.logger.info.assert_any_call("History migration entry %d completed 0 calls", 8)
