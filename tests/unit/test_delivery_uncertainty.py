"""A missing HTTP response must not send an already accepted message again."""
import asyncio
from concurrent.futures import Future
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import tracemalloc
from types import SimpleNamespace

import httpx
import pytest
from telegram import Bot, Chat, Document, Message, Update, User
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut, RetryAfter
from telegram.request import BaseRequest, HTTPXRequest

from efb_telegram_master.outbound import (
    DeliveryUncertainError, OutboundQueue, OutboundQueueScheduler, QueueRequest, QueuePersistenceError, _NamedMediaFile,
)
from efb_telegram_master.bot_manager import SyncBotFacade, QueuedDbLogContext
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
    manager._queue_operation = lambda _: document
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


@pytest.mark.parametrize("supplemental", [False, True])
def test_real_ptb_httpx_lost_response_does_not_generate_second_send(tmp_path, monkeypatch, supplemental):
    """Actual PTB + HTTPX exception conversion; no external Telegram side effects."""
    accepted = []
    original_read = _NamedMediaFile.read
    def bounded_read(stream, size=-1):
        assert size > 0, "the Telegram SDK tried to materialize the entire upload"
        return original_read(stream, size)
    monkeypatch.setattr(_NamedMediaFile, "read", bounded_read)
    class LostResponseBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok":true,"result":'
            raise httpx.ReadTimeout("response body lost after remote acceptance")

    async def transport(request):
        if request.url.path.endswith("getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 900, "is_bot": True, "first_name": "test"}})
        assert request.url.path.endswith("sendDocument")
        body = await request.aread()
        assert (b"x" * 2048 if accepted else b"archive bytes") in body
        accepted.append(request.url.path)
        if supplemental and len(accepted) == 1:
            return httpx.Response(200, json={"ok": True, "result": delivered_message().to_dict()})
        return httpx.Response(200, stream=LostResponseBody())
    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(transport)})
    bot = Bot("900:test-token", request=request)
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    loop = asyncio.new_event_loop()
    runtime = SimpleNamespace(call=loop.run_until_complete)
    runtime.call(bot.initialize())
    manager._bot = SyncBotFacade(bot, runtime)
    manager.TRANSPORT_RETRY_SECONDS = 0
    row_id, waiter = queue.enqueue_many([QueueRequest(
        "send_document", (42, b"archive bytes"), {"caption": "x" * 2048} if supplemental else {}
    )], lambda _: document)
    try:
        manager._outbound_scheduler.dispatch_once()
        if supplemental:
            manager._outbound_scheduler.harvest_completed()
            assert waiter.result(timeout=1).message_id == 100
            manager._outbound_scheduler.dispatch_once()
            [row_id] = manager._outbound_scheduler.in_flight
        failure = manager._outbound_scheduler.in_flight[row_id].future.exception()
        assert isinstance(failure, TimedOut), repr(failure)
        manager._outbound_scheduler.harvest_completed()
        for _ in range(6):
            manager._outbound_scheduler.dispatch_once()
            manager._outbound_scheduler.harvest_completed()
        assert len(accepted) == (2 if supplemental else 1)
        if not supplemental:
            with pytest.raises(DeliveryUncertainError):
                waiter.result(timeout=1)
        assert queue.connection.execute("SELECT delivery_hold FROM outbound_queue WHERE id=?", (row_id,)).fetchone()[0] == "uncertain:TimedOut/ReadTimeout"
    finally:
        runtime.call(bot.shutdown())
        loop.close()
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
    loop = asyncio.new_event_loop()
    runtime = SimpleNamespace(call=loop.run_until_complete)
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
        loop.close()
        queue.close()


def test_invalid_json_response_is_not_treated_as_definite_rejection(tmp_path):
    queue = OutboundQueue(tmp_path)
    row_id, _ = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {})], lambda _: document)
    accepted = []
    manager = setup_manager(queue)
    def send(*args, **kwargs):
        accepted.append(True)
        return BaseRequest.parse_json_payload(b"not a valid Telegram response")
    manager._bot = SimpleNamespace(send_document=send)
    for _ in range(3):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
    assert len(accepted) == 1
    state, receipt, held = queue.connection.execute(
        "SELECT delivery_state, completion_receipt, delivery_hold FROM outbound_queue WHERE id=?", (row_id,)
    ).fetchone()
    assert state == "queued" and receipt is None and held.startswith("uncertain:")
    assert list(queue.media_dir.iterdir())
    queue.close()


@pytest.mark.parametrize("failure", [BadRequest("rejected attachment"), TimedOut("response lost")])
def test_supplement_failure_keeps_content_and_reconciles_primary(tmp_path, manager_factory, failure):
    db = manager_factory(tmp_path)
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager.channel = SimpleNamespace(db=db)
    context = manager._encode_queued_log_context(QueuedDbLogContext(message_with_group(), None))
    row_id, waiter = queue.enqueue_many([QueueRequest(
        "send_document", (42, b"archive bytes"), {"filename": "archive.zip", "caption": "x" * 2048}, context
    )], lambda _: document)
    primary = Mock(return_value=delivered_message())
    supplement = Mock(side_effect=failure)
    def send(*args, **kwargs):
        return (supplement if kwargs.get("reply_to_message_id") == 100 else primary)(*args, **kwargs)
    manager._bot = SimpleNamespace(send_document=send)
    with patch.object(db, "add_or_update_message_log", side_effect=RuntimeError("temporary MsgLog failure")):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
        assert waiter.result(timeout=1).message_id == 100
        state, receipt = queue.connection.execute(
            "SELECT delivery_state, completion_receipt FROM outbound_queue WHERE id=?", (row_id,)
        ).fetchone()
        assert state == "sent_pending"
        assert manager._decode_queued_completion_receipt(receipt)[0].message_id == 100
    # Restart after primary receipt and supplemental work have committed together.
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager.channel = SimpleNamespace(db=db)
    manager._bot = SimpleNamespace(send_document=send)
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET reconcile_after=0")
    for _ in range(4):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
    assert primary.call_count == 1 and supplement.call_count == 1
    assert db.get_msg_log(master_msg_id="42.100").slave_message_id == "source-1"
    [(payload, held)] = queue.connection.execute("SELECT payload, delivery_hold FROM outbound_queue").fetchall()
    assert held.startswith("supplement_failed:" if isinstance(failure, BadRequest) else "uncertain:")
    args, kwargs = queue.decode_payload(payload)
    assert kwargs["document"].read() == b"x" * 2048
    queue.close_payload_resources(queue.payload_closeables(args, kwargs))
    queue.close()


def test_primary_receipt_and_supplement_retry_atomic_commit_without_resending(tmp_path, monkeypatch):
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    row_id, waiter = queue.enqueue_many([QueueRequest(
        "send_document", (42, b"archive bytes"), {"caption": "x" * 2048}
    )], lambda _: document)
    primary = Mock(return_value=delivered_message())
    supplemental_calls = []
    def send(*args, **kwargs):
        if kwargs.get("reply_to_message_id") == 100:
            supplemental_calls.append(kwargs)
            return delivered_message()
        return primary(*args, **kwargs)
    manager._bot = SimpleNamespace(send_document=send)
    clock = [100.0]
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: clock[0])
    # Fail after the supplemental INSERT, before the primary receipt UPDATE.
    with queue.connection:
        queue.connection.execute("CREATE TRIGGER fail_completion BEFORE UPDATE OF completion_receipt ON outbound_queue "
                                 "BEGIN SELECT RAISE(FAIL, 'database unavailable'); END")
    manager._outbound_scheduler.dispatch_once()
    manager._outbound_scheduler.harvest_completed()
    assert not waiter.done() and not supplemental_calls
    manager._outbound_scheduler.dispatch_once()
    assert manager._outbound_scheduler.next_deadline == 101.0
    assert queue.connection.execute("SELECT id, delivery_hold FROM outbound_queue").fetchall() == [(row_id, "in_flight")]
    assert len(list(queue.media_dir.iterdir())) == 1
    with queue.connection:
        queue.connection.execute("DROP TRIGGER fail_completion")
    clock[0] += 2
    manager._outbound_scheduler.harvest_completed()
    assert waiter.result(timeout=1).message_id == 100
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(send_document=send)
    manager._outbound_scheduler.dispatch_once()
    manager._outbound_scheduler.harvest_completed()
    assert primary.call_count == 1 and len(supplemental_calls) == 1
    assert not queue.connection.execute("SELECT id FROM outbound_queue").fetchall()
    assert not list(queue.media_dir.iterdir())
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


@pytest.mark.parametrize("stage", ["get_file", "download", "missing_path"])
def test_acquisition_failure_retries_after_restart_without_delivery_uncertainty(tmp_path, monkeypatch, stage):
    from efb_telegram_master.outbound import HISTORY_REPLAY_KEY

    clock = [1000.0]
    monkeypatch.setattr("efb_telegram_master.outbound.time.time", lambda: clock[0])
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: clock[0])
    queue = OutboundQueue(tmp_path)
    source = tmp_path / "saved-media"
    source.write_bytes(b"saved media bytes")
    row_id, _ = queue.enqueue_many([QueueRequest("copy_message", (), {
        "chat_id": 42, "from_chat_id": 1, "message_id": 2, "_slave_id": "__history__:source",
        HISTORY_REPLAY_KEY: {"source_sender_bot_id": "owner", "fallback_operation": "send_document",
                             "fallback_kwargs": {"document": "saved-file"}},
    })], lambda _: lambda chat_id, from_chat_id, message_id: None)
    copies = Mock(side_effect=BadRequest("Message to copy not found"))
    uploads = Mock(return_value=delivered_message())
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(copy_message=copies, send_document=uploads)
    get_file = Mock(side_effect=TimedOut("getFile response lost")) if stage == "get_file" else Mock(
        return_value=SimpleNamespace(file_path="https://example.invalid/media" if stage == "download" else None))
    manager.get_file = get_file
    with patch("efb_telegram_master.bot_manager.httpx.stream", side_effect=httpx.ReadTimeout("download failed")):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
    assert copies.call_count == 1 and uploads.call_count == 0
    get_file.assert_called_once_with("saved-file", sender_bot_id="owner")
    assert queue.connection.execute(
        "SELECT delivery_hold, reconcile_attempts, reconcile_after FROM outbound_queue WHERE id=?", (row_id,)
    ).fetchone() == (None, 1, 1001.0)
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(copy_message=copies, send_document=uploads)
    manager.get_file = Mock(return_value=SimpleNamespace(file_path=str(source)))
    manager._outbound_scheduler.dispatch_once()
    assert copies.call_count == 1
    assert manager._outbound_scheduler.next_deadline == 1001.0
    clock[0] += 1
    manager._outbound_scheduler.dispatch_once()
    manager._outbound_scheduler.harvest_completed()
    assert copies.call_count == 2 and uploads.call_count == 1
    assert not queue.connection.execute("SELECT id FROM outbound_queue").fetchall()
    queue.close()


def test_transient_acquisition_recovers_after_repeated_failures_and_restart(tmp_path, monkeypatch):
    from efb_telegram_master.outbound import HISTORY_REPLAY_KEY

    clock = [1000.0]
    monkeypatch.setattr("efb_telegram_master.outbound.time.time", lambda: clock[0])
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: clock[0])
    queue = OutboundQueue(tmp_path)
    row_id, waiter = queue.enqueue_many([QueueRequest("copy_message", (), {
        "chat_id": 42, "from_chat_id": 1, "message_id": 2, "_slave_id": "__history__:source",
        HISTORY_REPLAY_KEY: {"source_sender_bot_id": "owner", "fallback_operation": "send_document",
                             "fallback_kwargs": {"document": "saved-file"}},
    })], lambda _: lambda chat_id, from_chat_id, message_id: None)
    manager = setup_manager(queue)
    copies = Mock(side_effect=BadRequest("Message to copy not found"))
    uploads = Mock(return_value=delivered_message())
    manager._bot = SimpleNamespace(copy_message=copies, send_document=uploads)
    manager.get_file = Mock(side_effect=TimedOut("getFile unavailable"))
    delays = []
    for _ in range(10):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
        assert not waiter.done()
        delays.append(manager._outbound_scheduler.next_deadline - clock[0])
        clock[0] = manager._outbound_scheduler.next_deadline
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60, 60]
    assert uploads.call_count == 0
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(copy_message=copies, send_document=uploads)
    source = tmp_path / "saved-media"
    source.write_bytes(b"saved media bytes")
    manager.get_file = Mock(return_value=SimpleNamespace(file_path=str(source)))
    manager._outbound_scheduler.dispatch_once()
    manager._outbound_scheduler.harvest_completed()
    assert copies.call_count == 11 and uploads.call_count == 1
    assert not queue.connection.execute("SELECT id FROM outbound_queue").fetchall()
    queue.close()


def test_long_edit_attachment_retries_presend_failure_and_holds_interrupted_send(tmp_path):
    queue = OutboundQueue(tmp_path)
    _, waiter = queue.enqueue_many([QueueRequest("edit_message_text", (), {
        "chat_id": 42, "message_id": 100, "text": "x" * 5000,
        "_required_sender_bot_id": "__main__", "_send_mode": "blocking",
    })], lambda _: lambda chat_id, message_id, text: None)
    manager = setup_manager(queue)
    primary = Mock(return_value=delivered_message())
    failure = NetworkError("connection establishment failed")
    failure.__cause__ = httpx.ConnectError("not sent")
    supplement = Mock(side_effect=[failure, delivered_message()])
    manager._bot = SimpleNamespace(edit_message_text=primary, send_document=supplement)
    manager.TRANSPORT_RETRY_SECONDS = 0
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    scheduler.harvest_completed()
    assert waiter.result(timeout=1).message_id == 100
    scheduler.dispatch_once()
    scheduler.harvest_completed()
    assert queue.connection.execute("SELECT delivery_hold FROM outbound_queue").fetchone() == (None,)
    scheduler.dispatch_once()
    assert supplement.call_count == 2
    # Crash after the supplemental request, before its receipt can be harvested.
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(edit_message_text=primary, send_document=supplement)
    manager._outbound_scheduler.dispatch_once()
    assert primary.call_count == 1 and supplement.call_count == 2
    assert queue.connection.execute("SELECT delivery_hold FROM outbound_queue").fetchone() == ("in_flight",)
    assert list(queue.media_dir.iterdir())
    queue.close()


@pytest.mark.parametrize("operation,result", [
    ("send_document", SimpleNamespace(message_id=100, unpickleable=lambda: None)),
    ("edit_message_text", True),
])
def test_unusable_primary_receipt_retains_full_content_without_occupying_worker(tmp_path, operation, result):
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    if operation == "send_document":
        request = QueueRequest(operation, (42, b"archive bytes"), {"caption": "x" * 2048})
        resolver = document
    else:
        request = QueueRequest(operation, (), {"chat_id": 42, "message_id": 100, "text": "x" * 5000,
                                              "_required_sender_bot_id": "__main__"})
        resolver = lambda chat_id, message_id, text: None
    row_id, waiter = queue.enqueue_many([request], lambda _: resolver)
    primary = Mock(return_value=result)
    later = Mock(return_value=delivered_message())
    manager._bot = SimpleNamespace(**{operation: primary, "send_message": later})
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    scheduler.harvest_completed()
    with pytest.raises(DeliveryUncertainError):
        waiter.result(timeout=1)
    assert not scheduler.in_flight and not scheduler.stopping
    payload, receipt, held = queue.connection.execute(
        "SELECT payload, completion_receipt, delivery_hold FROM outbound_queue WHERE id=?", (row_id,)
    ).fetchone()
    assert receipt is None and held.startswith("uncertain:InvalidTelegramResponseError")
    _, kwargs = queue.decode_payload_raw(payload)
    assert kwargs.get("text", kwargs.get("caption")) in ("x" * 2048, "x" * 5000)
    _, next_waiter = queue.enqueue_many([QueueRequest("send_message", (42, "later"), {})],
                                      lambda _: lambda chat_id, text: None)
    scheduler.dispatch_once()
    scheduler.harvest_completed()
    assert next_waiter.result(timeout=1).message_id == 100 and primary.call_count == 1
    queue.close()


@pytest.mark.parametrize("rejection", [BadRequest("File not found"), Forbidden("Bot was blocked")])
def test_permanent_acquisition_rejection_is_retained_without_retry(tmp_path, rejection):
    from efb_telegram_master.outbound import HISTORY_REPLAY_KEY

    queue = OutboundQueue(tmp_path)
    row_id, waiter = queue.enqueue_many([QueueRequest("copy_message", (), {
        "chat_id": 42, "from_chat_id": 1, "message_id": 2, "_slave_id": "__history__:source",
        HISTORY_REPLAY_KEY: {"source_sender_bot_id": "owner", "fallback_operation": "send_document",
                             "fallback_kwargs": {"document": "saved-file"}},
    })], lambda _: lambda chat_id, from_chat_id, message_id: None)
    manager = setup_manager(queue)
    copies = Mock(side_effect=BadRequest("Message to copy not found"))
    uploads = Mock()
    manager._bot = SimpleNamespace(copy_message=copies, send_document=uploads)
    manager.get_file = Mock(side_effect=rejection)
    for _ in range(3):
        manager._outbound_scheduler.dispatch_once()
        manager._outbound_scheduler.harvest_completed()
    assert waiter.exception(timeout=1) is not None
    assert queue.connection.execute("SELECT delivery_hold FROM outbound_queue WHERE id=?", (row_id,)).fetchone()[0].startswith("history_failed:")
    assert copies.call_count == 1 and uploads.call_count == 0
    queue.close()


@pytest.mark.parametrize("operation", ["send_document", "edit_message_text"])
def test_confirming_primary_after_crash_preserves_pending_attachment(tmp_path, operation):
    queue = OutboundQueue(tmp_path)
    if operation == "send_document":
        request = QueueRequest(operation, (42, b"archive bytes"), {"caption": "x" * 2048})
        resolver = document
    else:
        request = QueueRequest(operation, (), {"chat_id": 42, "message_id": 100, "text": "x" * 5000,
                                              "_required_sender_bot_id": "__main__"})
        resolver = lambda chat_id, message_id, text: None
    row_id, _ = queue.enqueue_many([request], lambda _: resolver)
    manager = setup_manager(queue)
    primary = Mock(return_value=delivered_message())
    attachments = []
    def send(*args, **kwargs):
        if kwargs.get("reply_to_message_id") == 100:
            attachments.append(kwargs["document"].input_file_content.read())
            return delivered_message()
        return primary(*args, **kwargs)
    sender = SimpleNamespace(send_document=send, edit_message_text=send)
    manager._bot = sender
    manager._outbound_scheduler.dispatch_once()
    # The primary ACK reached the process, but its durable transaction did not run.
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager._bot = sender
    manager._outbound_scheduler.dispatch_once()
    assert primary.call_count == 1 and not attachments
    assert manager.confirm_queued_delivery(row_id, delivered_message())
    assert primary.call_count == 1 and not attachments
    manager._outbound_scheduler.dispatch_once()
    manager._outbound_scheduler.harvest_completed()
    assert primary.call_count == 1
    assert attachments == [b"x" * (2048 if operation == "send_document" else 5000)]
    assert not queue.connection.execute("SELECT id FROM outbound_queue").fetchall()
    assert not list(queue.media_dir.iterdir())
    queue.close()


@pytest.mark.parametrize("path", ["fallback", "ordinary_copy", "copy_after_retry"])
def test_history_confirmation_after_crash_uses_last_attempted_operation(tmp_path, path):
    from efb_telegram_master.outbound import HISTORY_REPLAY_KEY

    source = tmp_path / "saved-media"
    source.write_bytes(b"saved media bytes")
    queue = OutboundQueue(tmp_path)
    row_id, _ = queue.enqueue_many([QueueRequest("copy_message", (), {
        "chat_id": 42, "from_chat_id": 1, "message_id": 2, "_slave_id": "__history__:source",
        HISTORY_REPLAY_KEY: {"source_sender_bot_id": "owner", "fallback_operation": "send_document",
                             "fallback_kwargs": {"document": "saved-file", "caption": "x" * 2048}},
    })], lambda _: lambda chat_id, from_chat_id, message_id: None)
    copies = Mock(side_effect=(
        [delivered_message()] if path == "ordinary_copy" else
        [BadRequest("Message to copy not found"), delivered_message()]
    ))
    primary_uploads = []
    attachments = []
    def send(*args, **kwargs):
        contents = kwargs["document"].input_file_content.read()
        if kwargs.get("reply_to_message_id") == 100:
            attachments.append(contents)
        else:
            primary_uploads.append(contents)
            if path == "copy_after_retry":
                raise RetryAfter(0)
        return delivered_message()
    sender = SimpleNamespace(copy_message=copies, send_document=send)
    manager = setup_manager(queue)
    manager._bot = sender
    manager.get_file = Mock(return_value=SimpleNamespace(file_path=str(source)))
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    if path == "copy_after_retry":
        scheduler.harvest_completed()
        scheduler.dispatch_once()
    # Lose the process after the ACK, before harvesting the primary receipt.
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager._bot = sender
    manager._outbound_scheduler.dispatch_once()
    assert manager.confirm_queued_delivery(row_id, delivered_message())
    manager._outbound_scheduler.dispatch_once()
    manager._outbound_scheduler.harvest_completed()
    assert attachments == ([b"x" * 2048] if path == "fallback" else [])
    assert primary_uploads == ([] if path == "ordinary_copy" else [b"saved media bytes"])
    assert copies.call_count == (2 if path == "copy_after_retry" else 1)
    assert not queue.connection.execute("SELECT id FROM outbound_queue").fetchall()
    assert not list(queue.media_dir.iterdir())
    queue.close()


@pytest.mark.parametrize("timeout", [0, 1.1])
def test_shutdown_releases_accepted_future_with_unpersistable_receipt(tmp_path, timeout):
    from efb_telegram_master.etm_metrics import Metrics
    from efb_telegram_master.outbound import SchedulerStoppedError

    metrics = Metrics()
    queue = OutboundQueue(tmp_path, metrics=metrics)
    manager = setup_manager(queue)
    row_id, waiter = queue.enqueue_many([QueueRequest(
        "send_document", (42, b"archive bytes"), {"caption": "x" * 2048}
    )], lambda _: document)
    primary = Mock(return_value=delivered_message())
    manager._bot = SimpleNamespace(send_document=primary)
    with queue.connection:
        queue.connection.execute("CREATE TRIGGER fail_completion BEFORE UPDATE OF completion_receipt ON outbound_queue "
                                 "BEGIN SELECT RAISE(FAIL, 'database unavailable'); END")
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    scheduler.harvest_completed()
    assert scheduler.in_flight[row_id].future.done() and not waiter.done()
    scheduler.stop_and_drain(timeout=timeout)
    with pytest.raises(SchedulerStoppedError):
        waiter.result(timeout=1)
    assert not scheduler.in_flight and not scheduler.in_flight_destinations and not queue.waiters
    assert scheduler._permits.acquire(blocking=False)
    scheduler._permits.release()
    samples = [sample for family in metrics.in_flight.collect() for sample in family.samples]
    assert samples and all(sample.value == 0 for sample in samples)
    assert queue.connection.execute("SELECT id, delivery_hold, completion_receipt FROM outbound_queue").fetchall() == [
        (row_id, "in_flight", None)
    ]
    assert len(list(queue.media_dir.iterdir())) == 1
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager._bot = SimpleNamespace(send_document=primary)
    manager._outbound_scheduler.dispatch_once()
    assert primary.call_count == 1
    assert queue.connection.execute("SELECT id FROM outbound_queue").fetchall() == [(row_id,)]
    queue.close()


def test_receipt_recovery_during_drain_settles_primary_and_preserves_attachment(tmp_path, manager_factory):
    from efb_telegram_master.etm_metrics import Metrics

    db = manager_factory(tmp_path)
    metrics = Metrics()
    queue = OutboundQueue(tmp_path, metrics=metrics)
    manager = setup_manager(queue)
    manager.channel = SimpleNamespace(db=db)
    context = manager._encode_queued_log_context(QueuedDbLogContext(message_with_group(), None))
    row_id, waiter = queue.enqueue_many([QueueRequest(
        "send_document", (42, b"archive bytes"), {"caption": "x" * 2048}, context
    )], lambda _: document)
    result = delivered_message()
    primary = Mock(return_value=result)
    manager._bot = SimpleNamespace(send_document=primary)
    with queue.connection:
        queue.connection.execute("CREATE TRIGGER fail_completion BEFORE UPDATE OF completion_receipt ON outbound_queue "
                                 "BEGIN SELECT RAISE(FAIL, 'database unavailable'); END")
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    scheduler.harvest_completed()
    assert scheduler.in_flight[row_id].future.done() and not waiter.done()
    with queue.connection:
        queue.connection.execute("DROP TRIGGER fail_completion")
    scheduler.stop_and_drain(timeout=1.2)
    assert waiter.result(timeout=0) is result
    assert db.get_msg_log(master_msg_id="42.100").slave_message_id == "source-1"
    assert scheduler.failure is None
    assert not scheduler.in_flight and not scheduler.in_flight_destinations and not queue.waiters
    assert scheduler._permits.acquire(blocking=False)
    scheduler._permits.release()
    # Repeated shutdown/harvest must not publish a second primary completion.
    scheduler.harvest_completed()
    scheduler.stop_and_drain(timeout=0)
    completions = [sample for family in metrics.completions.collect() for sample in family.samples
                   if sample.name.endswith("_total")]
    assert len(completions) == 1 and completions[0].value == 1
    assert completions[0].labels["outcome"] == "success"
    assert all(sample.value == 0 for family in metrics.in_flight.collect() for sample in family.samples)
    [(child_id, state, payload)] = queue.connection.execute(
        "SELECT id, delivery_state, payload FROM outbound_queue"
    ).fetchall()
    assert child_id != row_id and state == "queued"
    args, kwargs = queue.decode_payload(payload)
    assert kwargs["reply_to_message_id"] == result.message_id
    assert kwargs["document"].read() == b"x" * 2048
    queue.close_payload_resources(queue.payload_closeables(args, kwargs))
    assert primary.call_count == 1
    queue.close()


def test_main_replacement_receipt_replaces_auxiliary_snapshot_identity(tmp_path, manager_factory):
    from efb_telegram_master.outbound import SenderSelection

    db = manager_factory(tmp_path)
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager.channel = SimpleNamespace(db=db)
    message = message_with_group()
    message.sender_bot_id = "901"
    message.file_bot_id = "901"
    message.file_id = "aux-original-id"
    db.add_or_update_message_log(message, delivered_message(901), sender_bot_id="901")
    context = manager._encode_queued_log_context(QueuedDbLogContext(message, (42, 100)))
    row_id, _ = queue.enqueue_many([QueueRequest("send_document", (42, b"replacement"), {}, context)], lambda _: document)
    receipt = Message.de_json({**delivered_message().to_dict(), "message_id": 101,
                              "document": {"file_id": "main-replacement-id", "file_unique_id": "replacement"}}, None)
    queue.record_telegram_completion(row_id, manager.encode_queued_completion_receipt(receipt, SenderSelection(None, None)))
    try:
        assert row_id in manager._outbound_scheduler.reconcile_sent_pending(row_id)
        stored = db.get_msg_log(master_msg_id="42.100")
        assert (stored.master_msg_id_alt, stored.file_id, stored.sender_bot_id, stored.file_bot_id) == (
            "42.101", "main-replacement-id", None, None,
        )
    finally:
        queue.close()


def test_main_reply_confirmation_preserves_file_issuer_through_restart(tmp_path, manager_factory):
    from efb_telegram_master import TelegramChannel
    from efb_telegram_master.chat_binding import ChatBindingManager
    from efb_telegram_master.outbound import HISTORY_REPLAY_KEY

    db = manager_factory(tmp_path)
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager.channel = SimpleNamespace(db=db)
    source = message_with_group()
    context = manager._encode_queued_log_context(QueuedDbLogContext(source, None))
    row_id, _ = queue.enqueue_many([QueueRequest("send_document", (42, b"archive bytes"), {}, context)], lambda _: document)
    with queue.connection:
        queue.connection.execute("UPDATE outbound_queue SET delivery_hold='in_flight', attempt_sender_bot_id='901' WHERE id=?", (row_id,))
    # This is the MAIN bot's incoming Update. The nested author is auxiliary,
    # but the nested file ID is issued to the main bot observing the reply.
    update = Update.de_json({"update_id": 1, "message": {
        "message_id": 101, "date": 1, "chat": {"id": 42, "type": "supergroup"},
        "from": {"id": 77, "is_bot": False, "first_name": "Admin"},
        "text": f"/confirm_send {row_id}", "reply_to_message": delivered_message(901).to_dict(),
    }}, Bot("900:test-token"))
    channel = SimpleNamespace(config={"admins": [77]}, bot_manager=manager)
    with patch("efb_telegram_master.sync_reply_text"), \
            patch.object(db, "add_or_update_message_log", side_effect=RuntimeError("temporary DB outage")):
        TelegramChannel.confirm_send(channel, update, SimpleNamespace(args=[str(row_id)]))
    assert queue.connection.execute("SELECT delivery_state FROM outbound_queue").fetchone() == ("sent_pending",)
    queue.close()
    queue = OutboundQueue(tmp_path)
    manager = setup_manager(queue)
    manager.channel = SimpleNamespace(db=db)
    try:
        with queue.connection:
            queue.connection.execute("UPDATE outbound_queue SET reconcile_after=0")
        assert row_id in manager._outbound_scheduler.reconcile_sent_pending(row_id)
        stored = db.get_msg_log(master_msg_id="42.100")
        assert (stored.sender_bot_id, stored.file_bot_id, stored.file_id) == ("901", "__main__", "file-id")
        cache = SimpleNamespace(get_chat=lambda *a, **kw: source.chat,
                                get_chat_member=lambda *a, **kw: source.author)
        restored = stored.build_etm_msg(cache)
        assert restored.sender_bot_id == "901" and restored.file_bot_id == "__main__"
        snapshot, _ = manager._decode_queued_log_context(manager._encode_queued_log_context(QueuedDbLogContext(restored)))
        assert snapshot.file_bot_id == "__main__"
        binding = SimpleNamespace(db=db)
        _, kwargs = ChatBindingManager._prepare_history_migration_call(binding, SimpleNamespace(
            formatted_text=None, source_master_msg_id="42.100"), 43, None)
        assert kwargs[HISTORY_REPLAY_KEY]["source_sender_bot_id"] == "__main__"
        manager._bot = SimpleNamespace(get_file=Mock(side_effect=BadRequest("test stops before download")))
        manager.bot_pool = SimpleNamespace(get_bot_by_id=Mock(side_effect=AssertionError("aux must not acquire main file")))
        with patch("efb_telegram_master.message.coordinator", SimpleNamespace(master=SimpleNamespace(bot_manager=manager))):
            restored._load_file()
        manager._bot.get_file.assert_called_once_with("file-id")
    finally:
        queue.close()


@pytest.mark.parametrize("sender_kwargs, expected", [({}, "901"), ({"sender_bot_id": None}, None),
                                                    ({"sender_bot_id": "902"}, "902")])
def test_msglog_distinguishes_omitted_sender_from_main(tmp_path, manager_factory, sender_kwargs, expected):
    db = manager_factory(tmp_path)
    message = message_with_group()
    message.sender_bot_id = "901"
    db.add_or_update_message_log(message, delivered_message(901), **sender_kwargs)
    assert db.get_msg_log(master_msg_id="42.100").sender_bot_id == expected


@pytest.mark.parametrize("media", ["document", "photo"])
def test_fresh_media_discards_previous_file_owner_override(media):
    from efb_telegram_master.msg_type import get_msg_type

    message = message_with_group()
    message.sender_bot_id = "901"
    message.file_bot_id = "__main__"
    message.file_id = "old-observed-id"
    data = delivered_message(901).to_dict()
    attachment = {"file_id": "fresh-aux-id", "file_unique_id": "fresh-unique-id"}
    data.pop("document")
    data[media] = attachment if media == "document" else [{**attachment, "width": 1, "height": 1}]
    receipt = Message.de_json(data, None)
    message.type_telegram = get_msg_type(receipt)
    message.put_telegram_file(receipt)
    assert message.file_id == "fresh-aux-id" and message.file_bot_id is None
    assert message.sender_bot_id == "901"
    manager = manager_adapter()
    owner = SimpleNamespace(disabled=False, bot=SimpleNamespace(
        get_file=Mock(side_effect=BadRequest("stop before download")),
    ))
    manager.bot_pool = SimpleNamespace(get_bot_by_id=lambda bot_id: owner if bot_id == "901" else None)
    manager._bot = SimpleNamespace(get_file=Mock(side_effect=AssertionError("main does not own fresh media")))
    with patch("efb_telegram_master.message.coordinator", SimpleNamespace(master=SimpleNamespace(bot_manager=manager))):
        message._load_file()
    owner.bot.get_file.assert_called_once_with("fresh-aux-id")
