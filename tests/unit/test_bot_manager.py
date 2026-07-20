import asyncio
import base64
import inspect
import io
import string
import random
import threading
from datetime import timedelta
from typing import Iterator, BinaryIO
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile

from efb_telegram_master.bot_manager import (
    QueuedDbLogContext,
    SendReceipt,
    SyncBotFacade,
    TelegramBotManager,
)
from efb_telegram_master.bot_manager import AsyncTelegramRuntime
from efb_telegram_master.outbound import OutboundQueue, QueueEnqueueError, QueueRequest, SenderSelection


def _bind_blocking_enqueue_helper(manager):
    if "_enqueue_blocking_send_and_wait" not in getattr(manager, "__dict__", {}):
        def _enqueue_blocking_send_and_wait(slave_id, chat_id, fn, args, kwargs, cleanup_files=None):
            queued_args = args[1:] if args and args[0] is manager else args
            result = manager.execute_queued_call(
                SimpleNamespace(operation=fn.__name__),
                queued_args,
                kwargs,
                SenderSelection(sender=manager._bot, sender_bot_id=None),
            )
            sender_bot_id = None
            required_sender = kwargs.get("_required_sender_bot_id")
            if required_sender not in {None, "__main__"}:
                sender_bot_id = required_sender
            return SendReceipt(message=result, sender_bot_id=sender_bot_id)

        manager._enqueue_blocking_send_and_wait = Mock(side_effect=_enqueue_blocking_send_and_wait)
    return manager


def _bind_db_update_helpers(manager):
    manager._run_database_update_callback = TelegramBotManager._run_database_update_callback.__get__(
        manager,
        TelegramBotManager,
    )
    manager._write_database_update = TelegramBotManager._write_database_update.__get__(
        manager,
        TelegramBotManager,
    )
    return manager


@pytest.fixture
def formatted_channel(channel):
    _bind_blocking_enqueue_helper(channel.bot_manager)
    return channel


def test_sync_bot_facade_preserves_telegram_method_signature():
    async def send_message(chat_id, text):
        return chat_id, text

    facade = SyncBotFacade(SimpleNamespace(send_message=send_message), Mock())

    assert inspect.signature(facade.send_message).bind(42, "message").arguments == {
        "chat_id": 42,
        "text": "message",
    }


def test_text_prefix_suffix(formatted_channel, bot_admin):
    message = formatted_channel.bot_manager.send_message(bot_admin, 'Message', prefix='Prefix', suffix='Suffix')
    assert message.text == 'Prefix\nMessage\nSuffix'

    edited = formatted_channel.bot_manager.edit_message_text(
        text="Edited text", prefix="Edited prefix", suffix="Edited suffix",
        chat_id=message.chat_id, message_id=message.message_id)
    assert edited.chat_id == message.chat_id
    assert edited.message_id == message.message_id
    assert edited.text == "Edited prefix\nEdited text\nEdited suffix"


@pytest.fixture(scope='function')
def image() -> Iterator[BinaryIO]:
    f = open('tests/mocks/image.png', 'rb')
    yield f
    f.close()


def test_caption_prefix_suffix(formatted_channel, bot_admin, image):
    message = formatted_channel.bot_manager.send_photo(bot_admin, image, caption='Message', prefix='Prefix', suffix='Suffix')
    assert message.caption == 'Prefix\nMessage\nSuffix'

    edited = formatted_channel.bot_manager.edit_message_caption(
        caption="Edited text", prefix="Edited prefix", suffix="Edited suffix",
        chat_id=message.chat_id, message_id=message.message_id)
    assert edited.chat_id == message.chat_id
    assert edited.message_id == message.message_id
    assert edited.caption == "Edited prefix\nEdited text\nEdited suffix"


def test_message_truncation(formatted_channel, bot_admin):
    msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
    with patch('telegram.Bot.send_document') as mock_send_document:
        message = formatted_channel.bot_manager.send_message(bot_admin, msg_body, prefix='Prefix')
        assert message.text.startswith('Prefix\n' + msg_body[:50])
        mock_send_document.assert_called()
        assert mock_send_document.call_args[1]['filename'].endswith('txt')

        # Edit message text
        msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
        edited = formatted_channel.bot_manager.edit_message_text(
            text=msg_body, prefix='Prefix',
            chat_id=message.chat_id, message_id=message.message_id
        )
        assert edited.text.startswith('Prefix\n' + msg_body[:50])
        mock_send_document.assert_called()
        assert mock_send_document.call_args[1]['filename'].endswith('txt')


def test_caption_truncation(formatted_channel, bot_admin, image):
    msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
    with patch('telegram.Bot.send_document') as mock_send_document:
        message = formatted_channel.bot_manager.send_photo(bot_admin, image, caption=msg_body, prefix='Prefix')
        assert message.caption.startswith('Prefix\n' + msg_body[:50])
        mock_send_document.assert_called()
        assert mock_send_document.call_args[1]['filename'].endswith('txt')

        # Edit message text
        msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
        edited = formatted_channel.bot_manager.edit_message_caption(
            caption=msg_body, prefix='Prefix',
            chat_id=message.chat_id, message_id=message.message_id
        )
        assert edited.caption.startswith('Prefix\n' + msg_body[:50])


def test_malformed_markdown_text(formatted_channel, bot_admin):
    formatted_channel.bot_manager.send_message(
        bot_admin,
        "*some _strange_ styling* with [an *incomplete* link](https://example.com/this.is.a.(link",
        parse_mode="markdown"
    )


def test_malformed_markdown_caption(formatted_channel, bot_admin, image):
    formatted_channel.bot_manager.send_photo(
        bot_admin,
        image,
        caption="*some _strange_ styling* with [an *incomplete* link](https://example.com/this.is.a.(link",
        parse_mode="markdown"
    )


def test_malformed_html_text(formatted_channel, bot_admin):
    formatted_channel.bot_manager.send_message(
        bot_admin,
        '<b>Bold and <i>italics</i> text</b> and <abbr title="unknown tag to Telegram">UTTT</abbr> and an <a href="https://example.com">incomplete link</a',
        parse_mode="html"
    )


def test_malformed_html_caption(formatted_channel, bot_admin, image):
    formatted_channel.bot_manager.send_photo(
        bot_admin,
        image,
        caption='<b>Bold and <i>italics</i> text</b> and <abbr title="unknown tag to Telegram">UTTT</abbr> and an <a href="https://example.com">incomplete link</a',
        parse_mode="html"
    )


def _make_queueing_manager() -> TelegramBotManager:
    manager = object.__new__(TelegramBotManager)
    manager._bot = Mock()
    manager._cleanup_tls = SimpleNamespace(pending_cleanup=[])
    manager._enqueue_eventual_send = Mock()
    manager._enqueue_blocking_send_and_wait = Mock()
    return manager


def test_blocking_enqueue_helper_executes_queued_send_preparation():
    manager = _make_queueing_manager()
    result_message = SimpleNamespace(message_id=7)
    manager._bot.send_message.return_value = result_message
    del manager._enqueue_blocking_send_and_wait
    _bind_blocking_enqueue_helper(manager)
    body = "x" * int(telegram.constants.MessageLimit.MAX_TEXT_LENGTH)

    receipt = manager._enqueue_blocking_send_and_wait(
        None,
        123,
        manager._queued_operation_callable("send_message"),
        (manager, 123, body),
        {"_required_sender_bot_id": "__main__"},
    )

    assert receipt.message is result_message
    manager._bot.send_message.assert_called_once_with(123, body[:100] + "\n...\n" + body[-100:])
    manager._bot.send_document.assert_called_once()
    assert manager._bot.send_document.call_args.kwargs["filename"] == "123_7.txt"


def _queued_send_document(chat_id, document, **kwargs):
    return chat_id, document, kwargs


@pytest.mark.parametrize(
    ("kind", "expected_filename"),
    [("explicit", "explicit.bin"), ("input-file", "input.bin"),
     ("local-basename", "source.bin"), ("none", None)],
)
def test_queued_document_filename_precedence(tmp_path, kind, expected_filename):
    queue = OutboundQueue(tmp_path)
    kwargs = {}
    if kind in {"explicit", "input-file"}:
        media = InputFile(b"media", filename="input.bin")
        if kind == "explicit":
            kwargs["filename"] = "explicit.bin"
        source = None
    elif kind == "local-basename":
        path = tmp_path / "source.bin"
        path.write_bytes(b"media")
        source = None
        media = path
    else:
        source = io.BufferedReader(io.BytesIO(b"media"))
        media = source
    queue.enqueue_many(
        [QueueRequest("send_document", (100, media), kwargs)],
        lambda _name: _queued_send_document,
    )
    if source is not None:
        source.close()
    if kind == "local-basename":
        path.unlink()

    args, decoded_kwargs = queue.decode_payload(queue.heads()[0].payload)
    delivered = args[1]
    if isinstance(delivered, InputFile):
        assert delivered.input_file_content == b"media"
    else:
        assert delivered.tell() == 0
        assert delivered.read() == b"media"
    if expected_filename is None:
        assert not hasattr(delivered, "name")
        assert "filename" not in decoded_kwargs
    else:
        actual_filename = delivered.filename if isinstance(delivered, InputFile) else delivered.name
        assert actual_filename == expected_filename
        assert decoded_kwargs.get("filename") == ("explicit.bin" if kind == "explicit" else None)


@pytest.mark.parametrize(
    ("operation", "encoded"),
    [
        ("send_message", "AYAFlSoAAAAAAAAASyqMBWhlbGxvlIaUfZSMFGRpc2FibGVfbm90aWZpY2F0aW9ulIhzhpQu"),
        ("send_photo", "AYAFlRgAAAAAAAAASyqMDEFnQUMtZmlsZS1pZJSGlH2UhpQu"),
        ("send_photo", "AYAFlSkAAAAAAAAASyqMHWh0dHBzOi8vZXhhbXBsZS5jb20vcGhvdG8uanBnlIaUfZSGlC4="),
        ("send_document", "AYAFlTEAAAAAAAAASypDDGxlZ2FjeS1ieXRlc5SGlH2UjAhmaWxlbmFtZZSMCmxlZ2FjeS5iaW6Uc4aULg=="),
    ],
    ids=["non-media", "file-id", "url", "bytes"],
)
def test_prechange_version_one_payloads_decode_and_execute_without_reencoding(
    operation, encoded, monkeypatch
):
    payload = base64.b64decode(encoded)
    assert payload[0] == 1
    encoder = Mock(side_effect=AssertionError("legacy payload must not be re-encoded"))
    monkeypatch.setattr(OutboundQueue, "encode_payload", encoder)
    args, kwargs = OutboundQueue.decode_payload(payload)
    manager = object.__new__(TelegramBotManager)
    sender = Mock()

    manager.execute_queued_call(
        SimpleNamespace(operation=operation), args, kwargs,
        SenderSelection(sender=sender, sender_bot_id=None),
    )

    getattr(sender, operation).assert_called_once_with(*args, **kwargs)
    encoder.assert_not_called()
    assert payload == base64.b64decode(encoded)


def test_build_bot_passes_local_mode_when_enabled():
    manager = object.__new__(TelegramBotManager)
    manager._bot_identity_kwargs = {
        "token": "123:token",
        "base_url": "http://localhost:8081/bot",
        "base_file_url": "file:///var/lib/telegram-bot-api",
    }
    manager._local_mode = True
    request = Mock()
    get_updates_request = Mock()

    with patch("efb_telegram_master.bot_manager.telegram.Bot") as bot_cls:
        manager._build_bot(request=request, get_updates_request=get_updates_request)

    bot_cls.assert_called_once_with(
        token="123:token",
        base_url="http://localhost:8081/bot",
        base_file_url="file:///var/lib/telegram-bot-api",
        local_mode=True,
        request=request,
        get_updates_request=get_updates_request,
    )


def test_default_connection_pool_size_uses_worker_count_multiplier(monkeypatch):
    monkeypatch.delenv(TelegramBotManager.HTTPX_POOL_MULTIPLIER_ENV, raising=False)
    assert TelegramBotManager._default_connection_pool_size({}) == 16

    monkeypatch.setenv(TelegramBotManager.HTTPX_POOL_MULTIPLIER_ENV, "3")
    assert TelegramBotManager._default_connection_pool_size({}) == 24

    monkeypatch.setenv(TelegramBotManager.HTTPX_POOL_MULTIPLIER_ENV, "0.5")
    assert TelegramBotManager._default_connection_pool_size({}) == 4


def test_queued_failure_decision_retries_only_eventual_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = object.__new__(TelegramBotManager)
    manager._bot_chat_disabled_until = {("10", 100): 1_111.0}
    manager.bot_pool = None
    task = SimpleNamespace(telegram_chat_id=100, slave_id=None, priority=0)
    selection = SimpleNamespace(sender_bot_id="10")
    monkeypatch.setattr("efb_telegram_master.bot_manager.time.monotonic", lambda: 1_000.0)

    retry = manager.record_queued_failure(task, telegram.error.RetryAfter(20), selection)
    assert retry.kind.name == "RETRY_EVENTUAL"
    assert retry.retry_at == 1_020.0

    blocking = manager.record_queued_failure(
        SimpleNamespace(telegram_chat_id=100, slave_id=None, priority=1),
        telegram.error.RetryAfter(20),
        selection,
    )
    assert blocking.kind.name == "TERMINAL_FAILURE"


def test_terminal_eventual_failure_does_not_clear_existing_cooldown() -> None:
    manager = object.__new__(TelegramBotManager)
    manager._bot_chat_disabled_until = {("10", 100): 1_025.0}
    manager.bot_pool = None
    task = SimpleNamespace(telegram_chat_id=100, slave_id=None, priority=0)

    decision = manager.record_queued_failure(task, Exception("send failed"), SimpleNamespace(sender_bot_id="10"))

    assert decision.kind.name == "TERMINAL_FAILURE"
    assert manager._bot_chat_disabled_until == {("10", 100): 1_025.0}


def test_public_send_message_routes_eligible_requests_as_eventual():
    manager = _make_queueing_manager()
    queued_receipt = SimpleNamespace(queued=True, task_id=1)
    manager._enqueue_eventual_send.return_value = queued_receipt

    result = manager.send_message(
        123,
        text="queued",
        _send_mode="eventual",
        _slave_id="slave.chat",
    )

    assert result is queued_receipt
    assert manager._enqueue_eventual_send.call_args.args[0:2] == ("slave.chat", 123)
    assert manager._enqueue_eventual_send.call_args.args[2].__name__ == "send_message"
    assert manager._enqueue_eventual_send.call_args.args[3:] == ((manager, 123), {"text": "queued"})


@pytest.mark.parametrize(
    ("operation", "args", "content_kwargs", "content_key", "content_index"),
    [
        ("send_message", (123, "message"), {}, "text", 1),
        ("send_message", (123,), {"text": "message"}, "text", 1),
        ("send_photo", (123, "photo", "caption"), {}, "caption", 2),
        ("send_photo", (123, "photo"), {"caption": "caption"}, "caption", 2),
    ],
)
def test_queued_routes_apply_affixes_without_passing_manager_kwargs_to_sender(
    operation,
    args,
    content_kwargs,
    content_key,
    content_index,
):
    manager = _make_queueing_manager()
    getattr(manager, operation)(
        *args,
        **content_kwargs,
        prefix="Prefix",
        suffix="Suffix",
        _send_mode="eventual",
        _slave_id="slave.chat",
    )
    queued_call = manager._enqueue_eventual_send.call_args
    queued_args = queued_call.args[3][1:]
    queued_kwargs = queued_call.args[4]
    assert "prefix" not in queued_kwargs
    assert "suffix" not in queued_kwargs

    sender = Mock()
    selection = SimpleNamespace(sender=sender)
    manager.execute_queued_call(SimpleNamespace(operation=operation), queued_args, queued_kwargs, selection)

    sender_call = getattr(sender, operation).call_args
    if content_key in sender_call.kwargs:
        effective_content = sender_call.kwargs[content_key]
    else:
        effective_content = sender_call.args[content_index]
    expected_content = content_kwargs.get(content_key) or args[content_index]
    assert effective_content == "Prefix\n" + expected_content + "\nSuffix"
    assert "prefix" not in sender_call.kwargs
    assert "suffix" not in sender_call.kwargs


@pytest.mark.parametrize(
    ("operation", "content_key", "content_index", "uses_keyword_content"),
    [
        ("send_message", "text", 1, False),
        ("send_message", "text", 1, True),
        ("send_photo", "caption", 2, False),
        ("send_photo", "caption", 2, True),
    ],
)
def test_queued_execution_sends_full_oversized_content_as_attachment_for_positional_and_keyword_inputs(
    operation,
    content_key,
    content_index,
    uses_keyword_content,
):
    manager = _make_queueing_manager()
    content_limit = (
        int(telegram.constants.MessageLimit.MAX_TEXT_LENGTH)
        if content_key == "text"
        else int(telegram.constants.MessageLimit.CAPTION_LENGTH)
    )
    content = "x" * (content_limit + 1)
    args = (123,) if operation == "send_message" else (123, "photo")
    content_kwargs = {content_key: content} if uses_keyword_content else {}
    if not uses_keyword_content:
        args += (content,)

    getattr(manager, operation)(
        *args,
        **content_kwargs,
        prefix="Prefix",
        suffix="Suffix",
        _send_mode="eventual",
        _slave_id="slave.chat",
    )
    queued_call = manager._enqueue_eventual_send.call_args
    queued_args = queued_call.args[3][1:]
    queued_kwargs = queued_call.args[4]
    full_content = "Prefix\n" + content + "\nSuffix"
    sender = Mock()
    sender.send_message.return_value = SimpleNamespace(message_id=7)
    sender.send_photo.return_value = SimpleNamespace(message_id=7)

    result = manager.execute_queued_call(
        SimpleNamespace(operation=operation),
        queued_args,
        queued_kwargs,
        SimpleNamespace(sender=sender),
    )

    assert result.message_id == 7
    sender_call = getattr(sender, operation).call_args
    if content_key in sender_call.kwargs:
        effective_content = sender_call.kwargs[content_key]
    else:
        effective_content = sender_call.args[content_index]
    assert effective_content == full_content[:100] + "\n...\n" + full_content[-100:]
    assert "prefix" not in sender_call.kwargs
    assert "suffix" not in sender_call.kwargs
    attachment = sender.send_document.call_args.args[1]
    assert attachment.getvalue() == full_content.encode("utf-8")
    assert "prefix" not in sender.send_document.call_args.kwargs
    assert "suffix" not in sender.send_document.call_args.kwargs


@pytest.mark.parametrize(
    ("operation", "args", "kwargs"),
    [
        ("send_message", (123,), {"text": "<broken>"}),
        ("send_photo", (123, "photo"), {"caption": "<broken>"}),
    ],
)
def test_queued_execution_retries_entity_parse_failure_once_without_parse_mode(
    operation,
    args,
    kwargs,
):
    manager = object.__new__(TelegramBotManager)
    sender = Mock()
    getattr(sender, operation).side_effect = [
        telegram.error.BadRequest("Can't parse entities"),
        SimpleNamespace(message_id=7),
    ]

    result = manager.execute_queued_call(
        SimpleNamespace(operation=operation),
        args,
        {
            **kwargs,
            "parse_mode": "HTML",
            "_send_mode": "eventual",
            "_slave_id": "slave.chat",
        },
        SimpleNamespace(sender=sender),
    )

    assert result.message_id == 7
    sender_calls = getattr(sender, operation).call_args_list
    assert len(sender_calls) == 2
    assert sender_calls[0].kwargs["parse_mode"] == "HTML"
    assert "parse_mode" not in sender_calls[1].kwargs
    assert sender_calls[1].kwargs == kwargs


def _blocking_queued_payload(manager: TelegramBotManager) -> tuple[tuple, dict]:
    enqueue_call = manager._enqueue_blocking_send_and_wait.call_args
    return enqueue_call.args[3][1:], enqueue_call.args[4]


def _assert_raw_ptb_kwargs(kwargs: dict) -> None:
    assert "prefix" not in kwargs
    assert "suffix" not in kwargs
    assert all(not key.startswith("_") for key in kwargs)


@pytest.mark.parametrize(
    ("operation", "positional_args", "keyword_kwargs", "content_key", "content_index"),
    [
        ("edit_message_text", ("positional text", 123, 456), {"text": "keyword text", "chat_id": 123, "message_id": 456}, "text", 0),
        (
            "edit_message_caption",
            (123, 456, "inline-positional", "positional caption"),
            {"caption": "keyword caption", "chat_id": 123, "message_id": 456},
            "caption",
            3,
        ),
    ],
)
def test_queued_edits_apply_affixes_for_positional_and_keyword_content(
    operation,
    positional_args,
    keyword_kwargs,
    content_key,
    content_index,
):
    manager = _make_queueing_manager()
    sender = Mock()

    for args, kwargs, expected_content in (
        (positional_args, {}, positional_args[content_index]),
        ((), keyword_kwargs, keyword_kwargs[content_key]),
    ):
        getattr(manager, operation)(
            *args,
            **kwargs,
            prefix="Prefix",
            suffix="Suffix",
            _send_mode="eventual",
        )
        queued_args, queued_kwargs = _blocking_queued_payload(manager)
        manager.execute_queued_call(
            SimpleNamespace(operation=operation),
            queued_args,
            queued_kwargs,
            SimpleNamespace(sender=sender),
        )
        raw_call = getattr(sender, operation).call_args
        delivered_content = raw_call.args[content_index] if args else raw_call.kwargs[content_key]
        assert delivered_content == f"Prefix\n{expected_content}\nSuffix"
        if operation == "edit_message_caption" and args:
            assert raw_call.args[2] == "inline-positional"
        _assert_raw_ptb_kwargs(raw_call.kwargs)
        manager._enqueue_blocking_send_and_wait.reset_mock()
        getattr(sender, operation).reset_mock()


@pytest.mark.parametrize(
    ("operation", "positional", "content_key", "content_index"),
    [
        ("edit_message_text", True, "text", 0),
        ("edit_message_text", False, "text", 0),
        ("edit_message_caption", True, "caption", 3),
        ("edit_message_caption", False, "caption", 3),
    ],
)
def test_queued_edit_overflow_attaches_the_actual_prepared_content(
    operation,
    positional,
    content_key,
    content_index,
):
    manager = _make_queueing_manager()
    content_limit = (
        int(telegram.constants.MessageLimit.MAX_TEXT_LENGTH)
        if content_key == "text"
        else int(telegram.constants.MessageLimit.CAPTION_LENGTH)
    )
    content = "x" * (content_limit + 1)
    if operation == "edit_message_text":
        args = (content, 123, 456) if positional else ()
        kwargs = {} if positional else {"text": content, "chat_id": 123, "message_id": 456}
    else:
        args = (123, 456, "inline-positional", content) if positional else ()
        kwargs = {} if positional else {"caption": content, "chat_id": 123, "message_id": 456}

    getattr(manager, operation)(
        *args,
        **kwargs,
        prefix="Prefix",
        suffix="Suffix",
        _send_mode="eventual",
    )
    queued_args, queued_kwargs = _blocking_queued_payload(manager)
    full_content = f"Prefix\n{content}\nSuffix"
    sender = Mock()
    getattr(sender, operation).return_value = SimpleNamespace(message_id=789)

    manager.execute_queued_call(
        SimpleNamespace(operation=operation),
        queued_args,
        queued_kwargs,
        SimpleNamespace(sender=sender),
    )

    raw_call = getattr(sender, operation).call_args
    delivered_content = raw_call.args[content_index] if positional else raw_call.kwargs[content_key]
    assert delivered_content == full_content[:100] + "\n...\n" + full_content[-100:]
    if operation == "edit_message_caption" and positional:
        assert raw_call.args[2] == "inline-positional"
    _assert_raw_ptb_kwargs(raw_call.kwargs)
    attachment = sender.send_document.call_args.args[1]
    assert attachment.getvalue() == full_content.encode("utf-8")
    _assert_raw_ptb_kwargs(sender.send_document.call_args.kwargs)


@pytest.mark.parametrize(
    ("operation", "args", "kwargs", "content_key", "content_index"),
    [
        ("edit_message_text", ("<broken>", 123, 456), {}, "text", 0),
        ("edit_message_caption", (123, 456, "inline-positional", "<broken>"), {}, "caption", 3),
    ],
)
def test_queued_edits_retry_malformed_entities_once_without_parse_mode(
    operation,
    args,
    kwargs,
    content_key,
    content_index,
):
    manager = _make_queueing_manager()
    getattr(manager, operation)(
        *args,
        **kwargs,
        prefix="Prefix",
        suffix="Suffix",
        parse_mode="HTML",
        _send_mode="eventual",
    )
    queued_args, queued_kwargs = _blocking_queued_payload(manager)
    sender = Mock()
    getattr(sender, operation).side_effect = [
        telegram.error.BadRequest("Can't parse entities"),
        SimpleNamespace(message_id=789),
    ]

    manager.execute_queued_call(
        SimpleNamespace(operation=operation),
        queued_args,
        queued_kwargs,
        SimpleNamespace(sender=sender),
    )

    raw_calls = getattr(sender, operation).call_args_list
    assert len(raw_calls) == 2
    expected_content = "Prefix\n" + args[content_index] + "\nSuffix"
    for raw_call in raw_calls:
        assert raw_call.args[content_index] == expected_content
        _assert_raw_ptb_kwargs(raw_call.kwargs)
    assert raw_calls[0].kwargs["parse_mode"] == "HTML"
    assert "parse_mode" not in raw_calls[1].kwargs
    if operation == "edit_message_caption":
        assert raw_calls[1].args[2] == "inline-positional"


def test_queued_route_defers_db_mapping_context_outside_telegram_kwargs():
    manager = _make_queueing_manager()
    db_context = QueuedDbLogContext(Mock(), None, Mock())

    manager.send_message(
        123,
        text="queued",
        _send_mode="eventual",
        _slave_id="slave.chat",
        _queued_db_log_context=db_context,
    )

    eventual_call = manager._enqueue_eventual_send.call_args
    assert eventual_call.args[4] == {"text": "queued"}
    assert eventual_call.kwargs["db_log_context"] is db_context


def test_queued_route_rejects_invalid_db_mapping_context():
    manager = _make_queueing_manager()

    with pytest.raises(QueueEnqueueError, match="QueuedDbLogContext"):
        manager.send_message(
            123,
            text="queued",
            _send_mode="eventual",
            _slave_id="slave.chat",
            _queued_db_log_context=object(),
        )


def test_queued_success_writes_deferred_db_mapping_once():
    manager = object.__new__(TelegramBotManager)
    etm_msg = Mock()
    old_msg_id = Mock()
    on_complete = Mock()
    manager._queued_db_log_contexts = {7: QueuedDbLogContext(etm_msg, old_msg_id, on_complete)}
    manager._queued_db_log_context_lock = threading.Lock()
    manager.bot_pool = None
    manager._write_database_update = Mock()
    row = SimpleNamespace(id=7, priority=0, telegram_chat_id=123, slave_id=None)
    result = Mock()

    TelegramBotManager.record_queued_success(manager, row, result, SimpleNamespace(sender_bot_id="10"))

    manager._write_database_update.assert_called_once_with(
        etm_msg,
        old_msg_id,
        result,
        sender_bot_id="10",
        on_complete=on_complete,
    )
    assert manager._queued_db_log_contexts == {}


def test_enqueue_registers_deferred_mapping_before_waking_worker():
    manager = object.__new__(TelegramBotManager)
    db_context = QueuedDbLogContext(Mock(), None, Mock())
    manager._queued_db_log_contexts = {}
    manager._queued_db_log_context_lock = threading.Lock()
    wake_event = Mock()
    manager._outbound_scheduler = SimpleNamespace(
        _lock=threading.RLock(),
        stopping=False,
        wake_event=wake_event,
    )
    manager._outbound_queue = SimpleNamespace(enqueue_many=Mock(return_value=(7, Mock())))
    manager._queue_operation = Mock()

    def assert_context_registered() -> None:
        assert manager._queued_db_log_contexts == {7: db_context}

    wake_event.set.side_effect = assert_context_registered
    row_id, _waiter = TelegramBotManager._enqueue_requests(
        manager,
        [Mock()],
        db_log_context=db_context,
    )

    assert row_id == "7"
    wake_event.set.assert_called_once_with()


def test_terminal_queued_failure_releases_deferred_mapping_callback():
    manager = object.__new__(TelegramBotManager)
    on_complete = Mock()
    manager._queued_db_log_contexts = {7: QueuedDbLogContext(Mock(), None, on_complete)}
    manager._queued_db_log_context_lock = threading.Lock()
    manager._bot_chat_disabled_until = {}
    manager.bot_pool = None
    row = SimpleNamespace(id=7, priority=0, telegram_chat_id=123, slave_id=None)

    decision = TelegramBotManager.record_queued_failure(
        manager,
        row,
        Exception("send failed"),
        SimpleNamespace(sender_bot_id="10"),
    )

    assert decision.kind.name == "TERMINAL_FAILURE"
    on_complete.assert_called_once_with()
    assert manager._queued_db_log_contexts == {}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"_send_mode": "eventual"},
        {"_send_mode": "blocking", "_slave_id": "slave.chat"},
    ],
)
def test_public_send_message_routes_ineligible_eventual_requests_as_blocking(kwargs):
    manager = _make_queueing_manager()

    manager.send_message(123, text="blocking", **kwargs)

    manager._enqueue_eventual_send.assert_not_called()
    expected_slave_id = kwargs.get("_slave_id")
    assert manager._enqueue_blocking_send_and_wait.call_args.args[:2] == (expected_slave_id, 123)


def test_public_send_message_routes_callback_keyboard_through_main_bot():
    manager = _make_queueing_manager()
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open", callback_data="cb")]])

    manager.send_message(
        123,
        reply_markup=reply_markup,
        _send_mode="eventual",
        _slave_id="slave.chat",
    )

    manager._enqueue_eventual_send.assert_not_called()
    blocking_args = manager._enqueue_blocking_send_and_wait.call_args.args
    assert blocking_args[:2] == ("slave.chat", 123)
    assert blocking_args[4]["_required_sender_bot_id"] == "__main__"


@pytest.mark.parametrize("sender_bot_id", [None, "777"])
def test_public_edit_message_text_is_blocking_and_requires_its_sender(sender_bot_id):
    manager = _make_queueing_manager()
    kwargs = {"text": "updated", "_send_mode": "eventual"}
    if sender_bot_id is not None:
        kwargs["_sender_bot_id"] = sender_bot_id

    manager.edit_message_text(chat_id=123, **kwargs)

    manager._enqueue_eventual_send.assert_not_called()
    blocking_args = manager._enqueue_blocking_send_and_wait.call_args.args
    assert blocking_args[:2] == (None, 123)
    assert blocking_args[2].__name__ == "edit_message_text"
    assert blocking_args[3:] == (
        (manager,),
        {
            "chat_id": 123,
            "text": "updated",
            "_required_sender_bot_id": sender_bot_id or "__main__",
        },
    )


def test_public_positional_edit_retries_chat_migration_without_replacing_text():
    manager = _make_queueing_manager()
    old_chat_id = 123
    new_chat_id = 456
    message_id = 789
    later_argument = "inline-message-id"
    result_message = SimpleNamespace(message_id=message_id)
    manager._bot.edit_message_text.side_effect = [
        telegram.error.ChatMigrated(new_chat_id),
        result_message,
    ]
    manager.channel = SimpleNamespace(
        chat_binding=SimpleNamespace(chat_migration_by_id=Mock())
    )

    def send_and_wait(_slave_id, _chat_id, fn, args, kwargs, cleanup_files=None):
        send_kwargs = {key: value for key, value in kwargs.items() if not key.startswith("_")}
        return SendReceipt(message=fn(*args, **send_kwargs))

    manager._enqueue_blocking_send_and_wait = Mock(side_effect=send_and_wait)

    receipt = manager.edit_message_text(
        "body",
        old_chat_id,
        message_id,
        later_argument,
        parse_mode="HTML",
    )

    assert receipt.message is result_message
    calls = manager._bot.edit_message_text.call_args_list
    assert len(calls) == 2
    assert calls[0].args == ("body", old_chat_id, message_id, later_argument)
    assert calls[0].kwargs == {"parse_mode": "HTML"}
    assert calls[1].args[0] == "body"
    assert calls[1].args[1] == new_chat_id
    assert calls[1].args[2:] == (message_id, later_argument)
    assert calls[1].kwargs == {"parse_mode": "HTML"}
    manager.channel.chat_binding.chat_migration_by_id.assert_called_once_with(old_chat_id, new_chat_id)


def test_enqueue_send_task_keeps_only_live_inputs_and_eventual_metadata():
    manager = object.__new__(TelegramBotManager)
    manager._enqueue_requests = Mock(return_value=("row-1", Mock()))
    manager._create_queued_message_placeholder = Mock(return_value=Mock())
    manager._make_send_receipt = Mock()

    def send_message(_manager, chat_id, text):
        return chat_id, text

    TelegramBotManager._enqueue_eventual_send(
        manager,
        "slave.chat",
        123,
        send_message,
        (manager, 123, "queued"),
        {"text": "queued"},
    )

    request = manager._enqueue_requests.call_args.args[0][0]
    assert request.kwargs == {
        "text": "queued",
        "_slave_id": "slave.chat",
        "_send_mode": "eventual",
    }
    assert {"target", "priority", "waiter"}.isdisjoint(
        inspect.signature(TelegramBotManager._enqueue_send_task).parameters
    )


def test_queued_chat_mutation_strips_private_queue_metadata_before_enqueue():
    manager = _make_queueing_manager()

    def send_location(chat_id, latitude, longitude):
        return chat_id, latitude, longitude

    manager._bot.send_location = send_location
    manager._enqueue_blocking_api_operation = Mock(return_value=True)

    result = manager.send_location(
        123, 1.0, 2.0,
        _sender_bot_id="777",
        _slave_id="slave.chat",
        _send_mode="eventual",
        _force_main_bot=True,
        _required_sender_bot_id="__main__",
        _queued_db_log_context=Mock(),
    )

    assert result is True
    request = manager._enqueue_blocking_api_operation.call_args.kwargs
    assert request["kwargs"] == {}
    assert request["required_sender_bot_id"] == "__main__"


def test_direct_operation_strips_private_queue_metadata_before_calling_bot():
    manager = _make_queueing_manager()
    manager._bot.get_me.return_value = "bot"

    assert manager.get_me(_send_mode="eventual") == "bot"

    manager._bot.get_me.assert_called_once_with()


def test_async_runtime_call_waits_for_bound_loop_before_falling_back():
    runtime = AsyncTelegramRuntime(Mock())
    runtime._ready = Mock()
    runtime._ready.wait.return_value = True
    runtime._loop = object()
    runtime._loop_thread_id = -1
    runtime._ensure_background_loop = Mock()
    future = Mock()
    future.result.return_value = "ok"
    coroutine = object()

    with patch("efb_telegram_master.bot_manager.asyncio.run_coroutine_threadsafe", return_value=future) as runner:
        result = runtime.call(coroutine, timeout=7)

    assert result == "ok"
    runtime._ready.wait.assert_called_once_with(timeout=2.0)
    runtime._ensure_background_loop.assert_not_called()
    runner.assert_called_once_with(coroutine, runtime._loop)
    future.result.assert_called_once_with(7)


def test_async_runtime_call_falls_back_to_background_loop_after_wait_timeout():
    runtime = AsyncTelegramRuntime(Mock())
    runtime._ready = Mock()
    runtime._ready.wait.return_value = False
    background_loop = object()
    runtime._loop = None
    runtime._loop_thread_id = None

    def ensure_background_loop():
        runtime._loop = background_loop
        runtime._loop_thread_id = -1

    runtime._ensure_background_loop = Mock(side_effect=ensure_background_loop)
    future = Mock()
    future.result.return_value = "ok"
    coroutine = object()

    with patch("efb_telegram_master.bot_manager.asyncio.run_coroutine_threadsafe", return_value=future) as runner:
        result = runtime.call(coroutine)

    assert result == "ok"
    runtime._ready.wait.assert_called_once_with(timeout=2.0)
    runtime._ensure_background_loop.assert_called_once_with()
    runtime.logger.debug.assert_called_once_with(
        "Telegram runtime is not ready after %.1fs; starting the background runtime loop.",
        2.0,
    )
    runner.assert_called_once_with(coroutine, background_loop)
    future.result.assert_called_once_with(None)


def test_async_runtime_stale_loop_clear_does_not_remove_rebound_loop():
    runtime = AsyncTelegramRuntime(Mock())
    current_loop = object()
    stale_loop = object()

    runtime._loop = current_loop
    runtime._loop_thread_id = -1
    runtime._loop_thread = Mock()
    runtime._owns_loop_thread = False
    runtime._ready.set()

    runtime.clear_loop(stale_loop)

    assert runtime._loop is current_loop
    assert runtime._loop_thread_id == -1
    assert runtime._ready.is_set()


def test_graceful_stop_runs_ptb_shutdown_on_runtime_loop():
    shutdown_coro = "shutdown-coro"
    shutdown_complete_event = threading.Event()
    shutdown_complete_event.set()
    stopping = threading.Event()
    manager = SimpleNamespace(
        logger=Mock(),
        _stopping=stopping,
        stop_queued_worker=Mock(),
        bot_pool=SimpleNamespace(shutdown=Mock()),
        application=SimpleNamespace(stop_running=Mock()),
        _shutdown_ptb_application=Mock(return_value=shutdown_coro),
        _shutdown_complete_event=shutdown_complete_event,
        _runtime=SimpleNamespace(
            _ready=SimpleNamespace(is_set=Mock(return_value=True)),
            call=Mock(),
            call_soon=Mock(return_value=True),
            clear_loop=Mock(),
            shutdown=Mock(),
            _owns_loop_thread=False,
        ),
    )

    TelegramBotManager.graceful_stop(manager)

    assert stopping.is_set()
    manager.stop_queued_worker.assert_called_once()
    manager.bot_pool.shutdown.assert_called_once()
    manager._shutdown_ptb_application.assert_called_once_with()
    manager._runtime.call.assert_called_once_with(shutdown_coro, timeout=30)
    manager._runtime.call_soon.assert_not_called()
    manager.application.stop_running.assert_not_called()
    manager._runtime.clear_loop.assert_called_once()
    manager._runtime.shutdown.assert_not_called()


def test_graceful_stop_is_idempotent_after_outbound_queue_closes():
    shutdown_complete_event = threading.Event()
    shutdown_complete_event.set()
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = (0,)
    manager = SimpleNamespace(
        logger=Mock(),
        _stopping=threading.Event(),
        _graceful_stop_lock=threading.Lock(),
        _graceful_stop_complete=False,
        _outbound_queue=SimpleNamespace(connection=connection),
        stop_queued_worker=Mock(),
        bot_pool=None,
        _shutdown_complete_event=shutdown_complete_event,
    )

    TelegramBotManager.graceful_stop(manager)
    TelegramBotManager.graceful_stop(manager)

    connection.execute.assert_called_once_with("SELECT COUNT(*) FROM outbound_queue")
    manager.stop_queued_worker.assert_called_once_with()


def test_channel_is_stopping_reads_manager_event():
    from efb_telegram_master import TelegramChannel

    stopping = threading.Event()
    channel = TelegramChannel.__new__(TelegramChannel)
    channel._stop_polling_called = False
    channel.bot_manager = SimpleNamespace(_stopping=stopping)

    assert TelegramChannel._is_stopping(channel) is False
    stopping.set()
    assert TelegramChannel._is_stopping(channel) is True


def test_graceful_stop_falls_back_to_direct_stop_when_runtime_loop_missing():
    shutdown_complete_event = threading.Event()
    shutdown_complete_event.set()
    manager = SimpleNamespace(
        logger=Mock(),
        stop_queued_worker=Mock(),
        bot_pool=None,
        application=SimpleNamespace(stop_running=Mock()),
        _shutdown_ptb_application=Mock(),
        _shutdown_complete_event=shutdown_complete_event,
        _runtime=SimpleNamespace(
            _ready=SimpleNamespace(is_set=Mock(return_value=False)),
            call=Mock(),
            call_soon=Mock(return_value=False),
            clear_loop=Mock(),
            shutdown=Mock(),
            _owns_loop_thread=False,
        ),
    )

    TelegramBotManager.graceful_stop(manager)

    manager.stop_queued_worker.assert_called_once()
    manager._shutdown_ptb_application.assert_not_called()
    manager._runtime.call.assert_not_called()
    manager._runtime.call_soon.assert_called_once_with(manager.application.stop_running)
    manager.application.stop_running.assert_called_once()
    manager._runtime.clear_loop.assert_called_once()
    manager._runtime.shutdown.assert_not_called()


def test_graceful_stop_signals_manual_event_on_event_loop_when_runtime_loop_missing():
    shutdown_complete_event = threading.Event()
    shutdown_complete_event.set()
    manual_evt_loop = SimpleNamespace(
        is_running=Mock(return_value=True),
        call_soon_threadsafe=Mock(),
    )
    manual_evt = SimpleNamespace(set=Mock(), _loop=manual_evt_loop)
    manager = SimpleNamespace(
        logger=Mock(),
        stop_queued_worker=Mock(),
        bot_pool=None,
        application=SimpleNamespace(stop_running=Mock()),
        _shutdown_complete_event=shutdown_complete_event,
        _manual_polling_stop_event=manual_evt,
        _runtime=SimpleNamespace(
            _ready=SimpleNamespace(is_set=Mock(return_value=False)),
            call=Mock(),
            call_soon=Mock(return_value=False),
            clear_loop=Mock(),
            shutdown=Mock(),
            _owns_loop_thread=False,
        ),
    )

    TelegramBotManager.graceful_stop(manager)

    manager._runtime.call_soon.assert_called_once()
    manual_evt_loop.call_soon_threadsafe.assert_called_once()
    manager.application.stop_running.assert_not_called()
    manual_evt.set.assert_not_called()
    manager._runtime.clear_loop.assert_called_once()
    manager._runtime.shutdown.assert_not_called()


def test_graceful_stop_shuts_down_metrics_server():
    shutdown_complete_event = threading.Event()
    shutdown_complete_event.set()
    metrics_httpd = Mock()
    manager = SimpleNamespace(
        logger=Mock(),
        stop_queued_worker=Mock(),
        _metrics_httpd=metrics_httpd,
        bot_pool=None,
        application=SimpleNamespace(stop_running=Mock()),
        _shutdown_ptb_application=Mock(),
        _shutdown_complete_event=shutdown_complete_event,
        _runtime=SimpleNamespace(
            _ready=SimpleNamespace(is_set=Mock(return_value=False)),
            call=Mock(),
            call_soon=Mock(return_value=True),
            clear_loop=Mock(),
            shutdown=Mock(),
            _owns_loop_thread=False,
        ),
    )

    TelegramBotManager.graceful_stop(manager)

    metrics_httpd.shutdown.assert_called_once_with()
    metrics_httpd.server_close.assert_called_once_with()


def test_stop_worker_join_covers_outbound_drain_deadline():
    manager = object.__new__(TelegramBotManager)
    manager.logger = Mock()
    manager._send_worker_stop = threading.Event()
    manager._outbound_scheduler = SimpleNamespace(
        stop_and_drain=Mock(),
        wake_event=threading.Event(),
    )
    manager._send_worker_thread = Mock()
    manager._send_worker_thread.is_alive.return_value = True

    manager.stop_queued_worker()

    manager._outbound_scheduler.stop_and_drain.assert_called_once_with(manager.SHUTDOWN_DRAIN_TIMEOUT)
    join_timeout = manager._send_worker_thread.join.call_args.kwargs["timeout"]
    assert join_timeout > manager.SHUTDOWN_DRAIN_TIMEOUT


def test_worker_finalizes_resources_once_after_stop_join_timeout():
    manager = object.__new__(TelegramBotManager)
    manager.logger = Mock()
    manager._send_worker_stop = threading.Event()
    manager._outbound_scheduler = SimpleNamespace(
        stopping=True,
        stop_and_drain=Mock(),
        wake_event=threading.Event(),
    )
    manager._send_executor = Mock()
    manager._outbound_queue = Mock()
    manager._outbound_finalization_lock = threading.Lock()
    manager._outbound_resources_finalized = False
    manager._send_worker_thread = Mock()
    manager._send_worker_thread.is_alive.side_effect = [True, True, False]

    manager.stop_queued_worker()

    manager._send_executor.shutdown.assert_not_called()
    manager._outbound_queue.close.assert_not_called()

    manager._queued_send_worker()

    manager._send_executor.shutdown.assert_called_once_with(wait=False)
    manager._outbound_queue.close.assert_called_once_with()

    manager.stop_queued_worker()

    manager._send_executor.shutdown.assert_called_once_with(wait=False)
    manager._outbound_queue.close.assert_called_once_with()


def test_queued_worker_finalizes_resources_after_stop_timeout():
    manager = object.__new__(TelegramBotManager)
    manager.logger = Mock()
    manager._send_worker_stop = threading.Event()
    manager._outbound_scheduler = SimpleNamespace(
        stopping=True,
        stop_and_drain=Mock(),
    )
    manager._send_executor = Mock()
    manager._outbound_queue = Mock()
    manager._outbound_finalization_lock = threading.Lock()
    manager._outbound_resources_finalized = False

    manager._queued_send_worker()

    manager._outbound_scheduler.stop_and_drain.assert_called_once_with(manager.SHUTDOWN_DRAIN_TIMEOUT)
    manager._send_executor.shutdown.assert_called_once_with(wait=False)
    manager._outbound_queue.close.assert_called_once_with()


def test_parse_metrics_config_defaults_and_disables_invalid_endpoint_options():
    logger = Mock()

    top_n, endpoint = TelegramBotManager._parse_metrics_config(
        {'top_n': None, 'host': '0.0.0.0', 'port': '9102'},
        logger,
    )

    assert top_n == 20
    assert endpoint == ('0.0.0.0', 9102)

    top_n, endpoint = TelegramBotManager._parse_metrics_config(
        {'top_n': '3', 'host': '127.0.0.1', 'port': object()},
        logger,
    )

    assert top_n == 3
    assert endpoint is None
    assert logger.warning.call_count == 2


@pytest.mark.asyncio
async def test_shutdown_ptb_application_signals_stop_running():
    application = SimpleNamespace(stop_running=Mock())
    manager = SimpleNamespace(application=application)

    await TelegramBotManager._shutdown_ptb_application(manager)

    application.stop_running.assert_called_once_with()


def test_polling_passes_custom_timeout_to_manual_lifecycle():
    recorded: dict[str, object] = {}

    async def recording_lifecycle(self, *, drop_pending_updates, timeout):
        recorded["drop_pending_updates"] = drop_pending_updates
        recorded["timeout"] = timeout

    def run_coro(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with patch.object(TelegramBotManager, "_run_application_lifecycle", recording_lifecycle):
        with patch("efb_telegram_master.bot_manager.asyncio.run", side_effect=run_coro):
            manager = SimpleNamespace(
                webhook=False,
                application=object(),
                logger=Mock(),
                _shutdown_complete_event=threading.Event(),
                _manual_polling_stop_event=None,
            )

            TelegramBotManager.polling(manager, drop_pending_updates=True, timeout=1)

    assert recorded["drop_pending_updates"] is True
    assert recorded["timeout"] == 1


def test_run_application_lifecycle_publishes_manual_stop_event_after_post_init():
    observed: list[tuple[str, object]] = []

    class FakeUpdater:
        def __init__(self):
            self.running = False

        async def start_polling(self, **kwargs):
            observed.append(("start_polling", manager._manual_polling_stop_event is not None))
            self.running = True
            assert manager._manual_polling_stop_event is not None
            manager._manual_polling_stop_event.set()

        async def stop(self):
            observed.append(("updater_stop", True))
            self.running = False

    async def initialize():
        observed.append(("initialize", manager._manual_polling_stop_event))

    async def post_init(_application):
        observed.append(("post_init", manager._manual_polling_stop_event))

    async def start():
        observed.append(("start", manager._manual_polling_stop_event is not None))
        application.running = True

    async def stop():
        observed.append(("stop", True))
        application.running = False

    async def shutdown():
        observed.append(("shutdown", True))

    async def post_shutdown(_application):
        observed.append(("post_shutdown", manager._manual_polling_stop_event))

    updater = FakeUpdater()
    application = SimpleNamespace(
        initialize=initialize,
        post_init=post_init,
        updater=updater,
        create_task=lambda coro: None,
        process_error=Mock(),
        start=start,
        running=False,
        stop=stop,
        post_stop=None,
        shutdown=shutdown,
        post_shutdown=post_shutdown,
    )
    manager = SimpleNamespace(
        application=application,
        logger=Mock(),
        _manual_polling_stop_event=None,
    )

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            TelegramBotManager._run_application_lifecycle(
                manager,
                drop_pending_updates=False,
                timeout=1,
            )
        )
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    assert observed[0] == ("initialize", None)
    assert observed[1] == ("post_init", None)
    assert ("start_polling", True) in observed
    assert ("start", True) in observed
    assert manager._manual_polling_stop_event is None

def test_write_db_mapping_logs_failure_and_runs_completion_callback():
    from efb_telegram_master.bot_manager import TelegramBotManager

    on_complete = Mock()
    db_mock = Mock()
    db_mock.add_or_update_message_log = Mock(side_effect=Exception("DB down"))
    mgr = SimpleNamespace(
        channel=SimpleNamespace(db=db_mock),
        logger=Mock(),
    )
    _bind_db_update_helpers(mgr)
    etm_msg = Mock()
    real_tg_msg = Mock()
    real_tg_msg.message_id = 999

    with patch("efb_telegram_master.bot_manager.get_msg_type", return_value="text"):
        TelegramBotManager.write_db_mapping(
            mgr,
            etm_msg,
            real_tg_msg,
            sender_bot_id=None,
            on_complete=on_complete,
        )

    mgr.logger.warning.assert_called_once()
    on_complete.assert_called_once()
