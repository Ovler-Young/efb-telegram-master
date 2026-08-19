import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from peewee import PostgresqlDatabase

from efb_telegram_master.models import DATABASE_MODELS, MsgLog, MsgLogIngestionScan
from efb_telegram_master.msglog_ingestion import MsgLogIngestionService
from efb_telegram_master.persistence.msglog_ingestion_repository import MsgLogIngestionRepository
from tests.unit.msglog_ingestion_support import FakeChatAssociations, FakeDatabase, FakeMTProto, sqlite_ingestion_database, topic_message

EXPECTED_MTPROTO_TIMES = [
    ("100.1", datetime(2026, 8, 4, 8, 15), None),
    ("100.2", datetime(2026, 8, 4, 12, 30), None),
    ("100.3", datetime(2026, 8, 4, 12, 30), None),
]


def test_ingestion_descends_in_hundred_id_batches_and_stores_mapped_messages():
    db = FakeDatabase()
    ordinary_reply = SimpleNamespace(
        id=4,
        message="ordinary reply",
        reply_to=SimpleNamespace(forum_topic=False, reply_to_top_id=None, reply_to_msg_id=10),
        action=None,
        media=None,
    )
    mtproto = FakeMTProto(
        {
            205: topic_message(205),
            104: topic_message(104, topic_root=True),
            4: ordinary_reply,
        }
    )

    asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert [ids for _, ids in mtproto.calls] == [
        list(range(205, 105, -1)),
        list(range(105, 5, -1)),
        list(range(5, 0, -1)),
    ]
    accepted = [entry for entry in db.persisted if entry[1] == "eligible"]
    assert [(entry[0], entry[2]) for entry in accepted] == [
        (205, "tests.slave"),
        (104, "tests.slave"),
    ]
    ordinary_reply_entry = next(entry for entry in db.persisted if entry[0] == 4)
    assert ordinary_reply_entry[1:] == ("not-topic", None, None)
    assert db.scan.status == "complete"


def test_ingestion_skips_are_neutral_and_existing_streak_completes_at_500():
    db = FakeDatabase(scan_boundary=503)
    db.persist_item = lambda scan, **kwargs: _five_hundred_rule(db, scan, **kwargs)
    mtproto = FakeMTProto(
        {message_id: topic_message(message_id) for message_id in range(1, 504)},
        scan_ceiling=503,
    )
    mtproto.messages[502] = SimpleNamespace(id=502, message="service", action=object(), reply_to=None)

    asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert db.scan.status == "complete"
    assert db.scan.cursor == 2
    assert db.scan.existing_streak == 500


def _five_hundred_rule(db, scan, *, source_message_id, classification, **_kwargs):
    db.persisted.append((source_message_id, classification, None, None))
    scan.cursor = source_message_id - 1
    if classification == "eligible":
        scan.existing_streak += 1
        return "existing"
    return "skipped"


def test_ingestion_collapses_media_to_generic_copyable_content():
    db = FakeDatabase(scan_boundary=1)
    media = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4", attributes=[]))
    mtproto = FakeMTProto({1: topic_message(1, media=media)}, scan_ceiling=1)

    asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    stored = db.persisted[0][3]
    assert stored.media_type == "Document"
    assert stored.msg_type == "File"
    assert stored.mime == "video/mp4"


def test_ingestion_persists_mtproto_times_as_utc_naive_datetimes(tmp_path):
    with sqlite_ingestion_database(tmp_path / "msglog.db") as (test_db, ingestion):
        mtproto = FakeMTProto(
            {
                3: topic_message(3, date=datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc)),
                2: topic_message(2, date=datetime(2026, 8, 4, 18, tzinfo=timezone(timedelta(hours=5, minutes=30)))),
                1: topic_message(1, date=datetime(2026, 8, 4, 8, 15)),
            },
            scan_ceiling=3,
        )
        service = MsgLogIngestionService(ingestion, FakeChatAssociations({10: "tests.slave target"}), mtproto)
        asyncio.run(service.run(100, lease_owner="worker-a"))
        test_db.close()
        test_db.connect()
        rows = list(MsgLog.select().order_by(MsgLog.time, MsgLog.master_msg_id))

        assert [(row.master_msg_id, row.time, row.time.tzinfo) for row in rows] == EXPECTED_MTPROTO_TIMES


@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_HOST"), reason="PostgreSQL test environment is not configured")
def test_postgresql_ingestion_persists_mtproto_times_as_utc_naive_datetimes():
    connection_kwargs = {
        "database": os.environ["TEST_POSTGRES_DB"],
        "host": os.environ["TEST_POSTGRES_HOST"],
        "port": int(os.environ["TEST_POSTGRES_PORT"]),
        "user": os.environ["TEST_POSTGRES_USER"],
        "password": os.environ["TEST_POSTGRES_PASSWORD"],
    }
    database_name = f"etm_msglog_time_{uuid.uuid4().hex}"
    admin_db = PostgresqlDatabase(**connection_kwargs)
    admin_db.connect()
    admin_db.connection().autocommit = True
    test_db = None
    model_binding = None
    try:
        admin_db.execute_sql(f'CREATE DATABASE "{database_name}"')
        test_db = PostgresqlDatabase(database_name, **{key: value for key, value in connection_kwargs.items() if key != "database"})
        model_binding = test_db.bind_ctx(DATABASE_MODELS)
        model_binding.__enter__()
        test_db.connect()
        test_db.create_tables([MsgLog, MsgLogIngestionScan])
        service = MsgLogIngestionService(
            MsgLogIngestionRepository("tests.master", test_db),
            FakeChatAssociations({10: "tests.slave target"}),
            FakeMTProto(
                {
                    3: topic_message(3, date=datetime(2026, 8, 4, 12, 30, tzinfo=timezone.utc)),
                    2: topic_message(2, date=datetime(2026, 8, 4, 18, tzinfo=timezone(timedelta(hours=5, minutes=30)))),
                    1: topic_message(1, date=datetime(2026, 8, 4, 8, 15)),
                },
                scan_ceiling=3,
            ),
        )
        asyncio.run(service.run(100, lease_owner="worker-a"))
        test_db.close()
        test_db.connect()
        rows = list(MsgLog.select().order_by(MsgLog.time, MsgLog.master_msg_id))
    finally:
        if test_db is not None and not test_db.is_closed():
            test_db.close()
        if model_binding is not None:
            model_binding.__exit__(None, None, None)
        admin_db.execute_sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (database_name,))
        admin_db.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_db.close()

    assert [(row.master_msg_id, row.time, row.time.tzinfo) for row in rows] == EXPECTED_MTPROTO_TIMES
