"""Run the outbound queue tests on SQLite, and on PostgreSQL when a server is configured."""

import contextlib
import os

import pytest

from efb_telegram_master.db_runtime import postgresql_connection
from efb_telegram_master.outbound import OutboundQueue, create_postgresql_queue_tables
from tests.unit.test_database_safety import postgres_config, postgres_server_config  # noqa: F401

BACKENDS = ["sqlite", "postgresql"] if os.getenv("TEST_POSTGRES_HOST") else ["sqlite"]


@pytest.fixture(params=BACKENDS)
def queue_backend(request):
    """The ``database`` setting the queue is built with: ``None`` for SQLite."""
    if request.param == "sqlite":
        return None
    return request.getfixturevalue("postgres_config")


@pytest.fixture
def make_queue(queue_backend):
    """Build an ``OutboundQueue`` on the backend under test.

    On PostgreSQL every test gets its own schema, so a queue opened again in the
    same test sees the rows of the earlier one, as a reopened SQLite file does.
    """
    if queue_backend is not None:
        connection = postgresql_connection(queue_backend)
        try:
            with connection, connection.cursor() as cursor:
                create_postgresql_queue_tables(cursor.execute)
        finally:
            connection.close()
    created = []

    def make(path, *args, **kwargs):
        queue = OutboundQueue(path, *args, database=queue_backend, **kwargs)
        created.append(queue)
        return queue

    yield make
    for queue in created:
        with contextlib.suppress(Exception):
            queue.close()
