"""A missing HTTP response must not send an already accepted message again."""
from concurrent.futures import Future
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import tracemalloc
from types import SimpleNamespace

import httpx
import pytest
from telegram import Bot, Chat, Document, Message, Update, User
from telegram.error import NetworkError, TimedOut, RetryAfter
from telegram.request import HTTPXRequest

from efb_telegram_master.outbound import (
    DeliveryUncertainError, OutboundQueue, OutboundQueueScheduler, QueueRequest, QueuePersistenceError, _NamedMediaFile,
)
from efb_telegram_master.bot_manager import AsyncTelegramRuntime, SyncBotFacade, QueuedDbLogContext
from efb_telegram_master.hold_send import hold
from tests.unit.test_database_safety import manager_factory as database_manager_fixture

manager_factory = database_manager_fixture
from tests.unit.test_restart_memory import message_with_group
from tests.unit.test_outbound_queue_runtime_evidence import manager_adapter


class ImmediateExecutor:
    def submit(self, fn, *args):
        result = Future()
        try:
            result.set_result(fn(*args))
        except BaseException as error:
            result.set_exception(error)
        return result


def document(chat_id, document, **kwargs):
    return None


def accepted_then_lost_response(accepted, cause_type=httpx.ReadTimeout):
    def send(*args, **kwargs):
        accepted.append((args, kwargs))  # Telegram accepted it; only its response is lost.
        try:
            raise cause_type("response lost after send")
        except httpx.HTTPError as cause:
            if isinstance(cause, httpx.TimeoutException):
                raise TimedOut("response lost") from cause
            raise NetworkError("response lost") from cause
    return send


@pytest.mark.parametrize("cause_type", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError])
def test_accepted_document_with_lost_response_is_not_retried_or_deleted(tmp_path, monkeypatch, cause_type):
    clock = [100.0]
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: clock[0])
    queue = OutboundQueue(tmp_path)
    row_id, waiter = queue.enqueue_many(
        [QueueRequest("send_document", (42, b"archive bytes"), {})], lambda _: document,
    )
    accepted = []
    manager = manager_adapter()
    manager._bot = SimpleNamespace(send_document=accepted_then_lost_response(accepted, cause_type))
    scheduler = OutboundQueueScheduler(queue, manager, ImmediateExecutor(), 1)
    try:
        for _ in range(6):
            scheduler.dispatch_once()
            scheduler.harvest_completed()
            clock[0] += 40.0
        assert len(accepted) == 1, "a lost response caused duplicate remote sends"
        assert queue.connection.execute("SELECT count(*) FROM outbound_queue WHERE id=?", (row_id,)).fetchone()[0] == 1
        assert list(queue.media_dir.iterdir()), "unconfirmed media must be retained"
        assert not scheduler.stopping
    finally:
        queue.close()


def delivered_message(sender=900, chat=42):
    return Message(100, datetime.now(timezone.utc), Chat(chat, "supergroup"),
                   from_user=User(sender, "test-bot", True),
                   document=Document("file-id", "unique-file-id", file_name="archive.zip", file_size=13))


def setup_manager(queue):
    manager = manager_adapter()
    manager._outbound_queue = queue
    manager.me = User(900, "test-bot", True)
    manager._outbound_scheduler = OutboundQueueScheduler(queue, manager, ImmediateExecutor(), 1)
    return manager


def test_unknown_result_survives_restart_and_other_messages_continue(tmp_path):
    queue = OutboundQueue(tmp_path)
    original, _ = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {})], lambda _: document)
    accepted = []
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(send_document=accepted_then_lost_response(accepted))
    manager._outbound_scheduler.dispatch_once()
    manager._outbound_scheduler.harvest_completed()
    assert queue.connection.execute("SELECT delivery_hold FROM outbound_queue WHERE id=?", (original,)).fetchone()[0].startswith("uncertain:")
    queue.close()

    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    new_calls = []
    manager._bot = SimpleNamespace(send_document=accepted_then_lost_response(accepted),
                                   send_message=lambda *a, **kw: new_calls.append(a) or delivered_message())
    later, _ = queue.enqueue_many([QueueRequest("send_message", (42, "later"), {})], lambda _: lambda chat_id, text: None)
    other, _ = queue.enqueue_many([QueueRequest("send_message", (43, "other"), {})], lambda _: lambda chat_id, text: None)
    for _ in range(6):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
    assert len(accepted) == 1 and len(new_calls) == 2
    assert queue.connection.execute("SELECT id FROM outbound_queue").fetchall() == [(original,)]
    queue.close()


def test_crash_after_remote_acceptance_before_harvest_does_not_resend(tmp_path):
    queue = OutboundQueue(tmp_path)
    row_id, _ = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {})], lambda _: document)
    sender = Mock(return_value=delivered_message())
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(send_document=sender)
    manager._outbound_scheduler.dispatch_once()
    # Simulated crash boundary: ACK returned, but harvest never persisted the receipt.
    assert sender.call_count == 1
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(send_document=sender)
    for _ in range(3):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
    assert sender.call_count == 1
    assert queue.connection.execute("SELECT delivery_hold FROM outbound_queue WHERE id=?", (row_id,)).fetchone()[0] == "in_flight"
    assert manager.confirm_queued_delivery(row_id, delivered_message())
    assert not queue.connection.execute("SELECT id FROM outbound_queue").fetchall()
    queue.close()


@pytest.mark.parametrize("cause_type", [httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ConnectError])
def test_proven_presend_failure_still_retries(tmp_path, cause_type):
    queue = OutboundQueue(tmp_path)
    _, waiter = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {})], lambda _: document)
    failure = TimedOut()
    failure.__cause__ = cause_type("request not sent")
    sender = Mock(side_effect=[failure, delivered_message()])
    manager = setup_manager(queue)
    manager.TRANSPORT_RETRY_SECONDS = 0
    manager._bot = SimpleNamespace(send_document=sender)
    for _ in range(2):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
    assert sender.call_count == 2 and waiter.result().message_id == 100
    assert not queue.connection.execute("SELECT id FROM outbound_queue").fetchall()
    queue.close()


def test_known_rate_limit_remains_retryable(tmp_path):
    queue = OutboundQueue(tmp_path)
    _, waiter = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {})], lambda _: document)
    sender = Mock(side_effect=[RetryAfter(0), delivered_message()])
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(send_document=sender)
    for _ in range(2):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
    assert sender.call_count == 2 and waiter.result().message_id == 100
    queue.close()


def test_operator_confirmation_writes_real_msglog_without_resending(tmp_path, manager_factory):
    db = manager_factory(tmp_path)
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager.channel = SimpleNamespace(db=db)
    context = manager._encode_queued_log_context(QueuedDbLogContext(message_with_group(), None))
    row_id, _ = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {}, context)], lambda _: document)
    accepted = []
    manager._bot = SimpleNamespace(send_document=accepted_then_lost_response(accepted))
    manager._outbound_scheduler.dispatch_once()
    manager._outbound_scheduler.harvest_completed()
    assert db.get_msg_log(master_msg_id="42.100") is None
    with pytest.raises(ValueError, match="original"):
        manager.confirm_queued_delivery(row_id, delivered_message(sender=901))
    with pytest.raises(ValueError, match="original"):
        manager.confirm_queued_delivery(row_id, delivered_message(chat=43))
    forwarded = delivered_message()
    object.__setattr__(forwarded, "forward_origin", object())
    with pytest.raises(ValueError, match="original"):
        manager.confirm_queued_delivery(row_id, forwarded)
    assert manager.confirm_queued_delivery(row_id, delivered_message())
    stored = db.get_msg_log(master_msg_id="42.100")
    assert stored is not None and stored.slave_message_id == "source-1"
    assert stored.file_id == "file-id"
    assert len(accepted) == 1
    assert not queue.connection.execute("SELECT id FROM outbound_queue").fetchall()
    assert not list(queue.media_dir.iterdir())
    queue.close()


def test_msglog_failure_keeps_receipt_and_never_reuploads(tmp_path, manager_factory):
    db = manager_factory(tmp_path)
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager.channel = SimpleNamespace(db=db)
    context = manager._encode_queued_log_context(QueuedDbLogContext(message_with_group(), None))
    row_id, _ = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {}, context)], lambda _: document)
    sender = Mock(return_value=delivered_message())
    manager._bot = SimpleNamespace(send_document=sender)
    with patch.object(db, "add_or_update_message_log", side_effect=RuntimeError("database temporarily unavailable")):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
        for _ in range(3):
            manager._outbound_scheduler.dispatch_once()
    assert sender.call_count == 1
    state, receipt, log = queue.connection.execute(
        "SELECT delivery_state, completion_receipt, log_context FROM outbound_queue WHERE id=?", (row_id,)
    ).fetchone()
    assert state == "sent_pending" and receipt and log == context
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET reconcile_after=0 WHERE id=?", (row_id,))
    assert row_id in manager._outbound_scheduler.reconcile_sent_pending(row_id)
    assert db.get_msg_log(master_msg_id="42.100") is not None
    assert sender.call_count == 1
    queue.close()


def test_real_ptb_httpx_lost_response_does_not_generate_second_send(tmp_path, monkeypatch):
    """Actual PTB + HTTPX exception conversion; no external Telegram side effects."""
    accepted = []
    original_read = _NamedMediaFile.read
    def bounded_read(stream, size=-1):
        assert size > 0, "the Telegram SDK tried to materialize the entire upload"
        return original_read(stream, size)
    monkeypatch.setattr(_NamedMediaFile, "read", bounded_read)
    async def transport(request):
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 900, "is_bot": True, "first_name": "test"}})
        assert request.url.path.endswith("sendDocument")
        assert b"archive bytes" in await request.aread()
        accepted.append(request.url.path)
        raise httpx.ReadTimeout("lost response after remote acceptance", request=request)
    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(transport)})
    bot = Bot("900:test-token", request=request)
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    runtime = AsyncTelegramRuntime(manager.logger)
    runtime._ensure_background_loop()
    runtime.call(bot.initialize())
    manager._bot = SyncBotFacade(bot, runtime)
    manager.TRANSPORT_RETRY_SECONDS = 0
    row_id, waiter = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {})], lambda _: document)
    try:
        manager._outbound_scheduler.dispatch_once()
        failure = manager._outbound_scheduler.in_flight[row_id].future.exception()
        assert isinstance(failure, TimedOut), repr(failure)
        manager._outbound_scheduler.harvest_completed()
        for _ in range(6):
            manager._outbound_scheduler.dispatch_once()
            manager._outbound_scheduler.harvest_completed()
        assert len(accepted) == 1
        with pytest.raises(DeliveryUncertainError):
            waiter.result()
        assert queue.connection.execute("SELECT delivery_hold FROM outbound_queue WHERE id=?", (row_id,)).fetchone()[0] == "uncertain:TimedOut/ReadTimeout"
    finally:
        runtime.call(bot.shutdown())
        runtime.shutdown()
        queue.close()


def test_offline_hold_preserves_payload_and_no_default_mutation(tmp_path):
    queue = OutboundQueue(tmp_path)
    row_id, _ = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {}, b"log")], lambda _: document)
    before = queue.connection.execute("SELECT payload, log_context FROM outbound_queue WHERE id=?", (row_id,)).fetchone()
    assert not hold(tmp_path, row_id)["held"]
    assert queue.heads(ready_only=True)
    assert hold(tmp_path, row_id, apply=True)["held"]
    assert not queue.heads(ready_only=True)
    assert queue.connection.execute("SELECT payload, log_context FROM outbound_queue WHERE id=?", (row_id,)).fetchone() == before
    assert list(queue.media_dir.iterdir())
    queue.close()


def test_381_mb_sidecar_reaches_http_transport_without_whole_file_read(tmp_path, monkeypatch):
    size = 381_320_453
    source = tmp_path / "archive.zip"
    with source.open("wb") as stream:
        stream.truncate(size)
    read = _NamedMediaFile.read
    def bounded_read(stream, length=-1):
        assert length > 0, "unbounded SDK read"
        return read(stream, length)
    monkeypatch.setattr(_NamedMediaFile, "read", bounded_read)
    totals = []
    class StreamingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("getMe"):
                return httpx.Response(200, json={"ok": True, "result": {"id": 900, "is_bot": True, "first_name": "test"}})
            consumed = 0
            async for chunk in request.stream:
                consumed += len(chunk)
            totals.append(consumed)
            raise httpx.ReadTimeout("accepted upload; dropped response", request=request)
    request = HTTPXRequest(httpx_kwargs={"transport": StreamingTransport()})
    bot = Bot("900:test-token", request=request)
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    runtime = AsyncTelegramRuntime(manager.logger)
    runtime._ensure_background_loop()
    runtime.call(bot.initialize())
    manager._bot = SyncBotFacade(bot, runtime)
    tracemalloc.start()
    try:
        row_id, _ = queue.enqueue_many([QueueRequest("send_document", (42, source), {"filename": "archive.zip"})], lambda _: document)
        assert len(queue.heads()[0].payload) < 4096
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
        assert len(totals) == 1 and size <= totals[0] < size + 65536
        assert tracemalloc.get_traced_memory()[1] < 16 * 1024 * 1024
        assert queue.connection.execute("SELECT delivery_hold FROM outbound_queue WHERE id=?", (row_id,)).fetchone()[0].startswith("uncertain:")
    finally:
        tracemalloc.stop()
        runtime.call(bot.shutdown())
        runtime.shutdown()
        queue.close()


def test_attempt_marker_failure_prevents_network_send(tmp_path, monkeypatch):
    queue = OutboundQueue(tmp_path)
    row_id, _ = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {})], lambda _: document)
    manager = setup_manager(queue)
    sender = Mock(return_value=delivered_message())
    manager._bot = SimpleNamespace(send_document=sender)
    monkeypatch.setattr(queue, "begin_delivery_attempt", Mock(side_effect=QueuePersistenceError("disk error")))
    manager._outbound_scheduler.dispatch_once()
    sender.assert_not_called()
    assert manager._outbound_scheduler.stopping
    assert queue.connection.execute("SELECT id FROM outbound_queue WHERE id=?", (row_id,)).fetchone()
    queue.close()


def test_confirmation_command_rejects_non_admin():
    from efb_telegram_master import TelegramChannel
    manager = Mock()
    channel = SimpleNamespace(config={"admins": [1]}, bot_manager=manager)
    update = Update(1, message=Message(2, datetime.now(timezone.utc), Chat(42, "supergroup"),
                    from_user=User(2, "not-admin", False), text="/confirm_send 1",
                    reply_to_message=delivered_message()))
    TelegramChannel.confirm_send(channel, update, SimpleNamespace(args=["1"]))
    manager.confirm_queued_delivery.assert_not_called()
