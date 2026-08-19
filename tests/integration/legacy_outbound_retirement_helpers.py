import uuid
from contextlib import contextmanager

import pytest
from peewee import PostgresqlDatabase


def database_kwargs(config):
    return {key: value for key, value in config.items() if key != "type"}


def new_database(admin_db, config):
    database_name = f"etm_legacy_{uuid.uuid4().hex}"
    admin_db.execute_sql(f'CREATE DATABASE "{database_name}"')
    return database_name, PostgresqlDatabase(database_name, **{key: value for key, value in database_kwargs(config).items() if key != "database"})


def drop_database(admin_db, database_name):
    admin_db.execute_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
    admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')


@contextmanager
def temporary_postgresql_database(config):
    admin_db = PostgresqlDatabase(**database_kwargs(config))
    admin_db.connect()
    admin_db.connection().autocommit = True
    database_name, test_db = new_database(admin_db, config)
    try:
        yield database_name, test_db
    finally:
        if not test_db.is_closed():
            test_db.close()
        drop_database(admin_db, database_name)
        admin_db.close()


@pytest.fixture
def poll_bot():
    """Keep database-retirement tests independent of Telegram polling."""
