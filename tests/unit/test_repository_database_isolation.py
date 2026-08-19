import pytest
from peewee import SqliteDatabase

from efb_telegram_master.core.models import ChatAssoc, HistoryMigrationEntry, TopicAssoc
from efb_telegram_master.persistence.chat_association_repository import ChatAssociationRepository


def test_repository_requires_an_explicit_database():
    with pytest.raises(TypeError):
        ChatAssociationRepository()


def test_repositories_keep_explicit_database_instances_isolated(tmp_path):
    first_database = SqliteDatabase(tmp_path / "first.db")
    second_database = SqliteDatabase(tmp_path / "second.db")
    models = (ChatAssoc, TopicAssoc, HistoryMigrationEntry)
    first = ChatAssociationRepository(first_database)
    second = ChatAssociationRepository(second_database)
    try:
        for current_database in (first_database, second_database):
            current_database.connect()
            with current_database.bind_ctx(models):
                current_database.create_tables(models)

        first.add_chat_assoc("first-master", "first-slave")
        second.add_chat_assoc("second-master", "second-slave")

        assert first.get_chat_assoc(master_uid="first-master") == ["first-slave"]
        assert first.get_chat_assoc(master_uid="second-master") == []
        assert second.get_chat_assoc(master_uid="second-master") == ["second-slave"]
        assert second.get_chat_assoc(master_uid="first-master") == []
    finally:
        first_database.close()
        second_database.close()
