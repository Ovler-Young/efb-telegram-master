import asyncio
import string
import random
import threading
from datetime import timedelta
from typing import Iterator, BinaryIO
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from efb_telegram_master.bot_manager import SendReceipt, TelegramBotManager
from efb_telegram_master.bot_manager import AsyncTelegramRuntime
from efb_telegram_master.etm_metrics import _ManagerStateExporter, bad_request_reason_class
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


def test_rate_limit_decorator_does_not_treat_sender_hint_as_send_ownership():
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

    assert result.sender_bot_id is None
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert "_required_sender_bot_id" not in blocking_kwargs


def test_rate_limit_decorator_ignores_non_edit_sender_hint_without_pool_lookup():
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
    assert "_required_sender_bot_id" not in blocking_kwargs
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
    assert blocking_kwargs["_required_sender_bot_id"] == "__main__"
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
    assert "_required_sender_bot_id" not in blocking_kwargs


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
    assert blocking_kwargs["_required_sender_bot_id"] == "__main__"
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
    assert blocking_kwargs["_required_sender_bot_id"] == "__main__"
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
    assert "_required_sender_bot_id" not in blocking_kwargs
    aux_bot.update_membership.assert_not_called()


def test_rate_limit_decorator_reply_does_not_query_or_require_target_sender():
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

    assert result.sender_bot_id is None
    manager.channel.db.get_msg_log.assert_not_called()
    blocking_kwargs = manager._enqueue_blocking_send_and_wait.call_args.args[4]
    assert "_required_sender_bot_id" not in blocking_kwargs


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
    assert blocking_kwargs["_required_sender_bot_id"] == "__main__"
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
    assert blocking_kwargs["_required_sender_bot_id"] == "777"


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
    assert "_required_sender_bot_id" not in blocking_kwargs
    manager.channel.db.get_msg_log.assert_not_called()


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
    assert "_required_sender_bot_id" not in queued_kwargs


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


def test_bot_chat_occupancy_rows_include_zero_and_cooling_groups():
    rows = []

    _ManagerStateExporter.append_bot_chat_occupancy_rows(
        rows,
        sender="aux",
        bot_id=123,
        username="botA",
        chat_counts={1: 1, 2: 18, 3: 5},
        known_chat_ids={1, 2, 3, 4, 5},
        effective_limit=18,
    )

    assert rows == [
        ("aux", 123, "botA", 0, 18, "available", 2),
        ("aux", 123, "botA", 1, 18, "available", 1),
        ("aux", 123, "botA", 5, 18, "available", 1),
        ("aux", 123, "botA", 18, 18, "cooling", 1),
    ]


def test_bot_debug_state_snapshots_group_cooldown_membership_and_reserved_slots():
    mgr = object.__new__(TelegramBotManager)
    mgr.me = SimpleNamespace(id=1, username="mainbot")

    aux_bot = SimpleNamespace(
        bot_id=123,
        username="botA",
        get_membership_cache_snapshot=Mock(return_value={
            "member": 2,
            "not_member": 1,
            "unknown_probe_pending": 1,
        }),
        get_reserved_slot_count=Mock(return_value=7),
    )
    mgr.bot_pool = SimpleNamespace(
        bots=[aux_bot],
        get_bot_by_id=Mock(return_value=aux_bot),
    )
    mgr._rate_limiter = SimpleNamespace(get_reserved_slot_count=Mock(return_value=3))
    mgr._bot_chat_disabled_until = {
        (None, 100): 115.0,
        ("123", 200): 110.0,
        ("123", 300): 90.0,
    }

    with patch("efb_telegram_master.bot_manager.time.time", return_value=100.0):
        exporter = _ManagerStateExporter(mgr)
        cooldown_rows = exporter.bot_chat_cooldown_rows()

    assert cooldown_rows == [
        ("main", 1, "mainbot", 1, 15.0),
        ("aux", 123, "botA", 1, 10.0),
    ]
    assert exporter.membership_cache_rows() == [
        (123, "botA", "member", 2),
        (123, "botA", "not_member", 1),
        (123, "botA", "unknown_probe_pending", 1),
    ]
    assert exporter.reserved_slots_rows() == [
        ("main", 1, "mainbot", 3),
        ("aux", 123, "botA", 7),
    ]


def test_bad_request_reason_classifies_common_failures():
    assert bad_request_reason_class(
        telegram.error.BadRequest("Message to be replied not found")
    ) == "reply_target_missing"
    assert bad_request_reason_class(
        telegram.error.BadRequest("Can't parse entities")
    ) == "invalid_markup"
    assert bad_request_reason_class(
        telegram.error.BadRequest("Message is too long")
    ) == "message_too_long"


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
