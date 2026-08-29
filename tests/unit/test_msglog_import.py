import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from peewee import PostgresqlDatabase, SqliteDatabase

from efb_telegram_master.db import MsgLog, TopicAssoc, database
from efb_telegram_master.msglog_import import (
    ImportValidationError,
    import_validated_artifact,
    parse_chat_file,
    validate_artifact,
)

WINDOW_FROM = "2026-01-01T00:00:00.000Z"
WINDOW_TO = "2026-01-02T00:00:00.000Z"
BASE_TIMESTAMP = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
TOPIC_CHAT_ID = "-1000000000042"


def _manifest(chats=None):
    return {
        "type": "manifest",
        "version": 1,
        "owner": {
            "profile": "recovery",
            "telegramUserId": "700",
            "username": "owner",
            "name": "Owner",
        },
        "window": {"from": WINDOW_FROM, "to": WINDOW_TO, "semantics": "[from,to)"},
        "chats": chats
        or [
            {
                "topicChatId": TOPIC_CHAT_ID,
                "sourceChatId": "42",
                "title": "Forum",
                "type": "supergroup",
            }
        ],
    }


def _message(
    message_id,
    *,
    sender_id="100",
    topic_chat_id=TOPIC_CHAT_ID,
    source_chat_id="42",
    timestamp=None,
    reply_to_top_id="900",
    reply_to_id=None,
    media=None,
):
    return {
        "type": "message",
        "version": 1,
        "topicChatId": topic_chat_id,
        "sourceChatId": source_chat_id,
        "messageId": str(message_id),
        "senderId": sender_id,
        "timestamp": timestamp
        if timestamp is not None
        else BASE_TIMESTAMP + int(message_id),
        "text": f"message-{message_id}",
        "replyToId": reply_to_id,
        "replyToTopId": reply_to_top_id,
        "media": media,
    }


def _write_artifact(path: Path, messages, chats=None):
    records = [_manifest(chats), *messages]
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )
    return path


def _validated(path, messages, chats=None, selected=(TOPIC_CHAT_ID,)):
    _write_artifact(path, messages, chats)
    return validate_artifact(path, "recovery", selected)


@contextmanager
def _bound_database(actual_database):
    original_database = database.obj
    database.initialize(actual_database)
    actual_database.connect(reuse_if_open=True)
    actual_database.create_tables([MsgLog, TopicAssoc])
    try:
        yield
    finally:
        actual_database.drop_tables([MsgLog, TopicAssoc], safe=True)
        actual_database.close()
        database.initialize(original_database)


@pytest.fixture
def sqlite_database(tmp_path):
    with _bound_database(SqliteDatabase(tmp_path / "msglog-import.db")):
        yield


def test_chat_file_matches_exporter_normalization_and_rejects_source_collisions():
    assert parse_chat_file("""
        # ETM groups
        -10012345678901234567890
        -42 # basic group
        -10012345678901234567890
        -00042
    """) == ("-10012345678901234567890", "-42")

    with pytest.raises(ImportValidationError, match="line 1"):
        parse_chat_file("group-one")
    with pytest.raises(ImportValidationError, match="does not contain"):
        parse_chat_file("# none")
    with pytest.raises(ImportValidationError, match="same source chat"):
        parse_chat_file("-1000000000042\n42\n")


def test_artifact_validation_rejects_rows_before_import(tmp_path, sqlite_database):
    artifact_path = _write_artifact(
        tmp_path / "invalid.jsonl",
        [_message(2), _message(1)],
    )
    with pytest.raises(ImportValidationError, match="deterministic order"):
        validate_artifact(artifact_path, "recovery", (TOPIC_CHAT_ID,))
    assert MsgLog.select().count() == 0

    outside_path = _write_artifact(
        tmp_path / "outside.jsonl",
        [_message(1, timestamp=BASE_TIMESTAMP - 1)],
    )
    with pytest.raises(ImportValidationError, match="outside"):
        validate_artifact(outside_path, "recovery", (TOPIC_CHAT_ID,))
    assert MsgLog.select().count() == 0


def test_sqlite_import_filters_senders_resolves_topics_and_is_idempotent(
    tmp_path, sqlite_database
):
    chats = [
        _manifest()["chats"][0],
        {
            "topicChatId": "-43",
            "sourceChatId": "43",
            "title": "Old group",
            "type": "group",
        },
        {
            "topicChatId": "-44",
            "sourceChatId": "44",
            "title": "Unselected group",
            "type": "group",
        },
    ]
    artifact = _validated(
        tmp_path / "recovery.jsonl",
        [
            _message(1, media={"type": "photo", "mimeType": "image/jpeg"}),
            _message(2, sender_id="200"),
            _message(3, sender_id="300"),
            _message(4, reply_to_top_id="999", reply_to_id="900"),
            _message(5, reply_to_top_id=None, reply_to_id="900"),
            _message(6, reply_to_top_id="998"),
            _message(
                7, topic_chat_id="-43", source_chat_id="43", reply_to_top_id="700"
            ),
            _message(
                8, topic_chat_id="-44", source_chat_id="44", reply_to_top_id="800"
            ),
        ],
        chats,
        (TOPIC_CHAT_ID, "-43"),
    )
    TopicAssoc.create(
        topic_chat_id=TOPIC_CHAT_ID,
        message_thread_id="900",
        slave_uid="tests.slave destination",
    )
    config = {
        "token": "100:main-secret",
        "auxiliary_bots": [{"token": "200:aux-secret"}],
    }

    first = import_validated_artifact(artifact, config, chunk_size=2)

    assert first.artifact_messages == 8
    assert first.selected_messages == 7
    assert first.imported == 3
    assert first.skipped_sender == 1
    assert first.skipped_unbound_topic == 2
    assert first.skipped_unknown_chat == 1
    assert first.unknown_chat_ids == ["-43"]
    assert first.unbound_topic_ids == [
        f"{TOPIC_CHAT_ID}.999",
        f"{TOPIC_CHAT_ID}.998",
    ]
    rows = {row.master_msg_id: row for row in MsgLog.select()}
    assert set(rows) == {
        f"{TOPIC_CHAT_ID}.1",
        f"{TOPIC_CHAT_ID}.2",
        f"{TOPIC_CHAT_ID}.5",
    }
    assert rows[f"{TOPIC_CHAT_ID}.1"].sender_bot_id is None
    assert rows[f"{TOPIC_CHAT_ID}.1"].media_type == "Photo"
    assert rows[f"{TOPIC_CHAT_ID}.1"].msg_type == "Image"
    assert rows[f"{TOPIC_CHAT_ID}.2"].sender_bot_id == "200"
    assert rows[f"{TOPIC_CHAT_ID}.5"].slave_origin_uid == "tests.slave destination"
    assert rows[f"{TOPIC_CHAT_ID}.5"].slave_member_uid == "tests.slave destination"
    assert rows[f"{TOPIC_CHAT_ID}.5"].sent_to == "tests.slave"
    assert rows[f"{TOPIC_CHAT_ID}.5"].slave_message_id.startswith("mtproto-backfill:")

    second = import_validated_artifact(artifact, config, chunk_size=2)
    assert second.imported == 0
    assert second.existing == 3
    assert MsgLog.select().count() == 3


def test_primary_and_alternate_ids_are_both_preserved(tmp_path, sqlite_database):
    artifact = _validated(
        tmp_path / "conflicts.jsonl", [_message(1), _message(2), _message(3)]
    )
    TopicAssoc.create(
        topic_chat_id=TOPIC_CHAT_ID,
        message_thread_id="900",
        slave_uid="tests.slave destination",
    )
    MsgLog.create(
        master_msg_id=f"{TOPIC_CHAT_ID}.1",
        master_msg_id_alt=f"{TOPIC_CHAT_ID}.2",
        slave_message_id="live-message",
        text="live text",
        slave_origin_uid="tests.slave live",
        slave_member_uid="tests.slave author",
        media_type="Text",
        msg_type="Text",
        sent_to="tests.slave",
    )

    summary = import_validated_artifact(artifact, {"token": "100:secret"})

    assert summary.imported == 1
    assert summary.existing == 2
    live = MsgLog.get_by_id(f"{TOPIC_CHAT_ID}.1")
    assert live.text == "live text"
    assert live.master_msg_id_alt == f"{TOPIC_CHAT_ID}.2"
    assert MsgLog.get_by_id(f"{TOPIC_CHAT_ID}.3").text == "message-3"


def test_conflicting_topic_associations_abort_before_writes(tmp_path, sqlite_database):
    artifact = _validated(tmp_path / "topic-conflict.jsonl", [_message(1)])
    TopicAssoc.create(
        topic_chat_id=TOPIC_CHAT_ID,
        message_thread_id="900",
        slave_uid="tests.slave first",
    )
    TopicAssoc.create(
        topic_chat_id=TOPIC_CHAT_ID,
        message_thread_id="900",
        slave_uid="tests.slave second",
    )

    with pytest.raises(ImportValidationError, match="Conflicting TopicAssoc"):
        import_validated_artifact(artifact, {"token": "100:secret"})
    assert MsgLog.select().count() == 0


def test_chunk_failure_rolls_back_current_chunk_and_rerun_resumes(
    tmp_path, sqlite_database, monkeypatch
):
    artifact = _validated(
        tmp_path / "restart.jsonl", [_message(1), _message(2), _message(3)]
    )
    TopicAssoc.create(
        topic_chat_id=TOPIC_CHAT_ID,
        message_thread_id="900",
        slave_uid="tests.slave destination",
    )
    original_insert_many = MsgLog.insert_many
    call_count = 0

    class ExecuteThenFail:
        def __init__(self, query):
            self.query = query

        def on_conflict_ignore(self):
            self.query = self.query.on_conflict_ignore()
            return self

        def execute(self):
            self.query.execute()
            raise RuntimeError("injected chunk failure")

    def failing_insert_many(rows, fields=None):
        nonlocal call_count
        call_count += 1
        query = original_insert_many(rows, fields=fields)
        return ExecuteThenFail(query) if call_count == 2 else query

    monkeypatch.setattr(MsgLog, "insert_many", failing_insert_many)
    with pytest.raises(RuntimeError, match="injected chunk failure"):
        import_validated_artifact(artifact, {"token": "100:secret"}, chunk_size=1)
    assert [row.master_msg_id for row in MsgLog.select()] == [f"{TOPIC_CHAT_ID}.1"]

    monkeypatch.setattr(MsgLog, "insert_many", original_insert_many)
    resumed = import_validated_artifact(artifact, {"token": "100:secret"}, chunk_size=1)
    assert resumed.existing == 1
    assert resumed.imported == 2
    assert MsgLog.select().count() == 3


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_HOST"),
    reason="PostgreSQL test environment is not configured",
)
def test_postgresql_import_uses_the_same_peewee_path(tmp_path):
    connection_kwargs = {
        "database": os.environ["TEST_POSTGRES_DB"],
        "host": os.environ["TEST_POSTGRES_HOST"],
        "port": int(os.environ["TEST_POSTGRES_PORT"]),
        "user": os.environ["TEST_POSTGRES_USER"],
        "password": os.environ["TEST_POSTGRES_PASSWORD"],
    }
    database_name = f"etm_import_{uuid.uuid4().hex}"
    admin_database = PostgresqlDatabase(**connection_kwargs)
    admin_database.connect()
    admin_database.connection().autocommit = True
    test_database = None
    try:
        admin_database.execute_sql(f'CREATE DATABASE "{database_name}"')
        test_database = PostgresqlDatabase(
            database_name,
            **{
                key: value
                for key, value in connection_kwargs.items()
                if key != "database"
            },
        )
        with _bound_database(test_database):
            artifact = _validated(tmp_path / "postgres.jsonl", [_message(1)])
            TopicAssoc.create(
                topic_chat_id=TOPIC_CHAT_ID,
                message_thread_id="900",
                slave_uid="tests.slave destination",
            )
            first = import_validated_artifact(artifact, {"token": "100:secret"})
            second = import_validated_artifact(artifact, {"token": "100:secret"})
            assert first.imported == 1
            assert second.existing == 1
    finally:
        if test_database is not None and not test_database.is_closed():
            test_database.close()
        admin_database.execute_sql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database_name,),
        )
        admin_database.execute_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_database.close()
