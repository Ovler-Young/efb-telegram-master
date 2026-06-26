import asyncio
import io
import string
import random
import threading
from contextlib import nullcontext
from datetime import timedelta
from typing import Iterator, BinaryIO
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaVideo
from telegram.request._requestparameter import RequestParameter

from efb_telegram_master.bot_manager import (
    SendReceipt,
    TelegramBotManager,
    _clone_file_argument,
    _clone_media_argument,
)
from efb_telegram_master.bot_manager import AsyncTelegramRuntime
from efb_telegram_master.rate_limiter import SlidingWindowRateLimiter


def _bind_blocking_enqueue_helper(manager):
    if "_enqueue_blocking_send_and_wait" not in getattr(manager, "__dict__", {}):
        def _enqueue_blocking_send_and_wait(slave_id, chat_id, fn, args, kwargs, cleanup_files=None):
            send_kwargs = {
                key: value for key, value in kwargs.items()
                if not key.startswith("_")
            }
            result = fn(*args, **send_kwargs)
            sender_bot_id = None
            if kwargs.get("_force_sender_known") and not kwargs.get("_force_main_bot"):
                sender_bot_id = kwargs.get("_force_sender_bot_id")
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
    manager._finish_queued_database_update = TelegramBotManager._finish_queued_database_update.__get__(
        manager,
        TelegramBotManager,
    )
    manager._finish_successful_send = TelegramBotManager._finish_successful_send.__get__(
        manager,
        TelegramBotManager,
    )
    manager._finish_failed_send = TelegramBotManager._finish_failed_send.__get__(
        manager,
        TelegramBotManager,
    )
    manager._cleanup_queued_task_files = TelegramBotManager._cleanup_queued_task_files.__get__(
        manager,
        TelegramBotManager,
    )
    manager._resolve_task_waiter_success = TelegramBotManager._resolve_task_waiter_success.__get__(
        manager,
        TelegramBotManager,
    )
    manager._resolve_task_waiter_exception = TelegramBotManager._resolve_task_waiter_exception
    manager._rate_limit_retry_after_seconds = TelegramBotManager._rate_limit_retry_after_seconds
    manager._requeue_after_telegram_rate_limit = TelegramBotManager._requeue_after_telegram_rate_limit.__get__(
        manager,
        TelegramBotManager,
    )
    if not hasattr(manager, "_release_reserved_slot"):
        manager._release_reserved_slot = Mock()
    return manager


def test_text_prefix_suffix(channel, bot_admin):
    message = channel.bot_manager.send_message(bot_admin, 'Message', prefix='Prefix', suffix='Suffix')
    assert message.text == 'Prefix\nMessage\nSuffix'

    edited = channel.bot_manager.edit_message_text(
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


def test_caption_prefix_suffix(channel, bot_admin, image):
    message = channel.bot_manager.send_photo(bot_admin, image, caption='Message', prefix='Prefix', suffix='Suffix')
    assert message.caption == 'Prefix\nMessage\nSuffix'

    edited = channel.bot_manager.edit_message_caption(
        caption="Edited text", prefix="Edited prefix", suffix="Edited suffix",
        chat_id=message.chat_id, message_id=message.message_id)
    assert edited.chat_id == message.chat_id
    assert edited.message_id == message.message_id
    assert edited.caption == "Edited prefix\nEdited text\nEdited suffix"


def test_message_truncation(channel, bot_admin):
    msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
    with patch('telegram.Bot.send_document') as mock_send_document:
        message = channel.bot_manager.send_message(bot_admin, msg_body, prefix='Prefix')
        assert message.text.startswith('Prefix\n' + msg_body[:50])
        mock_send_document.assert_called()
        assert mock_send_document.call_args[1]['filename'].endswith('txt')

        # Edit message text
        msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
        edited = channel.bot_manager.edit_message_text(
            text=msg_body, prefix='Prefix',
            chat_id=message.chat_id, message_id=message.message_id
        )
        assert edited.text.startswith('Prefix\n' + msg_body[:50])
        mock_send_document.assert_called()
        assert mock_send_document.call_args[1]['filename'].endswith('txt')


def test_caption_truncation(channel, bot_admin, image):
    msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
    with patch('telegram.Bot.send_document') as mock_send_document:
        message = channel.bot_manager.send_photo(bot_admin, image, caption=msg_body, prefix='Prefix')
        assert message.caption.startswith('Prefix\n' + msg_body[:50])
        mock_send_document.assert_called()
        assert mock_send_document.call_args[1]['filename'].endswith('txt')

        # Edit message text
        msg_body = ''.join(random.choice(string.ascii_letters) for _ in range(100000))
        edited = channel.bot_manager.edit_message_caption(
            caption=msg_body, prefix='Prefix',
            chat_id=message.chat_id, message_id=message.message_id
        )
        assert edited.caption.startswith('Prefix\n' + msg_body[:50])


def test_malformed_markdown_text(channel, bot_admin):
    channel.bot_manager.send_message(
        bot_admin,
        "*some _strange_ styling* with [an *incomplete* link](https://example.com/this.is.a.(link",
        parse_mode="markdown"
    )


def test_malformed_markdown_caption(channel, bot_admin, image):
    channel.bot_manager.send_photo(
        bot_admin,
        image,
        caption="*some _strange_ styling* with [an *incomplete* link](https://example.com/this.is.a.(link",
        parse_mode="markdown"
    )


def test_malformed_html_text(channel, bot_admin):
    channel.bot_manager.send_message(
        bot_admin,
        '<b>Bold and <i>italics</i> text</b> and <abbr title="unknown tag to Telegram">UTTT</abbr> and an <a href="https://example.com">incomplete link</a',
        parse_mode="html"
    )


def test_malformed_html_caption(channel, bot_admin, image):
    channel.bot_manager.send_photo(
        bot_admin,
        image,
        caption='<b>Bold and <i>italics</i> text</b> and <abbr title="unknown tag to Telegram">UTTT</abbr> and an <a href="https://example.com">incomplete link</a',
        parse_mode="html"
    )


def test_rate_limit_decorator_forced_routes_to_sender_bot():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    aux_bot = SimpleNamespace(bot=object(), bot_id=777, disabled=False, reserve_slot=Mock(return_value=0.0))
    used_bots = []
    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _using_bot=lambda bot: SimpleNamespace(__enter__=lambda *a: None, __exit__=lambda *a: None),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    class DummyContext:
        def __init__(self, bot):
            self.bot = bot

        def __enter__(self):
            used_bots.append(self.bot)
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager._using_bot = lambda bot: DummyContext(bot)
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123, _sender_bot_id="777")

    assert result.sender_bot_id == "777"
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_sender_known"] is True
    assert blocking_kwargs["_force_sender_bot_id"] == "777"


def test_rate_limit_decorator_falls_back_to_main_bot_when_sender_missing():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    bot_pool = SimpleNamespace(get_bot_by_id=Mock(return_value=None), acquire_send_slot=Mock())
    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        bot_pool=bot_pool,
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123, _sender_bot_id="777")

    assert result.chat_id == 123
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_sender_known"] is True
    assert blocking_kwargs["_force_sender_bot_id"] == "777"
    bot_pool.acquire_send_slot.assert_not_called()


def _make_lightweight_bot_manager():
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager._bot = Mock()
    manager._tls = threading.local()
    manager.bot_pool = None
    manager._send_worker_stop = threading.Event()
    manager.GLOBAL_LIMIT = 30
    manager.GLOBAL_WINDOW = 1.0
    manager.CHAT_LIMIT = 20
    manager.CHAT_WINDOW = 60.0
    manager._rate_limiter = SlidingWindowRateLimiter(
        global_limit=manager.GLOBAL_LIMIT,
        global_window=manager.GLOBAL_WINDOW,
        chat_limit=manager.CHAT_LIMIT,
        chat_window=manager.CHAT_WINDOW,
        safety_margin=0,
    )
    manager._cleanup_tls = SimpleNamespace(pending_cleanup=[])
    manager.logger = Mock()
    manager._using_bot = TelegramBotManager._using_bot.__get__(manager, TelegramBotManager)
    manager._make_send_receipt = TelegramBotManager._make_send_receipt.__get__(manager, TelegramBotManager)
    manager._calculate_rate_limit_delay = TelegramBotManager._calculate_rate_limit_delay.__get__(
        manager,
        TelegramBotManager,
    )
    _bind_blocking_enqueue_helper(manager)
    return manager


@pytest.mark.parametrize(("method_name", "bot_method_name", "kwargs"), [
    ("edit_message_text", "edit_message_text", {"chat_id": 123, "message_id": 456, "text": "updated"}),
    ("edit_message_caption", "edit_message_caption", {"chat_id": 123, "message_id": 456, "caption": "updated"}),
    ("edit_message_media", "edit_message_media", {"chat_id": 123, "message_id": 456, "media": object()}),
    ("edit_message_reply_markup", "edit_message_reply_markup", {"chat_id": 123, "message_id": 456, "reply_markup": None}),
])
def test_edit_message_methods_reserve_send_quota(method_name, bot_method_name, kwargs):
    manager = _make_lightweight_bot_manager()
    bot_method = getattr(manager._bot, bot_method_name)
    bot_method.return_value = SimpleNamespace(chat_id=123, message_id=456)

    result = getattr(manager, method_name)(**kwargs)

    assert result.chat_id == 123
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_main_bot"] is True
    bot_method.assert_called_once()


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


def test_rate_limit_decorator_routes_new_send_through_aux_pool():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    aux_bot = SimpleNamespace(bot=object(), bot_id=999, disabled=False)

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _calculate_rate_limit_delay=Mock(return_value=(1.0, 0, 0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123)

    assert result.chat_id == 123
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert "_force_sender_known" not in blocking_kwargs
    assert "_force_main_bot" not in blocking_kwargs


def test_rate_limit_decorator_switches_to_aux_when_main_reservation_gets_delayed():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    main_bot = object()
    aux_bot = SimpleNamespace(bot=object(), bot_id=999)
    used_bots = []

    class DummyContext:
        def __init__(self, bot):
            self.bot = bot

        def __enter__(self):
            used_bots.append(self.bot)
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=(aux_bot, 0.0))),
        _calculate_rate_limit_delay=Mock(return_value=(5.0, 0, 0)),
        _release_reserved_slot=Mock(),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(bot),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123, _slave_id="slave.chat")

    assert result.chat_id == 123
    assert manager._enqueue_blocking_send_and_wait.call_args.args[0] == "slave.chat"


@pytest.mark.parametrize(("method_name", "kwargs"), [
    ("edit_message_caption", {"chat_id": 123, "message_id": 456, "caption": "updated"}),
    ("edit_message_media", {"chat_id": 123, "message_id": 456, "media": object()}),
])
def test_no_sender_caption_and_media_edits_reserve_main_quota_without_aux_pool(method_name, kwargs):
    manager = _make_lightweight_bot_manager()
    manager.bot_pool = SimpleNamespace(acquire_send_slot=Mock())

    result = getattr(manager, method_name)(**kwargs)

    assert result.sender_bot_id is None
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_main_bot"] is True
    manager.bot_pool.acquire_send_slot.assert_not_called()


def test_rate_limit_decorator_force_main_bot_skips_aux_pool():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _bot=object(),
        _send_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(acquire_send_slot=Mock()),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123, _force_main_bot=True)

    assert result.sender_bot_id is None
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_main_bot"] is True
    manager.bot_pool.acquire_send_slot.assert_not_called()
    manager._record_aux_use.assert_not_called()


def test_rate_limit_decorator_pool_route_forbidden_marks_chat_non_member_and_retries_main():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    aux_bot = SimpleNamespace(bot=object(), bot_id=999, disabled=False, update_membership=Mock())

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123)

    assert result.chat_id == 123
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert "_force_sender_known" not in blocking_kwargs
    aux_bot.update_membership.assert_not_called()


def test_rate_limit_decorator_routes_reply_to_target_sender_bot():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id, **kwargs: SimpleNamespace(chat_id=chat_id))
    aux_bot = SimpleNamespace(bot=object(), bot_id=777, disabled=False)

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        channel=SimpleNamespace(db=SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(sender_bot_id="777")))),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123, reply_to_message_id=456)

    assert result.sender_bot_id == "777"
    manager.channel.db.get_msg_log.assert_called_once_with(master_msg_id="123.456")
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_sender_known"] is True
    assert blocking_kwargs["_force_sender_bot_id"] == "777"


def test_rate_limit_decorator_callback_keyboard_uses_main_bot_even_when_reply_target_was_aux():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id, **kwargs: SimpleNamespace(chat_id=chat_id))
    main_bot = object()

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _bot=main_bot,
        _send_worker_stop=threading.Event(),
        channel=SimpleNamespace(db=SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(sender_bot_id="777")))),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock()),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=Mock(return_value=DummyContext()),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open", callback_data="cb")]])

    result = decorated(manager, 123, reply_to_message_id=456, reply_markup=reply_markup)

    assert result.sender_bot_id is None
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_main_bot"] is True
    assert "_force_sender_known" not in blocking_kwargs
    manager.channel.db.get_msg_log.assert_not_called()


def test_rate_limit_decorator_edit_with_sender_id_keeps_forced_sender_with_callback_markup():
    def edit_message_text(self, chat_id, **kwargs):
        return SimpleNamespace(chat_id=chat_id)

    decorated = TelegramBotManager.Decorators.rate_limit_decorator(edit_message_text)
    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock()),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open", callback_data="cb")]])

    result = decorated(manager, 123, _sender_bot_id="777", reply_markup=reply_markup)

    assert result.sender_bot_id == "777"
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_sender_known"] is True
    assert blocking_kwargs["_force_sender_bot_id"] == "777"
    assert "_force_main_bot" not in blocking_kwargs


def test_rate_limit_decorator_routes_reply_to_main_bot():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id, **kwargs: SimpleNamespace(chat_id=chat_id))

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _bot=object(),
        _send_worker_stop=threading.Event(),
        channel=SimpleNamespace(db=SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(sender_bot_id=None)))),
        bot_pool=SimpleNamespace(acquire_send_slot=Mock()),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123, reply_to_message_id=456)

    assert result.sender_bot_id is None
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_sender_known"] is True
    assert blocking_kwargs["_force_sender_bot_id"] is None
    manager.bot_pool.acquire_send_slot.assert_not_called()


def test_rate_limit_decorator_eventual_mode_enqueues_task():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    queued_receipt = SimpleNamespace(queued=True, task_id="task-1")
    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        bot_pool=None,
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _enqueue_eventual_send=Mock(return_value=queued_receipt),
        logger=Mock(),
    )

    result = decorated(manager, 123, _send_mode="eventual", _slave_id="slave.chat")

    assert result is queued_receipt
    manager._enqueue_eventual_send.assert_called_once()


def test_rate_limit_decorator_warns_when_eventual_send_has_no_slave_id():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _bot=object(),
        _send_worker_stop=threading.Event(),
        bot_pool=None,
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _enqueue_eventual_send=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123, _send_mode="eventual")

    assert result.message.chat_id == 123
    manager._enqueue_eventual_send.assert_not_called()
    manager.logger.warning.assert_called_once_with(
        "Eventual send requested for chat %s without _slave_id; falling back to blocking.",
        123,
    )


def test_rate_limit_decorator_eventual_reply_preserves_target_sender():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    queued_receipt = SimpleNamespace(queued=True, task_id="task-1")
    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        channel=SimpleNamespace(db=SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(sender_bot_id=None)))),
        bot_pool=SimpleNamespace(acquire_send_slot=Mock()),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _enqueue_eventual_send=Mock(return_value=queued_receipt),
        logger=Mock(),
    )

    result = decorated(manager, 123, reply_to_message_id=456, _send_mode="eventual", _slave_id="slave.chat")

    assert result is queued_receipt
    queued_kwargs = manager._enqueue_eventual_send.call_args.args[4]
    assert queued_kwargs["reply_to_message_id"] == 456
    assert queued_kwargs["_force_sender_known"] is True
    assert queued_kwargs["_force_sender_bot_id"] is None


def test_select_queued_sender_passes_topic_affinity_key_to_bot_pool():
    chat_id = -1002608436807
    aux_bot = SimpleNamespace(bot=object(), bot_id=777)
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=(aux_bot, 0.0))),
        _calculate_rate_limit_delay=Mock(return_value=(1.0, 0, 0)),
        _bot_chat_disabled_until={},
        _bot=object(),
    )

    bot, sender_bot_id, delay = TelegramBotManager._select_queued_sender(
        manager,
        chat_id,
        message_thread_id=1007,
        now=10.0,
    )

    assert bot is aux_bot.bot
    assert sender_bot_id == "777"
    assert delay == 0.0
    call = manager.bot_pool.acquire_send_slot.call_args
    assert call.args == (chat_id,)
    assert call.kwargs["max_delay"] == 1e-9
    assert call.kwargs["affinity_key"] == (chat_id, 1007)
    assert call.kwargs["notify_admin"] is True


def test_select_queued_sender_passes_none_topic_affinity_key_to_bot_pool():
    chat_id = -1002608436807
    aux_bot = SimpleNamespace(bot=object(), bot_id=777)
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=(aux_bot, 0.0))),
        _calculate_rate_limit_delay=Mock(return_value=(1.0, 0, 0)),
        _bot_chat_disabled_until={},
        _bot=object(),
    )

    TelegramBotManager._select_queued_sender(manager, chat_id, now=10.0)

    call = manager.bot_pool.acquire_send_slot.call_args
    assert call.args == (chat_id,)
    assert call.kwargs["affinity_key"] == (chat_id, None)


def test_select_queued_sender_uses_main_when_no_bot_pool_and_main_has_no_delay():
    chat_id = -1002608436807
    main_bot = object()
    manager = SimpleNamespace(
        bot_pool=None,
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _bot_chat_disabled_until={},
        _bot=main_bot,
    )

    bot, sender_bot_id, delay = TelegramBotManager._select_queued_sender(manager, chat_id)

    assert bot is main_bot
    assert sender_bot_id is None
    assert delay == 0.0


def test_select_queued_sender_reserves_forced_aux_slot():
    chat_id = -1002608436807
    aux_bot = SimpleNamespace(
        bot=object(),
        bot_id=777,
        disabled=False,
        peek_delay=Mock(return_value=0.0),
        reserve_slot=Mock(),
    )
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _bot_chat_disabled_until={},
        _bot=object(),
        logger=Mock(),
    )

    bot, sender_bot_id, delay = TelegramBotManager._select_queued_sender(
        manager,
        chat_id,
        forced_sender_bot_id="777",
        now=10.0,
    )

    assert bot is aux_bot.bot
    assert sender_bot_id == "777"
    assert delay == 0.0
    aux_bot.reserve_slot.assert_called_once_with(chat_id)


def test_rate_limit_decorator_forced_sender_fallback_reserves_main_slot():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id, **kwargs: SimpleNamespace(chat_id=chat_id))
    calls = []

    manager = SimpleNamespace(
        _send_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=None)),
        _calculate_rate_limit_delay=Mock(side_effect=lambda chat_id: calls.append(chat_id) or (0.0, 0, 0)),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    _bind_blocking_enqueue_helper(manager)

    result = decorated(manager, 123, _sender_bot_id="777")

    assert result.chat_id == 123
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert blocking_kwargs["_force_sender_known"] is True
    assert blocking_kwargs["_force_sender_bot_id"] == "777"


def test_clone_file_argument_keeps_queued_send_readable_after_original_closes():
    original = io.BytesIO(b"queued-media")

    cloned = _clone_file_argument(original)
    original.close()

    assert cloned.read() == b"queued-media"


def test_clone_media_argument_preserves_input_media_attachment_reference():
    original = InputMediaVideo(io.BytesIO(b"queued-video"), filename="queued-video.mp4")

    cloned = _clone_media_argument(original)
    request_parameter = RequestParameter.from_input("media", cloned)

    assert cloned.media.attach_name is not None
    assert request_parameter.value["media"] == cloned.media.attach_uri
    assert cloned.media.attach_name in request_parameter.multipart_data


def test_select_queued_sender_available_path_passes_topic_affinity_key_to_bot_pool():
    chat_id = -1002608436807
    aux_bot = SimpleNamespace(bot=object(), bot_id=777)
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=(aux_bot, 0.0))),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _bot_chat_disabled_until={},
        _bot=object(),
    )

    TelegramBotManager._select_queued_sender(
        manager,
        chat_id,
        message_thread_id=1007,
        now=10.0,
    )

    call = manager.bot_pool.acquire_send_slot.call_args
    assert call.args == (chat_id,)
    assert call.kwargs["max_delay"] == 1e-9
    assert call.kwargs["affinity_key"] == (chat_id, 1007)
    assert callable(call.kwargs["skip_bot"])


def test_select_queued_sender_available_path_uses_slave_affinity_key():
    chat_id = -1002608436807
    aux_bot = SimpleNamespace(bot=object(), bot_id=777)
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=(aux_bot, 0.0))),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _bot_chat_disabled_until={},
        _bot=object(),
    )

    TelegramBotManager._select_queued_sender(
        manager,
        chat_id,
        slave_id="slave.chat",
        message_thread_id=1007,
        now=10.0,
    )

    call = manager.bot_pool.acquire_send_slot.call_args
    assert call.kwargs["affinity_key"] == "slave.chat"


def test_select_queued_sender_reuses_main_delay_when_aux_unavailable():
    chat_id = -1002608436807
    main_bot = object()
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=None)),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _bot_chat_disabled_until={},
        _bot=main_bot,
    )

    bot, sender_bot_id, delay = TelegramBotManager._select_queued_sender(
        manager,
        chat_id,
        now=10.0,
    )

    assert bot is main_bot
    assert sender_bot_id is None
    assert delay == 0.0
    assert manager._calculate_rate_limit_delay.call_args_list == [
        call(chat_id, peek_only=True),
        call(chat_id),
    ]


def test_handle_rate_limit_error_retries_retry_after_even_when_generic_retry_disabled():
    attempts = {"count": 0}

    def flaky(self, chat_id):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise telegram.error.RetryAfter(1)
        return "ok"

    handler = TelegramBotManager.Decorators.handle_rate_limit_error(flaky)
    manager = SimpleNamespace(_send_worker_stop=threading.Event(), logger=Mock())
    TelegramBotManager.Decorators.enable_retry = False

    with patch("efb_telegram_master.bot_manager.time.sleep") as sleep:
        result = handler(manager, 123)

    assert result == "ok"
    assert attempts["count"] == 2
    sleep.assert_called()


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
        _send_queues={"mock": ["task"]},
        _send_queues_lock=threading.Lock(), _send_in_flight={},
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
        _send_queues={},
        _send_queues_lock=threading.Lock(), _send_in_flight={},
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
        _send_queues={},
        _send_queues_lock=threading.Lock(), _send_in_flight={},
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
        _send_queues={},
        _send_queues_lock=threading.Lock(), _send_in_flight={},
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

# ── Tests for outbound pipeline refactoring ─────────────────────────


# --- Per-target FIFO ordering ---

def test_enqueue_appends_to_per_target_fifo():
    """Tasks for the same (slave_id, chat_id) target must be FIFO."""
    from efb_telegram_master.bot_manager import TelegramBotManager

    target = ("slave.chat", 100)
    mgr = SimpleNamespace(
        _send_queues={},
        _send_queues_lock=threading.Lock(),
        _tasks_enqueued=0,
        _metrics=Mock(),
        logger=Mock(),
    )

    with patch("efb_telegram_master.bot_manager.time.monotonic", side_effect=[100.0, 101.0]):
        TelegramBotManager._enqueue_send_task(mgr, target, lambda: None, (), {}, cleanup_files=[])
        TelegramBotManager._enqueue_send_task(mgr, target, lambda: None, (), {}, cleanup_files=[])

    q = mgr._send_queues[target]
    assert len(q) == 2
    assert q[0].task_id != q[1].task_id  # distinct tasks
    assert q[0].enqueued_at == 100.0
    assert q[1].enqueued_at == 101.0
    assert q[0].task_id == "slave.chat_100_1"
    assert q[1].task_id == "slave.chat_100_2"
    assert mgr._tasks_enqueued == 2
    assert mgr._metrics.task_enqueued.call_count == 2
    mgr._metrics.task_enqueued.assert_called_with(priority=False)


def test_enqueue_priority_tasks_precede_normal_tasks_and_keep_priority_order():
    from efb_telegram_master.bot_manager import TelegramBotManager

    target = ("slave.chat", 100)
    mgr = SimpleNamespace(
        _send_queues={},
        _send_queues_lock=threading.Lock(),
        _tasks_enqueued=0,
        logger=Mock(),
    )

    TelegramBotManager._enqueue_send_task(mgr, target, lambda: None, (), {}, cleanup_files=[])
    TelegramBotManager._enqueue_send_task(mgr, target, lambda: None, (), {}, cleanup_files=[], priority=True)
    TelegramBotManager._enqueue_send_task(mgr, target, lambda: None, (), {}, cleanup_files=[], priority=True)
    TelegramBotManager._enqueue_send_task(mgr, target, lambda: None, (), {}, cleanup_files=[])

    assert [task.task_id for task in mgr._send_queues[target]] == [
        "slave.chat_100_2",
        "slave.chat_100_3",
        "slave.chat_100_1",
        "slave.chat_100_4",
    ]


def test_enqueue_blocking_send_times_out_and_removes_queued_task():
    from efb_telegram_master.bot_manager import TelegramBotManager

    mgr = SimpleNamespace(
        _send_queues={},
        _send_queues_lock=threading.Lock(),
        _tasks_enqueued=0,
        BLOCKING_SEND_TIMEOUT=0.01,
        BLOCKING_SEND_TARGET_SLAVE_ID=TelegramBotManager.BLOCKING_SEND_TARGET_SLAVE_ID,
        logger=Mock(),
    )
    mgr._enqueue_send_task = TelegramBotManager._enqueue_send_task.__get__(mgr, TelegramBotManager)
    mgr._remove_queued_send_task = TelegramBotManager._remove_queued_send_task.__get__(
        mgr,
        TelegramBotManager,
    )
    mgr._resolve_task_waiter_exception = TelegramBotManager._resolve_task_waiter_exception
    mgr._cleanup_queued_task_files = Mock()

    with pytest.raises(RuntimeError, match="Blocking send to chat 100 timed out after 0.01s"):
        TelegramBotManager._enqueue_blocking_send_and_wait(
            mgr,
            None,
            100,
            lambda *_args, **_kwargs: None,
            (),
            {},
        )

    assert mgr._send_queues == {}
    removed_task = mgr._cleanup_queued_task_files.call_args.args[0]
    assert removed_task.task_id == "__blocking___100_1"


def test_requeue_prepends_to_target_fifo():
    """_requeue_send_task must put the task at the FRONT of its target queue."""
    import collections as _collections
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    target = ("slave.chat", 100)
    existing_task = QueuedSendTask(target, lambda: "existing", (), {}, "t2")

    mgr = SimpleNamespace(
        _send_queues={target: _collections.deque([existing_task])},
        _send_queues_lock=threading.Lock(),
        logger=Mock(),
    )

    retry_task = QueuedSendTask(target, lambda: "retry", (), {}, "t1")

    TelegramBotManager._requeue_send_task(mgr, retry_task)

    q = mgr._send_queues[target]
    assert len(q) == 2
    assert q[0].task_id == "t1"  # retry task at front
    assert q[1].task_id == "t2"  # existing task behind it


def test_harvest_completed_sends_does_not_retry_bad_request():
    """BadRequest is not transient, even though PTB makes it a NetworkError."""
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(
        ("slave.chat", 100), lambda: None, (),
        {"reply_to_message_id": 321, "message_thread_id": 654}, "t1",
    )
    future = Future()
    future.set_exception(telegram.error.BadRequest("Message to be replied not found"))
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, None)},
        _metrics=Mock(),
        logger=Mock(),
        _release_reserved_slot=Mock(),
    )
    _bind_db_update_helpers(mgr)

    TelegramBotManager._harvest_completed_sends(mgr)

    assert mgr._send_in_flight == {}
    mgr._release_reserved_slot.assert_called_once_with(None, 100)
    mgr._metrics.send_completed.assert_called_once_with("main", "dropped_bad_request")
    mgr._metrics.task_dropped.assert_called_once_with("bad_request")
    mgr.logger.warning.assert_called_once_with(
        "Non-retryable BadRequest for queued task %s, dropping: %s "
        "(chat_id=%s, reply_to_message_id=%s, message_thread_id=%s, method=%s)",
        "t1",
        future.exception(),
        100,
        321,
        654,
        task.function.__name__,
    )


def test_harvest_completed_sends_records_success_metrics():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(
        ("slave.chat", 100), lambda: None, (), {}, "t1",
        enqueued_at=95.0,
    )
    result = SimpleNamespace(message_id=123)
    future = Future()
    future.set_result(result)
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, "777")},
        _metrics=Mock(),
        _record_aux_use=Mock(),
        logger=Mock(),
    )
    _bind_db_update_helpers(mgr)

    with patch("efb_telegram_master.bot_manager.time.monotonic", return_value=100.0):
        TelegramBotManager._harvest_completed_sends(mgr)

    assert mgr._send_in_flight == {}
    mgr._metrics.send_completed.assert_called_once_with("aux", "ok", 5.0)
    mgr._record_aux_use.assert_called_once_with(100)


def test_harvest_completed_sends_resolves_blocking_waiter_with_receipt():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    waiter = Future()
    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1", waiter=waiter)
    real_tg_msg = SimpleNamespace(chat_id=100, message_id=999)
    future = Future()
    future.set_result(real_tg_msg)
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, "777")},
        _record_aux_use=Mock(),
        logger=Mock(),
    )
    mgr._make_send_receipt = TelegramBotManager._make_send_receipt.__get__(mgr, TelegramBotManager)
    _bind_db_update_helpers(mgr)

    TelegramBotManager._harvest_completed_sends(mgr)

    receipt = waiter.result(timeout=0)
    assert receipt.message is real_tg_msg
    assert receipt.sender_bot_id == "777"


def test_harvest_bad_request_sets_blocking_waiter_exception():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    waiter = Future()
    error = telegram.error.BadRequest("Message to be replied not found")
    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1", waiter=waiter)
    future = Future()
    future.set_exception(error)
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, None)},
        logger=Mock(),
        _release_reserved_slot=Mock(),
    )
    _bind_db_update_helpers(mgr)

    TelegramBotManager._harvest_completed_sends(mgr)

    with pytest.raises(telegram.error.BadRequest):
        waiter.result(timeout=0)


def test_harvest_retry_after_disables_only_sender_bot_chat_and_requeues_front():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1")
    future = Future()
    future.set_exception(telegram.error.RetryAfter(2))
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, "777")},
        _bot_chat_disabled_until={},
        _target_retry_after={},
        _release_reserved_slot=Mock(),
        _requeue_send_task=Mock(),
        _metrics=Mock(),
        logger=Mock(),
    )
    _bind_db_update_helpers(mgr)

    with patch("efb_telegram_master.bot_manager.time.time", return_value=10.0):
        TelegramBotManager._harvest_completed_sends(mgr)

    assert mgr._send_in_flight == {}
    assert mgr._bot_chat_disabled_until == {("777", 100): 12.0}
    assert mgr._target_retry_after == {task.target: 12.0}
    mgr._release_reserved_slot.assert_called_once_with("777", 100)
    mgr._requeue_send_task.assert_called_once_with(task)
    mgr._metrics.rate_limited.assert_called_once_with("aux")
    mgr._metrics.task_requeued.assert_called_once_with("rate_limit")


def test_harvest_retry_after_keeps_blocking_waiter_pending():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    waiter = Future()
    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1", waiter=waiter)
    future = Future()
    future.set_exception(telegram.error.RetryAfter(2))
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, None)},
        _bot_chat_disabled_until={},
        _target_retry_after={},
        _release_reserved_slot=Mock(),
        _requeue_send_task=Mock(),
        logger=Mock(),
    )
    _bind_db_update_helpers(mgr)

    with patch("efb_telegram_master.bot_manager.time.time", return_value=10.0):
        TelegramBotManager._harvest_completed_sends(mgr)

    assert not waiter.done()
    mgr._requeue_send_task.assert_called_once_with(task)


def test_harvest_retry_after_uses_main_bot_disable_key():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1")
    future = Future()
    future.set_exception(telegram.error.RetryAfter(2))
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, None)},
        _bot_chat_disabled_until={},
        _target_retry_after={},
        _release_reserved_slot=Mock(),
        _requeue_send_task=Mock(),
        logger=Mock(),
    )
    _bind_db_update_helpers(mgr)

    with patch("efb_telegram_master.bot_manager.time.time", return_value=10.0):
        TelegramBotManager._harvest_completed_sends(mgr)

    assert mgr._bot_chat_disabled_until == {(None, 100): 12.0}
    assert mgr._target_retry_after == {task.target: 12.0}


def test_harvest_429_disables_only_sender_bot_chat_and_requeues_front():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1")
    future = Future()
    future.set_exception(telegram.error.NetworkError("429 Too Many Requests"))
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, "777")},
        _bot_chat_disabled_until={},
        _target_retry_after={},
        _release_reserved_slot=Mock(),
        _requeue_send_task=Mock(),
        _metrics=Mock(),
        logger=Mock(),
    )
    _bind_db_update_helpers(mgr)

    with patch("efb_telegram_master.bot_manager.time.time", return_value=10.0):
        TelegramBotManager._harvest_completed_sends(mgr)

    assert mgr._send_in_flight == {}
    assert mgr._bot_chat_disabled_until == {("777", 100): 70.0}
    assert mgr._target_retry_after == {task.target: 70.0}
    mgr._release_reserved_slot.assert_called_once_with("777", 100)
    mgr._requeue_send_task.assert_called_once_with(task)
    mgr._metrics.rate_limited.assert_called_once_with("aux")
    mgr._metrics.task_requeued.assert_called_once_with("rate_limit")


def test_rate_limit_retry_after_parser_handles_cause_and_text():
    retry_after = telegram.error.RetryAfter(timedelta(seconds=3))
    assert TelegramBotManager._rate_limit_retry_after_seconds(retry_after) == 3.0

    cause = RuntimeError("HTTP 429")
    cause.response = SimpleNamespace(status_code=429)
    caused_error = telegram.error.NetworkError("request failed")
    caused_error.__cause__ = cause
    assert TelegramBotManager._rate_limit_retry_after_seconds(caused_error) == 60.0

    text_error = telegram.error.NetworkError("Flood control exceeded. Retry in 7 seconds")
    assert TelegramBotManager._rate_limit_retry_after_seconds(text_error) == 7.0


def test_dispatch_ready_send_tasks_requeues_local_limiter_delay_without_disabling_bot_chat():
    import collections as _collections
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1")
    mgr = SimpleNamespace(
        _send_queues={task.target: _collections.deque([task])},
        _send_queues_lock=threading.Lock(),
        _send_in_flight={},
        _send_worker_stop=threading.Event(),
        _select_queued_sender=Mock(return_value=(object(), None, 2.0)),
        _dispatch_send=Mock(),
        _requeue_send_task=None,
        _bot_chat_disabled_until={},
        _metrics=Mock(),
        _target_retry_after={},
    )
    mgr._requeue_send_task = TelegramBotManager._requeue_send_task.__get__(mgr, TelegramBotManager)

    TelegramBotManager._dispatch_ready_send_tasks(mgr, 10.0)

    assert list(mgr._send_queues[task.target]) == [task]
    assert mgr._bot_chat_disabled_until == {}
    assert mgr._target_retry_after == {task.target: 12.0}
    mgr._dispatch_send.assert_not_called()
    mgr._metrics.task_requeued.assert_called_once_with("local_rate_limit")


def test_dispatch_ready_send_tasks_skips_target_until_retry_after():
    import collections as _collections
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1")
    mgr = SimpleNamespace(
        _send_queues={task.target: _collections.deque([task])},
        _send_queues_lock=threading.Lock(),
        _send_in_flight={},
        _send_worker_stop=threading.Event(),
        _target_retry_after={task.target: 11.0},
        _select_queued_sender=Mock(),
        _dispatch_send=Mock(),
    )

    TelegramBotManager._dispatch_ready_send_tasks(mgr, 10.0)

    assert list(mgr._send_queues[task.target]) == [task]
    mgr._select_queued_sender.assert_not_called()
    mgr._dispatch_send.assert_not_called()


def test_dispatch_ready_send_tasks_releases_aux_slot_on_requeue():
    import collections as _collections
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1")
    mgr = SimpleNamespace(
        _send_queues={task.target: _collections.deque([task])},
        _send_queues_lock=threading.Lock(),
        _send_in_flight={},
        _send_worker_stop=threading.Event(),
        _target_retry_after={},
        _select_queued_sender=Mock(return_value=(object(), "777", 2.0)),
        _release_reserved_slot=Mock(),
        _dispatch_send=Mock(),
        _requeue_send_task=None,
    )
    mgr._requeue_send_task = TelegramBotManager._requeue_send_task.__get__(mgr, TelegramBotManager)

    TelegramBotManager._dispatch_ready_send_tasks(mgr, 10.0)

    mgr._release_reserved_slot.assert_called_once_with("777", 100)
    assert mgr._target_retry_after == {task.target: 12.0}
    assert list(mgr._send_queues[task.target]) == [task]


def test_different_targets_dispatch_concurrently_with_same_chat_id():
    """Tasks for different targets can be in-flight simultaneously."""
    import collections as _collections
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    pending_future = Future()
    in_flight = {("slave.a", 100): (pending_future, "task", None)}
    sender_bot = object()

    send_queues = {
        ("slave.a", 100): _collections.deque([QueuedSendTask(("slave.a", 100), lambda: None, (), {}, "t1")]),
        ("slave.b", 100): _collections.deque([QueuedSendTask(("slave.b", 100), lambda: None, (), {}, "t2")]),
    }
    mgr = SimpleNamespace(
        _send_queues=send_queues,
        _send_queues_lock=threading.Lock(),
        _send_in_flight=in_flight,
        _send_worker_stop=threading.Event(),
        _target_retry_after={},
        _select_queued_sender=Mock(return_value=(sender_bot, "777", 0.0)),
        _dispatch_send=Mock(),
        _metrics=Mock(),
    )

    TelegramBotManager._dispatch_ready_send_tasks(mgr, 10.0)

    mgr._dispatch_send.assert_called_once()
    dispatched_task = mgr._dispatch_send.call_args.args[0]
    assert dispatched_task.target == ("slave.b", 100)
    assert ("slave.a", 100) in mgr._send_queues
    assert ("slave.b", 100) not in mgr._send_queues
    mgr._metrics.task_dispatched.assert_called_once_with("aux")


def test_dispatch_send_observes_success_latency():
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(("slave.chat", 100), lambda **_kwargs: "ok", (), {}, "t1")
    mgr = SimpleNamespace(
        _send_executor=ThreadPoolExecutor(max_workers=1),
        _send_in_flight={},
        _using_bot=Mock(return_value=nullcontext()),
        _metrics=Mock(),
    )

    try:
        with patch("efb_telegram_master.bot_manager.time.monotonic", side_effect=[10.0, 12.0]):
            TelegramBotManager._dispatch_send(mgr, task, object(), "777")
            future = mgr._send_in_flight[task.target][0]
            assert future.result(timeout=1) == "ok"
    finally:
        mgr._send_executor.shutdown(wait=False)

    mgr._metrics.observe_send_latency.assert_called_once_with("aux", 2.0)


def test_queued_send_worker_final_snapshot_after_in_flight_wait():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1")
    future = Future()
    future.set_result(None)
    stop_event = threading.Event()
    stop_event.set()
    mgr = SimpleNamespace(
        logger=Mock(),
        _send_worker_stop=stop_event,
        _send_in_flight={task.target: (future, task, None)},
        _send_executor=Mock(),
        _send_queues={},
        _send_queues_lock=threading.Lock(),
        _bot_chat_disabled_until={},
        bot_pool=None,
        _metrics=Mock(),
    )
    _bind_db_update_helpers(mgr)
    mgr._drop_pending_queued_tasks_on_shutdown = TelegramBotManager._drop_pending_queued_tasks_on_shutdown.__get__(
        mgr,
        TelegramBotManager,
    )
    mgr._snapshot_send_metrics = TelegramBotManager._snapshot_send_metrics.__get__(mgr, TelegramBotManager)

    TelegramBotManager._queued_send_worker(mgr)

    assert mgr._send_in_flight == {}
    mgr._send_executor.shutdown.assert_called_once_with(wait=False)
    mgr._metrics.snapshot.assert_called_once_with(
        queued_tasks=0,
        queued_targets=0,
        max_target_depth=0,
        in_flight=0,
        disabled_bot_chats=0,
        retry_targets=0,
        worker_alive=False,
        aux_pool_size=0,
        aux_disabled=0,
    )


def test_queued_placeholder_has_no_delay_fields():
    from efb_telegram_master.bot_manager import TelegramBotManager

    placeholder = TelegramBotManager._create_queued_message_placeholder(
        SimpleNamespace(logger=Mock()), 100, "task-1",
    )

    assert placeholder.text == "[Message queued for delivery]"
    assert placeholder._queued_execution_pending is True
    assert not hasattr(placeholder, "execute_time")
    assert not hasattr(placeholder, "delay_time")
    assert not hasattr(placeholder, "expected_send_time")


def test_dispatch_send_strips_internal_kwargs_and_sets_skip_retry():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    captured = {}

    class InlineExecutor:
        def submit(self, fn):
            future = Future()
            future.set_result(fn())
            return future

    def send(**kwargs):
        captured.update(kwargs)
        return "sent"

    task = QueuedSendTask(
        ("slave.chat", 100),
        send,
        (),
        {
            "text": "hello",
            "_send_mode": "eventual",
            "_slave_id": "slave.chat",
            "_force_sender_known": True,
            "_force_sender_bot_id": "777",
            "_skip_rate_limit_retry": False,
        },
        "t1",
    )
    mgr = SimpleNamespace(
        _send_executor=InlineExecutor(),
        _send_in_flight={},
        _using_bot=Mock(return_value=nullcontext()),
    )

    TelegramBotManager._dispatch_send(mgr, task, object(), "777")

    assert captured == {"text": "hello", "_skip_rate_limit_retry": True}
    assert task.target in mgr._send_in_flight


@pytest.mark.parametrize(("method_name", "kwargs"), [
    ("edit_message_caption", {"chat_id": 123, "message_id": 456, "caption": "updated"}),
    ("edit_message_media", {"chat_id": 123, "message_id": 456, "media": object()}),
])
def test_edit_methods_strip_skip_retry_before_calling_bot(method_name, kwargs):
    mgr = SimpleNamespace(_active_bot=Mock())
    bot_method = getattr(mgr._active_bot, method_name)
    bot_method.return_value = SimpleNamespace(chat_id=123, message_id=456)

    getattr(TelegramBotManager, method_name)(
        mgr,
        _bypass_rate_limit=True,
        _skip_rate_limit_retry=True,
        **kwargs,
    )

    assert "_skip_rate_limit_retry" not in bot_method.call_args.kwargs


def test_harvest_forbidden_from_main_bot_logs_explicit_error():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedSendTask, TelegramBotManager

    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1")
    future = Future()
    future.set_exception(telegram.error.Forbidden("bot was kicked"))
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, None)},
        _release_reserved_slot=Mock(),
        logger=Mock(),
    )
    _bind_db_update_helpers(mgr)

    TelegramBotManager._harvest_completed_sends(mgr)

    mgr._release_reserved_slot.assert_called_once_with(None, 100)
    mgr.logger.error.assert_called_once_with(
        "Main bot got Forbidden in chat %s for queued task %s; dropping task: %s",
        100,
        "t1",
        future.exception(),
    )


# --- Queued DB logging ---

def test_enqueue_eventual_send_moves_db_context_from_kwargs_to_task():
    from efb_telegram_master.bot_manager import QueuedDbLogContext, TelegramBotManager

    db_context = QueuedDbLogContext(Mock(), None, Mock())
    mgr = SimpleNamespace(
        _send_queues={},
        _send_queues_lock=threading.Lock(),
        _tasks_enqueued=0,
        logger=Mock(),
    )
    mgr._enqueue_send_task = TelegramBotManager._enqueue_send_task.__get__(mgr, TelegramBotManager)
    mgr._create_queued_message_placeholder = TelegramBotManager._create_queued_message_placeholder.__get__(
        mgr,
        TelegramBotManager,
    )
    mgr._make_send_receipt = TelegramBotManager._make_send_receipt.__get__(mgr, TelegramBotManager)

    TelegramBotManager._enqueue_eventual_send(
        mgr,
        "slave.chat",
        100,
        lambda: None,
        (),
        {"_queued_db_log_context": db_context, "text": "hello"},
    )

    task = mgr._send_queues[("slave.chat", 100)][0]
    assert task.db_log_context is db_context
    assert "_queued_db_log_context" not in task.kwargs


def test_harvest_completed_sends_writes_db_from_task_context():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedDbLogContext, QueuedSendTask, TelegramBotManager

    on_complete = Mock()
    etm_msg = Mock()
    real_tg_msg = Mock()
    real_tg_msg.message_id = 999
    db_mock = Mock()
    db_context = QueuedDbLogContext(etm_msg, "old-message-id", on_complete)
    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1", db_log_context=db_context)
    future = Future()
    future.set_result(real_tg_msg)
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, "777")},
        channel=SimpleNamespace(db=db_mock),
        _record_aux_use=Mock(),
        logger=Mock(),
    )
    _bind_db_update_helpers(mgr)

    with patch("efb_telegram_master.bot_manager.get_msg_type", return_value="text"):
        TelegramBotManager._harvest_completed_sends(mgr)

    db_mock.add_or_update_message_log.assert_called_once_with(
        etm_msg,
        real_tg_msg,
        "old-message-id",
        sender_bot_id="777",
    )
    etm_msg.put_telegram_file.assert_called_once_with(real_tg_msg)
    on_complete.assert_called_once()


def test_harvest_completed_sends_runs_callback_without_db_for_non_message_result():
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import QueuedDbLogContext, QueuedSendTask, TelegramBotManager

    on_complete = Mock()
    db_context = QueuedDbLogContext(Mock(), None, on_complete)
    task = QueuedSendTask(("slave.chat", 100), lambda: None, (), {}, "t1", db_log_context=db_context)
    future = Future()
    future.set_result(None)
    mgr = SimpleNamespace(
        _send_in_flight={task.target: (future, task, None)},
        logger=Mock(),
    )
    _bind_db_update_helpers(mgr)

    TelegramBotManager._harvest_completed_sends(mgr)

    db_context.etm_msg.put_telegram_file.assert_not_called()
    on_complete.assert_called_once()


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
