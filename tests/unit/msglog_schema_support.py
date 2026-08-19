import os

OLD_MSGLOG_SCHEMA = """
CREATE TABLE "msglog" (
    "master_msg_id" TEXT NOT NULL PRIMARY KEY,
    "master_msg_id_alt" TEXT,
    "slave_message_id" TEXT NOT NULL,
    "text" TEXT NOT NULL,
    "slave_origin_uid" TEXT NOT NULL,
    "slave_origin_display_name" TEXT,
    "slave_member_uid" TEXT,
    "slave_member_display_name" TEXT,
    "media_type" TEXT,
    "mime" TEXT,
    "file_id" TEXT,
    "file_unique_id" TEXT,
    "msg_type" TEXT NOT NULL,
    "pickle" {blob_type},
    "sent_to" TEXT NOT NULL,
    "sender_bot_id" TEXT,
    "time" {time_type}
)
"""

LEGACY_INGESTION_SCAN_SCHEMA = """
CREATE TABLE "msglogingestionscan" (
    "id" INTEGER NOT NULL PRIMARY KEY,
    "source_chat_id" TEXT NOT NULL UNIQUE,
    "scan_boundary" INTEGER NOT NULL,
    "cursor" INTEGER NOT NULL,
    "existing_streak" INTEGER NOT NULL DEFAULT 0,
    "scanned_count" INTEGER NOT NULL DEFAULT 0,
    "inserted_count" INTEGER NOT NULL DEFAULT 0,
    "existing_count" INTEGER NOT NULL DEFAULT 0,
    "skipped_count" INTEGER NOT NULL DEFAULT 0,
    "lease_owner" TEXT,
    "lease_expires_at" {timestamp_type},
    "status" TEXT NOT NULL DEFAULT 'pending',
    "error" TEXT,
    "created_at" {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def create_old_msglog_schema(test_db, *, blob_type="BLOB", time_type="DATETIME"):
    test_db.execute_sql(OLD_MSGLOG_SCHEMA.format(blob_type=blob_type, time_type=time_type))
    placeholders = ", ".join([test_db.param] * 6)
    test_db.execute_sql(
        f"INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, msg_type, sent_to) VALUES ({placeholders})",
        ("100.1", "old-message", "old text", "tests.slave chat", "Text", "tests.master"),
    )


def create_legacy_ingestion_scan_schema(test_db, *, timestamp_type="DATETIME"):
    test_db.execute_sql(LEGACY_INGESTION_SCAN_SCHEMA.format(timestamp_type=timestamp_type))


def insert_legacy_ingestion_scan_rows(test_db):
    columns = ("source_chat_id", "scan_boundary", "cursor", "existing_streak", "scanned_count", "inserted_count", "existing_count", "skipped_count", "lease_owner", "status", "error")
    placeholders = ", ".join([test_db.param] * len(columns))
    statement = f"INSERT INTO msglogingestionscan ({', '.join(columns)}) VALUES ({placeholders})"
    for row in (
        ("100", 500, 0, 500, 500, 5, 495, 0, None, "complete", None),
        ("200", 900, 900, 0, 0, 0, 0, 0, None, "pending", None),
        ("300", 1000, 875, 125, 125, 20, 90, 15, "worker-a", "running", "temporary failure"),
    ):
        test_db.execute_sql(statement, row)


def legacy_ingestion_scan_rows(test_db):
    return test_db.execute_sql(
        "SELECT source_chat_id, scan_boundary, cursor, existing_streak, scanned_count, inserted_count, "
        "existing_count, skipped_count, lease_owner, status, error, rescan_requested "
        "FROM msglogingestionscan ORDER BY source_chat_id"
    ).fetchall()


def msglog_values(master_msg_id, **values):
    return {
        "master_msg_id": master_msg_id,
        "slave_message_id": f"slave-{master_msg_id}",
        "text": "text",
        "slave_origin_uid": "tests.slave chat",
        "slave_member_uid": "tests.slave author",
        "msg_type": "Text",
        "sent_to": "tests.master",
        **values,
    }


def postgres_connection_kwargs():
    return {
        "database": os.environ["TEST_POSTGRES_DB"],
        "host": os.environ["TEST_POSTGRES_HOST"],
        "port": int(os.environ["TEST_POSTGRES_PORT"]),
        "user": os.environ["TEST_POSTGRES_USER"],
        "password": os.environ["TEST_POSTGRES_PASSWORD"],
    }
