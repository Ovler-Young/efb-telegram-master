import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master.msglog_ingestion import MsgLogIngestionService
from efb_telegram_master.msglog_scan import MsgLogScanScheduler
from efb_telegram_master.runtime.mtproto import MTProtoClient, MTProtoConfig
from tests.unit.mtproto_support import FakeClient


def enabled_config() -> MTProtoConfig:
    return MTProtoConfig.from_mapping({"enabled": True, "api_id": 123, "api_hash": "hash"})


@pytest.mark.asyncio
async def test_get_messages_builds_ascending_batches_of_at_most_100(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(MTProtoClient, "_build_telethon_client", staticmethod(FakeClient))
    client = MTProtoClient(enabled_config(), "bot-token", tmp_path)
    await client.connect()

    responses = await client.get_channel_messages("channel", list(range(205, 0, -1)))

    assert [request.id for request in client.client.requests] == [
        list(range(1, 101)),
        list(range(101, 201)),
        list(range(201, 206)),
    ]
    assert len(responses) == 205
    await client.disconnect()


def test_msglog_scan_recovers_mtproto_before_running_pending_work(monkeypatch: pytest.MonkeyPatch):
    completed = threading.Event()

    class Runtime:
        def call(self, coroutine, timeout=None):
            asyncio.run(coroutine)

    class MTProto:
        enabled = True
        connected = False
        config = SimpleNamespace(scan_ceiling=10)

        def __init__(self) -> None:
            self.connect_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1
            self.connected = True

    async def run(_service, _source_chat_id, *, lease_owner, stop_requested):
        assert lease_owner
        assert not stop_requested()
        completed.set()

    monkeypatch.setattr(MsgLogIngestionService, "run", run)
    mtproto = MTProto()
    ingestion = SimpleNamespace(get_or_create_scan=Mock(return_value=SimpleNamespace(status="pending", scanned_count=3)))
    scheduler = MsgLogScanScheduler(SimpleNamespace(async_runtime=Runtime()), mtproto, ingestion, Mock(), Mock())

    assert scheduler.schedule(100) == "resumed"
    assert completed.wait(1)
    assert mtproto.connect_calls == 1
    assert scheduler.stop(1) == ()
