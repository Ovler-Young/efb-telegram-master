import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from peewee import SqliteDatabase

from efb_telegram_master import utils
from efb_telegram_master.history_replay import HistoryReplayWorker, history_location_text
from efb_telegram_master.models import HistoryMigrationEntry, MsgLog, database
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
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(), copy_message=Mock()), Mock(), HistoryMigrationRepository(), Mock(), Mock())
    try:
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
    finally:
        test_database.close()
        database.initialize(original_database)


def test_pending_history_migrations_send_entries_in_position_order_and_delete_each_success():
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(), copy_message=Mock()), Mock(), HistoryMigrationRepository(), Mock(), Mock())
    manager.REPLAY_PAGE_SIZE = 2
    try:
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.insert_many(
            [
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.20", "formatted_text": "first\n", "position": 0},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.21", "formatted_text": "second\n", "position": 1},
                {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.22", "formatted_text": None, "position": 2},
            ]
        ).execute()

        pages = []
        original_get_entries_page = manager.history_migrations.get_entries_page

        def get_entries_page(*args):
            page = original_get_entries_page(*args)
            pages.append(page)
            return page

        with patch.object(manager.history_migrations, "get_entries_page", side_effect=get_entries_page) as get_entries_page_mock:
            manager.process_pending()

        assert [call.kwargs["text"] for call in manager.bot.send_message.call_args_list] == ["first\n", "second\n"]
        manager.bot.copy_message.assert_called_once_with(chat_id=12345, from_chat_id=10, message_id=22, disable_notification=True)
        assert HistoryMigrationEntry.select().count() == 0
        assert [len(page) for page in pages] == [2, 1, 0]
        assert get_entries_page_mock.call_args_list == [
            (("tests.mocks.slave.chat", 12345, None, None, 2), {}),
            (("tests.mocks.slave.chat", 12345, None, (1, 2), 2), {}),
            (("tests.mocks.slave.chat", 12345, None, (2, 3), 2), {}),
        ]
    finally:
        test_database.close()
        database.initialize(original_database)


def test_pending_history_migrations_keep_the_failed_entry_and_remaining_boundary():
    original_database = database.obj
    test_database = SqliteDatabase(":memory:")
    database.initialize(test_database)
    test_database.connect()
    manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(side_effect=[None, RuntimeError("Telegram failed")]), copy_message=Mock()), Mock(), HistoryMigrationRepository(), Mock(), Mock())
    try:
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
    finally:
        test_database.close()
        database.initialize(original_database)


def test_queue_history_migration_entries_persists_pending_rows():
    manager = HistoryReplayWorker(Mock(), Mock(), Mock(), Mock(), Mock())
    base_time = datetime.now()
    text_log = Mock()
    text_log.master_msg_id = "10.20"
    text_log.text = "hello"
    text_log.media_type = "Text"
    text_log.time = base_time
    message_reconstructor = Mock()
    message_reconstructor.build.return_value = SimpleNamespace(author=SimpleNamespace(display_name="author"))
    media_log = Mock()
    media_log.master_msg_id = "10.21"
    media_log.text = ""
    media_log.media_type = "Photo"
    media_log.time = base_time + timedelta(seconds=1)
    manager.SOURCE_PAGE_SIZE = 1
    manager.msglogs.get_recent_message_page.side_effect = [[text_log], [media_log], []]
    manager.message_reconstructor = message_reconstructor

    def replace_entries(_slave_chat_id, _target_chat_id, _thread_id, entries):
        queued_entries.extend(entries)
        return len(queued_entries)

    queued_entries = []
    manager.history_migrations.replace_entries.side_effect = replace_entries

    queued_count = manager.queue_entries(
        "tests.mocks.slave.chat",
        12345,
    )

    assert queued_count == 2
    assert len(queued_entries) == 2
    assert queued_entries[0]["source_master_msg_id"] == "10.20"
    assert queued_entries[0]["formatted_text"] == f"*author* `{base_time.strftime('%Y-%m-%d %H:%M')}`\nhello\n\n"
    assert queued_entries[1]["source_master_msg_id"] == "10.21"
    assert queued_entries[1]["formatted_text"] is None
    assert manager.msglogs.get_recent_message_page.call_args_list == [
        (("tests.mocks.slave.chat", None, 1), {}),
        (("tests.mocks.slave.chat", (base_time, "10.20"), 1), {}),
        (("tests.mocks.slave.chat", (base_time + timedelta(seconds=1), "10.21"), 1), {}),
    ]
    manager.msglogs.get_recent_messages.assert_not_called()


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


def test_history_migration_replacement_stages_source_before_acquiring_sqlite_writer_lock(tmp_path):
    original_database = database.obj
    database_path = tmp_path / "history.db"
    test_database = SqliteDatabase(database_path, pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    database.initialize(test_database)
    test_database.connect()
    repository = HistoryMigrationRepository()
    source_staged = threading.Event()
    continue_source = threading.Event()
    errors = []
    target = {"slave_chat_id": "tests.mocks.slave.chat", "target_chat_id": "12345", "source_master_msg_id": "10.20", "formatted_text": "old", "position": 0}
    replacement = {**target, "source_master_msg_id": "10.21", "formatted_text": "first"}
    try:
        test_database.create_tables([HistoryMigrationEntry])
        HistoryMigrationEntry.create(**target)

        def entries():
            yield replacement
            source_staged.set()
            assert continue_source.wait(5)
            yield {**replacement, "source_master_msg_id": "10.22", "formatted_text": "second", "position": 1}

        def replace() -> None:
            try:
                test_database.connect(reuse_if_open=True)
                repository.replace_entries("tests.mocks.slave.chat", 12345, None, entries())
            except BaseException as error:
                errors.append(error)
            finally:
                if not test_database.is_closed():
                    test_database.close()

        worker = threading.Thread(target=replace)
        worker.start()
        assert source_staged.wait(5)

        concurrent_database = SqliteDatabase(database_path, pragmas={"busy_timeout": 250})
        concurrent_database.connect()
        try:
            concurrent_database.execute_sql(
                "INSERT INTO historymigrationentry (slave_chat_id, target_chat_id, source_master_msg_id, formatted_text, position, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("tests.mocks.slave.chat", "12345", "10.23", "concurrent", 2, datetime.now()),
            )
        finally:
            concurrent_database.close()

        continue_source.set()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors
        assert [entry.source_master_msg_id for entry in HistoryMigrationEntry.select().order_by(HistoryMigrationEntry.position)] == ["10.21", "10.22"]
    finally:
        continue_source.set()
        test_database.close()
        database.initialize(original_database)


def test_get_recent_messages_returns_oldest_first(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    existing = list(MsgLog.select().where(MsgLog.slave_origin_uid == slave_uid))
    for row in existing:
        row.delete_instance()

    base_time = datetime.now()
    for idx in range(3):
        MsgLog.create(
            master_msg_id=f"9000.{idx}",
            master_msg_id_alt=None,
            slave_message_id=f"slave-{idx}",
            text=f"text-{idx}",
            slave_origin_uid=slave_uid,
            slave_member_uid=slave_uid,
            media_type="Text",
            mime=None,
            file_id=None,
            file_unique_id=None,
            msg_type="Text",
            sent_to=channel.channel_id,
            sender_bot_id=None,
            time=base_time + timedelta(seconds=idx),
        )

    recent = channel.msglogs.get_recent_messages(slave_uid, limit=0)
    assert [row.slave_message_id for row in recent] == ["slave-0", "slave-1", "slave-2"]
    first_page = channel.msglogs.get_recent_message_page(slave_uid, None, 2)
    second_page = channel.msglogs.get_recent_message_page(slave_uid, (first_page[-1].time, first_page[-1].master_msg_id), 2)
    assert [row.slave_message_id for row in first_page] == ["slave-0", "slave-1"]
    assert [row.slave_message_id for row in second_page] == ["slave-2"]

    for row in MsgLog.select().where(MsgLog.slave_origin_uid == slave_uid):
        row.delete_instance()
