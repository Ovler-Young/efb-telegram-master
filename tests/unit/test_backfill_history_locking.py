import threading
from datetime import datetime

from peewee import SqliteDatabase

from efb_telegram_master.core.models import DATABASE_MODELS, HistoryMigrationEntry
from efb_telegram_master.persistence.history_migration_repository import HistoryMigrationRepository


def test_history_migration_replacement_stages_source_before_acquiring_sqlite_writer_lock(tmp_path):
    database_path = tmp_path / "history.db"
    test_database = SqliteDatabase(database_path, pragmas={"journal_mode": "wal", "busy_timeout": 5000}, check_same_thread=False)
    with test_database.bind_ctx(DATABASE_MODELS):
        test_database.connect()
        repository = HistoryMigrationRepository(test_database)
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
