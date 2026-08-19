import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master.models import MsgLog, MsgLogIngestionLeaseLostError, MsgLogIngestionScan
from efb_telegram_master.msglog_ingestion import MsgLogIngestionService
from efb_telegram_master.runtime.mtproto import MTProtoRetryableError
from tests.unit.msglog_ingestion_support import FakeChatAssociations, FakeDatabase, FakeMTProto, sqlite_ingestion_database, topic_message


def test_association_rescan_restarts_completed_scan_and_repeats_active_lease():
    class Associations:
        def __init__(self):
            self.slave_uid = None
            self.requested_during_active_lease = False

        def get_topic_assoc_slave_uid(self, source_chat_id, topic_id):
            assert (source_chat_id, topic_id) == (100, 10)
            if self.slave_uid is not None and not self.requested_during_active_lease:
                self.requested_during_active_lease = True
                assert ingestion.request_association_rescan(100) == "running"
            return self.slave_uid

    with sqlite_ingestion_database() as (_, ingestion):
        associations = Associations()
        mtproto = FakeMTProto({1: topic_message(1)}, scan_ceiling=1)
        service = MsgLogIngestionService(ingestion, associations, mtproto)
        asyncio.run(service.run(100, lease_owner="worker-a"))

        completed = MsgLogIngestionScan.get(MsgLogIngestionScan.source_chat_id == "100")
        assert completed.status == "complete"
        assert MsgLog.select().count() == 0

        associations.slave_uid = "tests.slave target"
        assert ingestion.request_association_rescan(100) == "pending"
        asyncio.run(service.run(100, lease_owner="worker-b"))

        scan = MsgLogIngestionScan.get_by_id(completed.id)
        row = MsgLog.get_by_id("100.1")

        assert associations.requested_during_active_lease
        assert [ids for _channel, ids in mtproto.calls] == [[1], [1], [1]]
        assert (scan.status, scan.lease_owner, scan.rescan_requested) == ("complete", None, False)
        assert (row.provenance, row.slave_origin_uid) == ("mtproto_ingested", "tests.slave target")


def test_ingestion_marks_transient_mtproto_failure_for_retry_without_advancing_cursor(caplog):
    db = FakeDatabase(scan_boundary=5)
    mtproto = FakeMTProto({}, error=MTProtoRetryableError("temporary"), scan_ceiling=5)

    with caplog.at_level(logging.INFO, logger="efb_telegram_master.msglog_ingestion"):
        asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert db.scan.status == "retryable-error"
    assert db.scan.error == "temporary"
    assert db.scan.cursor == 5
    assert [(record.event, getattr(record, "error_type", None)) for record in caplog.records] == [
        ("msglog_ingestion.start", None),
        ("msglog_ingestion.retry", "MTProtoRetryableError"),
    ]


def test_ingestion_stops_after_blocked_fetch_without_persisting_or_fetching_again():
    db = FakeDatabase(scan_boundary=2)
    stop_requested = False
    fetched = asyncio.Event()
    release = asyncio.Event()

    class BlockingMTProto(FakeMTProto):
        async def get_channel_messages(self, channel, message_ids):
            self.calls.append((channel, list(message_ids)))
            fetched.set()
            await release.wait()
            return [topic_message(message_ids[0])]

    mtproto = BlockingMTProto({}, scan_ceiling=2)

    async def run():
        nonlocal stop_requested
        task = asyncio.create_task(MsgLogIngestionService(db, db.chat_associations, mtproto).run(100, lease_owner="worker-a", stop_requested=lambda: stop_requested))
        await fetched.wait()
        stop_requested = True
        release.set()
        await task

    asyncio.run(run())

    assert db.persisted == []
    assert len(mtproto.calls) == 1
    assert db.scan.status == "pending"
    assert db.scan.cursor == 2


def test_ingestion_already_stopped_performs_no_database_or_mtproto_work():
    ingestion = SimpleNamespace(
        get_or_create_scan=Mock(),
        claim_scan=Mock(),
        persist_item=Mock(),
        finish_scan=Mock(),
        release_scan=Mock(),
    )
    mtproto = SimpleNamespace(config=SimpleNamespace(scan_ceiling=1), get_input_channel=Mock(), get_channel_messages=Mock())

    asyncio.run(MsgLogIngestionService(ingestion, Mock(), mtproto).run(100, lease_owner="worker-a", stop_requested=lambda: True))

    for method in (
        ingestion.get_or_create_scan,
        ingestion.claim_scan,
        ingestion.persist_item,
        ingestion.finish_scan,
        ingestion.release_scan,
        mtproto.get_input_channel,
        mtproto.get_channel_messages,
    ):
        method.assert_not_called()


def test_ingestion_logs_lease_loss_with_a_stable_event(caplog):
    db = FakeDatabase(scan_boundary=5)
    mtproto = FakeMTProto({}, scan_ceiling=5)

    async def lose_lease(_source_chat_id):
        raise MsgLogIngestionLeaseLostError()

    mtproto.get_input_channel = lose_lease
    with caplog.at_level(logging.INFO, logger="efb_telegram_master.msglog_ingestion"):
        asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert [(record.event, getattr(record, "error_type", None)) for record in caplog.records] == [
        ("msglog_ingestion.start", None),
        ("msglog_ingestion.lease_lost", None),
    ]


def test_ingestion_does_not_log_complete_when_lease_is_lost_at_completion(caplog):
    with sqlite_ingestion_database() as (_, ingestion):
        service = MsgLogIngestionService(ingestion, FakeChatAssociations({10: "tests.slave target"}), FakeMTProto({1: topic_message(1)}, scan_ceiling=1))
        original_persist_item = ingestion.persist_item

        def persist_then_transfer_lease(scan, **kwargs):
            outcome = original_persist_item(scan, **kwargs)
            MsgLogIngestionScan.update(lease_owner="worker-b", lease_expires_at=datetime.now() + timedelta(seconds=60)).where(MsgLogIngestionScan.id == scan.id).execute()
            return outcome

        ingestion.persist_item = persist_then_transfer_lease
        with caplog.at_level(logging.INFO, logger="efb_telegram_master.msglog_ingestion"):
            asyncio.run(service.run(100, lease_owner="worker-a"))
        scan = MsgLogIngestionScan.get(MsgLogIngestionScan.source_chat_id == "100")

        assert (scan.status, scan.lease_owner) == ("running", "worker-b")
        assert [record.event for record in caplog.records] == ["msglog_ingestion.start", "msglog_ingestion.lease_lost"]


def test_ingestion_logs_unexpected_failure_with_error_type(caplog):
    db = FakeDatabase(scan_boundary=5)
    mtproto = FakeMTProto({}, scan_ceiling=5)

    async def fail(_source_chat_id):
        raise ValueError("failed")

    mtproto.get_input_channel = fail
    with caplog.at_level(logging.INFO, logger="efb_telegram_master.msglog_ingestion"):
        asyncio.run(MsgLogIngestionService(db.msglog_ingestion, db.chat_associations, mtproto).run(100, lease_owner="worker-a"))

    assert db.scan.status == "error"
    assert [(record.event, getattr(record, "error_type", None)) for record in caplog.records] == [
        ("msglog_ingestion.start", None),
        ("msglog_ingestion.error", "ValueError"),
    ]
