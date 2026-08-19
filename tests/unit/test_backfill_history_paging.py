from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from peewee import SqliteDatabase

from efb_telegram_master.core import utils
from efb_telegram_master.core.models import DATABASE_MODELS, HistoryMigrationEntry, MsgLog
from efb_telegram_master.history.history_replay import HistoryReplayWorker
from efb_telegram_master.persistence.history_migration_repository import HistoryMigrationRepository


def test_pending_history_migrations_send_entries_in_position_order_and_delete_each_success():
    test_database = SqliteDatabase(":memory:")
    with test_database.bind_ctx(DATABASE_MODELS):
        test_database.connect()
        manager = HistoryReplayWorker(SimpleNamespace(send_message=Mock(), copy_message=Mock()), Mock(), HistoryMigrationRepository(test_database), Mock(), Mock())
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

    queued_count = manager.queue_entries("tests.mocks.slave.chat", 12345)

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
