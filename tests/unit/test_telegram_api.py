from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import ChatMigrated

from efb_telegram_master.outbound import OutboundQueue
from efb_telegram_master.outbound_types import QueueEnqueueError, SendReceipt
from efb_telegram_master.telegram_api import TelegramAPI


class _RecordingQueue:
    def __init__(self) -> None:
        self.requests = []
        self.side_effect = None

    def enqueue_and_wait(self, request):
        self.requests.append(request)
        if self.side_effect:
            return self.side_effect(request)
        return SendReceipt(SimpleNamespace(operation=request.operation))


def _api() -> tuple[TelegramAPI, Mock, _RecordingQueue, Mock]:
    bot = Mock()
    queue = _RecordingQueue()
    topic_sync = Mock()
    api = TelegramAPI(SimpleNamespace(topic_sync=topic_sync), bot, queue, None)
    return api, bot, queue, topic_sync


def test_answer_callback_query_does_not_forward_internal_routing_arguments() -> None:
    answer_callback_query = Mock()
    api = TelegramAPI(SimpleNamespace(), SimpleNamespace(answer_callback_query=answer_callback_query), SimpleNamespace(), None)

    api.answer_callback_query("query", text="Done", chat_id=1, message_id=2, cache_time=180)

    assert answer_callback_query.call_args.kwargs == {"text": "Done", "cache_time": 180}


@pytest.mark.parametrize(
    ("operation", "args", "kwargs", "content_key", "expected"),
    [
        ("send_message", (1, "body"), {"prefix": "before", "suffix": "after"}, "text", "before\nbody\nafter"),
        ("send_photo", (1, "photo"), {"caption": "body", "prefix": "before", "suffix": "after"}, "caption", "before\nbody\nafter"),
    ],
)
def test_send_operations_affix_content_and_keep_routing_metadata_out_of_telegram_kwargs(operation, args, kwargs, content_key, expected) -> None:
    api, _bot, queue, _chat_binding = _api()

    getattr(api, operation)(*args, **kwargs, _sender_bot_id="aux-7", _slave_id="slave.chat")

    request = queue.requests[0]
    assert request.operation == operation
    delivered = request.args[1 if content_key == "text" else 2] if len(request.args) > (1 if content_key == "text" else 2) else request.kwargs[content_key]
    assert delivered == expected
    assert request.required_sender_bot_id is None
    assert request.slave_id == "slave.chat"
    assert not {"prefix", "suffix", "_sender_bot_id", "_slave_id"} & request.kwargs.keys()


def test_callback_keyboard_forces_main_sender() -> None:
    api, _bot, queue, _chat_binding = _api()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Open", callback_data="open")]])

    api.send_message(1, "body", reply_markup=keyboard, _sender_bot_id="aux-7")

    assert queue.requests[0].required_sender_bot_id == "__main__"


@pytest.mark.parametrize(
    ("operation", "args", "required_sender_bot_id"),
    [
        ("send_audio", (1, "audio"), None),
        ("send_document", (1, "document"), None),
        ("send_video", (1, "video"), None),
        ("send_animation", (1, "animation"), None),
        ("send_voice", (1, "voice"), None),
        ("send_sticker", (1, "sticker"), None),
        ("send_media_group", (1, ["media"]), None),
        ("forward_message", (1, 2, 3), None),
        ("copy_message", (1, 2, 3), None),
        ("edit_message_caption", (1, 2, "inline-id", "caption"), "__main__"),
        ("edit_message_media", (1, 2, "media"), "__main__"),
    ],
)
def test_remaining_public_queued_operations_route_to_the_outbound_queue(operation, args, required_sender_bot_id) -> None:
    api, _bot, queue, _chat_binding = _api()

    getattr(api, operation)(*args)

    request = queue.requests[0]
    assert request.operation == operation
    assert request.required_sender_bot_id == required_sender_bot_id


@pytest.mark.parametrize(
    ("operation", "args", "kwargs"),
    [
        ("edit_message_reply_markup", (), {"chat_id": 1, "message_id": 2}),
        ("send_location", (1, 1.0, 2.0), {}),
        ("send_venue", (1, 1.0, 2.0, "title", "address"), {}),
        ("create_forum_topic", (1, "topic"), {}),
        ("edit_forum_topic", (1, 2), {}),
        ("reopen_forum_topic", (1, 2), {}),
        ("set_chat_title", (1, "title"), {}),
        ("set_chat_photo", (1, object()), {}),
        ("pin_chat_message", (1, 2), {}),
        ("set_chat_description", (1, "description"), {}),
    ],
)
def test_edits_and_chat_mutations_queue_through_main_sender(operation, args, kwargs) -> None:
    api, _bot, queue, _chat_binding = _api()

    getattr(api, operation)(*args, **kwargs, _sender_bot_id="aux-7", _slave_id="slave.chat", _force_main_bot=True)

    request = queue.requests[0]
    assert request.operation == operation
    assert request.required_sender_bot_id == "__main__"
    assert request.slave_id is None
    assert not {"_sender_bot_id", "_slave_id", "_force_main_bot"} & request.kwargs.keys()


def test_edit_requires_explicit_sender_and_delete_routes_to_requested_sender() -> None:
    api, _bot, queue, _chat_binding = _api()

    api.edit_message_text(chat_id=1, message_id=2, text="edited")
    api.edit_message_text(chat_id=1, message_id=2, text="edited", _sender_bot_id="aux-7")
    api.delete_message(1, 2, _sender_bot_id="aux-8")

    assert [request.required_sender_bot_id for request in queue.requests] == ["__main__", "aux-7", "aux-8"]


@pytest.mark.parametrize(
    ("operation", "args", "kwargs", "content_key", "content_index"),
    [
        ("edit_message_text", ("body", 1, 2), {}, "text", 0),
        ("edit_message_text", (), {"chat_id": 1, "message_id": 2, "text": "body"}, "text", 0),
        ("edit_message_caption", (1, 2, "inline-id", "body"), {}, "caption", 3),
        ("edit_message_caption", (), {"chat_id": 1, "message_id": 2, "caption": "body"}, "caption", 3),
    ],
)
def test_edit_operations_affix_positional_and_keyword_content(operation, args, kwargs, content_key, content_index) -> None:
    api, _bot, queue, _chat_binding = _api()

    getattr(api, operation)(*args, **kwargs, prefix="Prefix", suffix="Suffix")

    request = queue.requests[0]
    content = request.args[content_index] if len(request.args) > content_index else request.kwargs[content_key]
    assert content == "Prefix\nbody\nSuffix"
    assert request.required_sender_bot_id == "__main__"
    assert "prefix" not in request.kwargs
    assert "suffix" not in request.kwargs


def test_keyword_only_set_chat_title_strips_queue_metadata() -> None:
    api, _bot, queue, _chat_binding = _api()

    api.set_chat_title(
        chat_id=1,
        title="title",
        _sender_bot_id="aux-7",
        _slave_id="slave.chat",
        _force_main_bot=True,
    )

    request = queue.requests[0]
    assert request.args == ()
    assert request.kwargs == {"chat_id": 1, "title": "title"}
    assert request.required_sender_bot_id == "__main__"
    assert request.slave_id is None


def test_direct_calls_strip_queue_metadata_and_preserve_chat_action_thread_arguments() -> None:
    api, bot, queue, _chat_binding = _api()
    bot.send_chat_action.return_value = True
    bot.get_me.return_value = "bot"
    kwargs = {"message_thread_id": 7, "api_kwargs": {"keep": "value"}, "_sender_bot_id": "aux-7"}

    assert api.send_chat_action(1, "typing", **kwargs) is True
    assert api.get_me(_sender_bot_id="aux-7") == "bot"

    assert kwargs == {"message_thread_id": 7, "api_kwargs": {"keep": "value"}, "_sender_bot_id": "aux-7"}
    bot.send_chat_action.assert_called_once_with(1, "typing", api_kwargs={"keep": "value", "message_thread_id": 7})
    bot.get_me.assert_called_once_with()
    assert queue.requests == []


def test_positional_edit_retries_with_migrated_chat_id() -> None:
    api, _bot, queue, topic_sync = _api()

    def migrate_then_succeed(request):
        if len(queue.requests) == 1:
            raise ChatMigrated(2)
        return SendReceipt(SimpleNamespace(message_id=3))

    queue.side_effect = migrate_then_succeed
    receipt = api.edit_message_text("body", 1, 3, "inline-id", parse_mode="HTML")

    assert receipt.message_id == 3
    assert [request.args for request in queue.requests] == [("body", 1, 3, "inline-id"), ("body", 2, 3, "inline-id")]
    topic_sync.migrate_chat_associations.assert_called_once_with(1, 2)


@pytest.mark.parametrize(
    ("operation", "kwargs"),
    [
        ("send_media_group", {"chat_id": 1, "media": ["media"], "message_thread_id": 7}),
        ("forward_message", {"chat_id": 1, "from_chat_id": 2, "message_id": 3, "message_thread_id": 7}),
    ],
)
def test_ordinary_operations_keep_destination_thread_and_strip_queue_metadata(operation, kwargs) -> None:
    api, _bot, queue, _chat_binding = _api()

    getattr(api, operation)(**kwargs, _sender_bot_id="aux-7", _slave_id="slave.chat", _force_main_bot=True)

    request = queue.requests[0]
    assert request.operation == operation
    assert request.kwargs == kwargs
    assert request.required_sender_bot_id == "__main__"
    assert request.slave_id == "slave.chat"


def test_forward_message_retries_after_chat_migration_and_waits_for_receipt() -> None:
    api, _bot, queue, topic_sync = _api()

    def migrate_then_succeed(request):
        if len(queue.requests) == 1:
            raise ChatMigrated(4)
        return SendReceipt(SimpleNamespace(message_id=5))

    queue.side_effect = migrate_then_succeed

    receipt = api.forward_message(chat_id=1, from_chat_id=2, message_id=3)

    assert receipt.message_id == 5
    assert [request.kwargs["chat_id"] for request in queue.requests] == [1, 4]
    topic_sync.migrate_chat_associations.assert_called_once_with(1, 4)


def test_chat_migration_retry_rewinds_an_exhausted_upload() -> None:
    api, _bot, queue, topic_sync = _api()
    photo = BytesIO(b"photo")
    uploaded = []

    def migrate_then_succeed(request):
        source = request.args[1]
        uploaded.append(source.read())
        if len(uploaded) == 1:
            raise ChatMigrated(4)
        return SendReceipt(SimpleNamespace(message_id=5))

    queue.side_effect = migrate_then_succeed

    receipt = api.send_photo(1, photo)

    assert receipt.message_id == 5
    assert uploaded == [b"photo", b"photo"]
    topic_sync.migrate_chat_associations.assert_called_once_with(1, 4)


def test_api_cleans_claimed_upload_when_enqueue_fails(tmp_path) -> None:
    api, _bot, queue, _chat_binding = _api()
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")
    api.register_upload_cleanup(str(upload))
    queue.side_effect = lambda _request: (_ for _ in ()).throw(QueueEnqueueError("queue rejected request"))

    with pytest.raises(QueueEnqueueError, match="queue rejected request"):
        api.send_document(1, upload.as_uri())

    assert not upload.exists()


def test_chat_migration_preserves_owned_upload_until_the_retried_request_finishes(tmp_path) -> None:
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"upload")
    attempts = 0

    class Limiter:
        def peek_delay(self, _chat_id):
            return 0.0

        def try_acquire(self, _chat_id):
            return True

        def occupancy_snapshot(self):
            return {"global": 0.0, "chat": 0.0}

    class Sender:
        def send_document(self, chat_id, document, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ChatMigrated(2)
            assert upload.exists()
            return SimpleNamespace(message_id=5)

    queue = OutboundQueue(Sender(), None, Limiter(), worker_count=1, blocking_timeout=1, shutdown_drain_timeout=1, shutdown_join_grace=0.1)
    queue.start()
    try:
        api = TelegramAPI(SimpleNamespace(topic_sync=Mock()), Sender(), queue, None)
        api.register_upload_cleanup(str(upload))

        receipt = api.send_document(1, upload.as_uri())

        assert receipt.message_id == 5
        assert attempts == 2
        assert not upload.exists()
    finally:
        queue.stop()
