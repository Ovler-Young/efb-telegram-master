import copy
import threading
from types import SimpleNamespace
from unittest.mock import patch, Mock
import datetime

import pytest
from telegram.error import NetworkError
from telegram import Document, Message, Chat
from ehforwarderbot import MsgType, Channel
from ehforwarderbot.status import MessageRemoval
from ehforwarderbot.message import LinkAttribute, MessageCommand, MessageCommands

from efb_telegram_master.aggregate import make_source_member, member_message
from efb_telegram_master.db import MsgLog
from efb_telegram_master.live_aggregate import LiveTextAggregation
from efb_telegram_master.outbound import OutboundQueue, OutboundQueueScheduler, QueueRequest
from efb_telegram_master.queued_log import decode_aggregation
from efb_telegram_master.slave_message import SlaveMessageProcessor
from efb_telegram_master.utils import chat_id_to_str
from tests.unit.test_live_aggregate import source, SourceCache
from tests.unit.test_live_aggregate_runtime import runtime, append, complete


def restart(manager):
    queue_path = manager._outbound_queue.path.parent
    executor = manager._outbound_scheduler.executor
    manager._outbound_queue.close()
    manager._outbound_queue = OutboundQueue(queue_path)
    manager._outbound_scheduler = OutboundQueueScheduler(manager._outbound_queue, manager, executor, 1)
    manager.live_aggregation = LiveTextAggregation(manager)


def processor(manager):
    result = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    result.bot = manager
    result.channel = manager.channel
    result.flag = manager.channel.flag
    result.db = manager.channel.db
    result.chat_manager = SourceCache()
    result.chat_manager.update_chat_obj = lambda chat: chat
    result.logger = manager.logger
    result._pending_slave_messages = set()
    result._pending_slave_messages_lock = threading.Lock()
    result.get_slave_msg_dest = lambda msg: ("Alice:", (-100, None))
    result.is_silent = lambda msg: False
    manager.send_chat_action = lambda *a, **kw: None
    manager._cleanup_tls = threading.local()
    return result


def remove(handler, uid):
    message = source(uid)
    message.author = None
    handler.send_status(MessageRemoval(Mock(spec=Channel), Mock(spec=Channel), message))


def edit(processor, uid, text):
    message = source(uid, text)
    message.chat.members.append(message.author)
    message.edit = True
    processor.send_message(message)


def test_pending_and_inflight_source_edit_removal_recover(runtime):
    manager = runtime
    handler = processor(manager)
    key, first = append(manager, "one", 1)
    append(manager, "two", 1.1, "second")
    edit(handler, "one", "pending edited")
    remove(handler, "two")
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    next(iter(scheduler.in_flight.values())).future.result(timeout=2)
    edit(handler, "one", "edited during first send")
    scheduler.harvest_completed()
    restart(manager)
    complete(manager)
    row = MsgLog.get()
    assert len(row.aggregate["children"]) == 1
    assert row.text == "Alice:\nedited during first send"
    assert row.aggregate["children"][0]["source_revision"] == 3
    remove(handler, "one")
    complete(manager)
    removed = MsgLog.get().aggregate["children"][0]
    assert removed["status"] == "removed"
    assert member_message(removed).text == "edited during first send"
    append(manager, "empty", 10)
    assert manager.live_aggregation.remove_source_member(key, (key[0], "empty"))
    scheduler = manager._outbound_scheduler
    scheduler.dispatch_once()
    assert not scheduler.in_flight
    assert len(manager.transport.calls) == 3


def test_independent_boundary_survives_restart(runtime):
    manager = runtime
    key, _ = append(manager, "one", 1)
    with manager.live_aggregation.independent(key):
        manager._enqueue_requests([QueueRequest("send_message", (), {"chat_id": -100, "text": "media"})])
    restart(manager)
    append(manager, "three", 1.2)
    complete(manager)
    complete(manager)
    complete(manager)
    assert [call[2] for call in manager.transport.calls] == ["Alice:\nbody", "media", "Alice:\nbody"]
    assert [row.aggregate["children"][0]["source_id"] for row in MsgLog.select().order_by(MsgLog.master_msg_id)] == ["one", "three"]


@pytest.mark.parametrize("middle_type", [MsgType.Text, MsgType.Link])
def test_topic_association_change_closes_the_former_source_stream(runtime, monkeypatch, middle_type):
    manager = runtime
    manager.channel.flag.config["text_aggregation"] = True
    handler = processor(manager)
    handler.get_slave_msg_dest = lambda msg: ("Alice:", (-100, manager.channel.db.get_topic_thread_id(
        chat_id_to_str(chat=msg.chat), -100)))
    arrivals = {"one": 1, "other": 2, "one-follow": 3, "two": 5, "three": 9}
    monkeypatch.setattr("efb_telegram_master.aggregate.make_source_member", lambda msg, **kwargs:
                        make_source_member(msg, received_time=datetime.datetime.fromtimestamp(arrivals[str(msg.uid)]),
                                           **kwargs))
    for uid, topic in (("one", 7), ("other", 9), ("one-follow", 7), ("two", 8), ("three", 7)):
        message = source(uid)
        if uid == "other":
            message.chat.uid = "other-group"
        if uid == "two" and middle_type == MsgType.Link:
            message.type = MsgType.Link
            message.attributes = LinkAttribute("Link", url="https://example.com/")
        message.chat.members.append(message.author)
        manager.channel.db.add_topic_assoc(-100, topic, chat_id_to_str(chat=message.chat))
        handler.send_message(message)
        complete(manager)

    rows = list(MsgLog.select().order_by(MsgLog.master_msg_id))
    expected = [("7", ["one", "one-follow"]), ("9", ["other"])]
    if middle_type == MsgType.Text:
        expected.append(("8", ["two"]))
    expected.append(("7", ["three"]))
    assert [
        (row.master_message_thread_id, [member["source_id"] for member in row.aggregate["children"]])
        for row in rows if row.aggregate
    ] == expected
    assert manager.transport.messages[3].message_thread_id == 8


def test_fixed_owner_update_uncertainty_survives_restart(runtime):
    manager = runtime
    key, member = append(manager, "one", 1)
    complete(manager)
    member = copy.deepcopy(member)
    member["source_revision"] = 2
    parent = manager.live_aggregation.queue_container_update("-100.1", [member], key=key)
    manager.transport.error = NetworkError("response lost")
    manager._outbound_scheduler.dispatch_once()
    submitted = next(iter(manager._outbound_scheduler.in_flight.values()))
    with pytest.raises(NetworkError):
        submitted.future.result(timeout=2)
    manager._outbound_scheduler.harvest_completed()
    restart(manager)
    member["source_revision"] = 3
    child = manager.live_aggregation.queue_container_update("-100.1", [member], key=key)
    assert child not in [row.id for row in manager._outbound_queue.heads(ready_only=True)]
    assert manager._outbound_queue.contains(parent)


def test_redirect_confirmation_survives_hint_failure_and_newer_edit(runtime):
    manager = runtime
    handler = processor(manager)
    key, _ = append(manager, "one", 1)
    append(manager, "two", 1.1, "other saved source")
    complete(manager)
    registered = {}
    manager.channel.commands = SimpleNamespace(register_command=lambda receipt, context: registered.setdefault(receipt.message_id, context))
    message = source("one", "new body and action")
    message.chat.members.append(message.author)
    message.edit = True
    message.commands = MessageCommands([MessageCommand(name="Action", callable_name="action")])
    handler.send_message(message)
    # A subsequent plain-text edit retains the pending independent route.
    edit(handler, "one", "latest independent version")
    module = SimpleNamespace(action=lambda: "executed")
    with patch("ehforwarderbot.coordinator.get_module_by_id", return_value=module):
        complete(manager)
    assert manager.transport.calls[-1][3]["reply_markup"].inline_keyboard[0][0].text == "Action"
    assert registered[2].body == "new body and action"
    old = manager.channel.db.get_msg_log(master_msg_id="-100.1")
    assert old.aggregate["children"][0]["status"] == "redirected"
    assert old.text.endswith("other saved source")
    # Make the original owner's hint fail after the new output was finalized.
    original_edit = manager.transport.edit_message_text
    def fail_hint(text, chat_id, message_id, parse_mode=None, **kwargs):
        if message_id == 1:
            raise NetworkError("hint response lost")
        return original_edit(text, chat_id, message_id, parse_mode, **kwargs)
    manager.transport.edit_message_text = fail_hint
    manager._outbound_scheduler.dispatch_once()
    submitted = next(iter(manager._outbound_scheduler.in_flight.values()))
    # The newer standalone edit was queued first; it serializes behind the replacement receipt.
    submitted.future.result(timeout=2)
    manager._outbound_scheduler.harvest_completed()
    resolved, member = manager.channel.db.resolve_source_member(key[0], "one", "-100")
    assert resolved.master_msg_id == "-100.2"
    assert member["source_revision"] == 3
    assert member_message(member).text == "latest independent version"
    manager._outbound_scheduler.dispatch_once()
    submitted = next(iter(manager._outbound_scheduler.in_flight.values()))
    with pytest.raises(NetworkError):
        submitted.future.result(timeout=2)
    manager._outbound_scheduler.harvest_completed()
    assert manager.channel.db.resolve_source_member(key[0], "one", "-100")[0].master_msg_id == "-100.2"
    assert manager.channel.db.get_msg_log(master_msg_id="-100.1").aggregate["confirmed_text"] == old.text
    restart(manager)
    edit(handler, "one", "editable after failed hint")
    complete(manager)
    assert member_message(manager.channel.db.resolve_source_member(key[0], "one", "-100")[1]).text == "editable after failed hint"


def test_pending_qualification_split_preserves_source_order(runtime):
    manager = runtime
    handler = processor(manager)
    key, _ = append(manager, "before", 1)
    append(manager, "split", 1.1)
    append(manager, "after", 1.2, "after body")
    changed = source("split", "independent body")
    changed.chat.members.append(changed.author)
    changed.edit = True
    changed.commands = MessageCommands([MessageCommand(name="Action", callable_name="action")])
    handler.send_message(changed)
    restart(manager)
    manager.channel.commands = SimpleNamespace(register_command=lambda *a: None)
    with patch("ehforwarderbot.coordinator.get_module_by_id", return_value=SimpleNamespace()):
        complete(manager)
        complete(manager)
        complete(manager)
    assert [call[2] for call in manager.transport.calls] == ["Alice:\nbody", "Alice:\nindependent body", "Alice:\nafter body"]
    assert manager.channel.db.resolve_source_member(key[0], "split", "-100")[1]["source_revision"] == 2
    assert [child["source_id"] for row in MsgLog.select() if row.aggregate for child in row.aggregate["children"]] == ["before", "after"]


@pytest.mark.parametrize("late_after_confirmation", [False, True])
def test_split_recovery_keeps_preexisting_media_after_split_members(runtime, late_after_confirmation):
    manager = runtime
    handler = processor(manager)
    append(manager, "before", 1)
    key, _ = append(manager, "split", 1.1)
    append(manager, "after", 1.2, "after body")
    with manager.live_aggregation.independent(key):
        manager._enqueue_requests([QueueRequest("send_message", (), {"chat_id": -100, "text": "media"})])
    changed = source("split", "independent body")
    changed.chat.members.append(changed.author)
    changed.edit = True
    changed.commands = MessageCommands([MessageCommand(name="Action", callable_name="action")])
    handler.send_message(changed)
    restart(manager)
    manager.channel.commands = SimpleNamespace(register_command=lambda *a: None)
    with patch("ehforwarderbot.coordinator.get_module_by_id", return_value=SimpleNamespace()):
        if late_after_confirmation:
            for _ in range(3):
                complete(manager)
        append(manager, "late", 1.3, "late text")
        for _ in range(2 if late_after_confirmation else 5):
            complete(manager)
    assert [call[2] for call in manager.transport.calls] == [
        "Alice:\nbody", "Alice:\nindependent body", "Alice:\nafter body", "media", "Alice:\nlate text",
    ]


def test_minimum_removal_resolves_pending_independent_source(runtime, tmp_path):
    manager = runtime
    handler = processor(manager)
    key, _ = append(manager, "split", 1)
    manager.transport.send_document = lambda chat_id, document, caption=None, **kwargs: None
    path = tmp_path / "source.txt"
    path.write_bytes(b"complete attachment content")
    changed = source("split", "independent body")
    changed.chat.members.append(changed.author)
    changed.edit = True
    changed.type = MsgType.File
    changed.file = path.open("rb")
    changed.path = path
    changed.filename = "source.txt"
    changed.mime = "text/plain"
    handler.send_message(changed)
    assert list(manager._outbound_queue.media_dir.iterdir())
    remove(handler, "split")
    assert not list(manager._outbound_queue.media_dir.iterdir())
    assert path.read_bytes() == b"complete attachment content"
    restart(manager)
    _, member, _ = manager.live_aggregation.source_state(key, (key[0], "split"))
    assert member["status"] == "removed"
    assert member_message(member).text == "independent body"
    manager._outbound_scheduler.dispatch_once()
    assert not manager.transport.calls


def test_pending_withdrawal_survives_restart_binding_change_and_explicit_edit(runtime):
    manager = runtime
    manager.channel.flag.config["text_aggregation"] = True
    handler = processor(manager)
    key, _ = append(manager, "withdrawn", 1, "saved withdrawn body")
    remove(handler, "withdrawn")
    restart(manager)
    _, member, predecessor = manager.live_aggregation.source_state(key, (key[0], "withdrawn"))
    assert member["status"] == "removed" and member["source_revision"] == 2
    assert member_message(member).text == "saved withdrawn body" and predecessor is None
    append(manager, "withdrawn", 10, "stale duplicate")
    manager.channel.flag.config["text_aggregation"] = False
    handler.get_slave_msg_dest = lambda msg: ("Alice:", (-101, 4))
    handler.send_message(source("withdrawn", "redelivered after binding change"))
    manager._outbound_scheduler.dispatch_once()
    assert not manager.transport.calls and MsgLog.select().count() == 0
    # An explicit newer source update is accepted durably before publication.
    handler.get_slave_msg_dest = lambda msg: ("Alice:", (-100, None))
    edit(handler, "withdrawn", "explicitly restored body")
    restart(manager)
    _, newer, _ = manager.live_aggregation.source_state(key, (key[0], "withdrawn"))
    assert newer["status"] == "active" and newer["source_revision"] == 3
    complete(manager)
    assert manager.transport.calls[-1][2] == "Alice:\nexplicitly restored body"
    assert manager.channel.db.resolve_source_member(key[0], "withdrawn", "-100")[1]["source_revision"] == 3


def test_oversize_redirect_keeps_full_content_attachment_before_successor(runtime):
    manager = runtime
    handler = processor(manager)
    key, _ = append(manager, "one", 1)
    append(manager, "two", 1.1, "other body")
    complete(manager)
    documents = []
    def send_document(chat_id, document, caption=None, **kwargs):
        content = document.read() if hasattr(document, "read") else document.input_file_content
        if hasattr(content, "read"):
            content = content.read()
        documents.append(content)
        manager.transport.calls.append(("send_document", chat_id, caption, kwargs))
        number = len(manager.transport.messages) + 1
        receipt = Message(number, datetime.datetime.now(datetime.timezone.utc), Chat(chat_id, "supergroup"),
                          document=Document("document-file", "document-unique", file_name="full.html", mime_type="text/html"))
        manager.transport.messages[number] = receipt
        return receipt
    manager.transport.send_document = send_document
    long_body = "full source body " * 16875  # 270,000 characters exceed the saved-payload limit.
    edit(handler, "one", long_body)
    append(manager, "after", 10, "next text")
    complete(manager)
    resolved, member = manager.channel.db.resolve_source_member(key[0], "one", "-100")
    assert member_message(member).text == long_body
    restart(manager)
    # The full-content child remains the durable publication barrier after restart.
    complete(manager)
    assert manager.transport.calls[-1][0] == "send_document"
    assert long_body.encode() in documents[0]
    old = MsgLog.get_by_id("-100.1")
    assert member_message(old.aggregate["children"][0]).text == long_body
    assert old.text == "Alice:\nbody\n\nAlice:\nother body"
    assert any(decode_aggregation(row.log_context).get("routing_hint")
               for row in manager._outbound_queue.aggregation_rows())
    restart(manager)
    complete(manager)
    assert manager.transport.calls[-1][2] == "Alice:\nnext text"
    complete(manager)
    old = MsgLog.get_by_id("-100.1")
    assert [child["status"] for child in old.aggregate["children"]] == ["redirected", "active"]
    assert old.text == "Alice:\n[message moved]\n\nAlice:\nother body"
    assert len(documents) == 1
    assert [call[0] for call in manager.transport.calls].count("send_message") == 3
    # Other members continue to update the original container.
    edit(handler, "two", "edited survivor")
    complete(manager)
    assert manager.channel.db.resolve_source_member(key[0], "two", "-100")[0].master_msg_id == "-100.1"
    assert MsgLog.get_by_id("-100.1").text == "Alice:\n[message moved]\n\nAlice:\nedited survivor"
    append(manager, "unrelated", 20, "other topic still schedules", topic=7)
    complete(manager)
    assert manager.transport.messages[5].text == "Alice:\nother topic still schedules"
    assert not manager._outbound_queue.aggregation_rows()


def test_file_redirect_persists_source_type_and_actual_file_owner(runtime, tmp_path):
    manager = runtime
    handler = processor(manager)
    key, _ = append(manager, "one", 1)
    complete(manager)
    manager.sender_id = "700"
    uploaded = []
    def send_document(chat_id, document, caption=None, **kwargs):
        content = document.read() if hasattr(document, "read") else document.input_file_content
        uploaded.append(content.read() if hasattr(content, "read") else content)
        receipt = Message(2, datetime.datetime.now(datetime.timezone.utc), Chat(chat_id, "supergroup"), caption=caption,
                          document=Document("real-file", "real-unique", file_name="source.txt", mime_type="text/plain"))
        manager.transport.messages[2] = receipt
        manager.transport.calls.append(("send_document", chat_id, caption, kwargs))
        return receipt
    manager.transport.send_document = send_document
    path = tmp_path / "source.txt"
    path.write_bytes(b"complete attachment content")
    message = source("one", "file body")
    message.type = MsgType.File
    message.edit = True
    message.chat.members.append(message.author)
    message.file = path.open("rb")
    message.path = path
    message.filename = "source.txt"
    message.mime = "text/plain"
    handler.send_message(message)
    restart(manager)
    complete(manager)
    row, member = manager.channel.db.resolve_source_member(key[0], "one", "-100")
    assert uploaded == [b"complete attachment content"]
    assert (row.msg_type, row.media_type, row.file_id, row.file_unique_id, row.sender_bot_id) == (
        "File", "Document", "real-file", "real-unique", "700")
    restored = member_message(member)
    assert (restored.type, restored.text, restored.file_id, restored.file_bot_id) == (
        MsgType.File, "file body", "real-file", "700")
