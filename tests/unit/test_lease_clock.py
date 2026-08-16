import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from peewee import SqliteDatabase

from efb_telegram_master.models import UTC_LEASE_CLOCK, MsgLogIngestionScan, SlaveMessageDelivery, database
from efb_telegram_master.msglog_ingestion_repository import MsgLogIngestionRepository
from efb_telegram_master.msglog_scan import MsgLogScanScheduler
from efb_telegram_master.slave_message_delivery_repository import SlaveMessageDeliveryRepository


def _with_timezone(timezone_name):
    original_timezone = os.environ.get("TZ")
    os.environ["TZ"] = timezone_name
    time.tzset()
    return original_timezone


def _restore_timezone(original_timezone):
    if original_timezone is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = original_timezone
    time.tzset()


def test_legacy_local_leases_are_not_reclaimed_early_or_blocked_after_expiry():
    original_timezone = _with_timezone("Pacific/Pago_Pago")
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    scans = MsgLogIngestionRepository("tests")
    deliveries = SlaveMessageDeliveryRepository()
    try:
        test_db.create_tables([MsgLogIngestionScan, SlaveMessageDelivery])
        scan = scans.get_or_create_scan(100, 500)
        MsgLogIngestionScan.update(status="running", lease_owner="legacy", lease_expires_at=datetime.now() + timedelta(minutes=1), lease_clock=None).where(MsgLogIngestionScan.id == scan.id).execute()
        SlaveMessageDelivery.create(slave_origin_uid="tests.slave chat", slave_message_id="legacy", lease_expires_at=datetime.now() + timedelta(minutes=1), lease_clock=None, owner_token="legacy")

        assert scans.claim_scan(100, "other", 60) is None
        assert deliveries.claim("tests.slave chat", "legacy") is None

        os.environ["TZ"] = "Pacific/Kiritimati"
        time.tzset()
        MsgLogIngestionScan.update(lease_expires_at=datetime.now() - timedelta(seconds=1)).where(MsgLogIngestionScan.id == scan.id).execute()
        SlaveMessageDelivery.update(lease_expires_at=datetime.now() - timedelta(seconds=1)).where(SlaveMessageDelivery.slave_message_id == "legacy").execute()

        assert scans.claim_scan(100, "replacement", 60) is not None
        assert deliveries.claim("tests.slave chat", "legacy") is not None
        assert MsgLogIngestionScan.get_by_id(scan.id).lease_clock == UTC_LEASE_CLOCK
        assert SlaveMessageDelivery.get(SlaveMessageDelivery.slave_message_id == "legacy").lease_clock == UTC_LEASE_CLOCK
    finally:
        test_db.close()
        database.initialize(original_database)
        _restore_timezone(original_timezone)


def test_new_utc_scan_lease_and_scheduler_deferral_ignore_host_timezone():
    original_timezone = _with_timezone("Pacific/Kiritimati")
    original_database = database.obj
    test_db = SqliteDatabase(":memory:")
    database.initialize(test_db)
    test_db.connect()
    scans = MsgLogIngestionRepository("tests")
    deliveries = SlaveMessageDeliveryRepository()
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        test_db.create_tables([MsgLogIngestionScan, SlaveMessageDelivery])
        scans.get_or_create_scan(100, 500)
        scan = scans.claim_scan(100, "worker", 120)
        assert scan is not None
        stored = MsgLogIngestionScan.get_by_id(scan.id)
        assert stored.lease_clock == UTC_LEASE_CLOCK
        assert before + timedelta(seconds=115) <= stored.lease_expires_at <= datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=125)

        delivery_token = deliveries.claim("tests.slave chat", "utc")
        assert delivery_token is not None
        assert deliveries.renew("tests.slave chat", "utc", delivery_token)
        assert SlaveMessageDelivery.get(SlaveMessageDelivery.slave_message_id == "utc").lease_clock == UTC_LEASE_CLOCK
        assert deliveries.complete("tests.slave chat", "utc", delivery_token)
        release_token = deliveries.claim("tests.slave chat", "release")
        assert release_token is not None
        assert deliveries.release("tests.slave chat", "release", release_token)

        scheduler = object.__new__(MsgLogScanScheduler)
        scheduler.ingestion = SimpleNamespace(get_or_create_scan=lambda *_args: stored)
        scheduler.mtproto = SimpleNamespace(config=SimpleNamespace(scan_ceiling=500))
        scheduler._pending_source_chat_ids = set()
        captured = {}
        scheduler._enqueue_locked = lambda source_chat_id, **kwargs: captured.update(source_chat_id=source_chat_id, **kwargs)
        with patch("efb_telegram_master.msglog_scan.time.monotonic", return_value=100.0):
            scheduler._defer_unclaimed_scan_locked(100)
    finally:
        test_db.close()
        database.initialize(original_database)
        _restore_timezone(original_timezone)

    assert captured["source_chat_id"] == 100
    assert 210.0 <= captured["not_before"] <= 225.0
