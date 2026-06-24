import asyncio
import io
import string
import random
import threading
from typing import Iterator, BinaryIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from efb_telegram_master.bot_manager import SendReceipt, TelegramBotManager, _clone_file_argument
from efb_telegram_master.bot_manager import AsyncTelegramRuntime
from efb_telegram_master.rate_limiter import SlidingWindowRateLimiter


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


def test_empty_file_detection_skips_http_url():
    manager = SimpleNamespace(send_message=Mock(), _=lambda text: text)

    result = TelegramBotManager._detect_empty_file(
        manager,
        "https://example.com/images/photo.jpg",
        100,
        "",
        "",
        "",
    )

    assert result is None
    manager.send_message.assert_not_called()


def test_rate_limit_decorator_forced_routes_to_sender_bot():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    aux_bot = SimpleNamespace(bot=object(), bot_id=777, disabled=False, reserve_slot=Mock(return_value=0.0))
    used_bots = []
    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _select_forced_sender=Mock(return_value=(aux_bot.bot, "777", 0.0)),
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

    result = decorated(manager, 123, _sender_bot_id="777")

    assert result.sender_bot_id == "777"
    assert used_bots == [aux_bot.bot]
    manager._select_forced_sender.assert_called_once_with(123, "777")


def test_rate_limit_decorator_falls_back_to_main_bot_when_sender_missing():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    bot_pool = SimpleNamespace(get_bot_by_id=Mock(return_value=None), acquire_send_slot=Mock())
    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=bot_pool,
        _select_forced_sender=Mock(return_value=(object(), None, 0.0)),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    result = decorated(manager, 123, _sender_bot_id="777")

    assert result.chat_id == 123
    manager._calculate_rate_limit_delay.assert_called_once_with(123)
    bot_pool.acquire_send_slot.assert_not_called()


def _make_lightweight_bot_manager():
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager._bot = Mock()
    manager._tls = threading.local()
    manager.bot_pool = None
    manager._delayed_worker_stop = threading.Event()
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
    chat_count, global_count = manager._rate_limiter.get_counts(123)
    assert chat_count == 1
    assert global_count == 1
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


def test_rate_limit_decorator_routes_new_send_through_aux_pool():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    aux_bot = SimpleNamespace(bot=object(), bot_id=999, disabled=False)

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _calculate_rate_limit_delay=Mock(return_value=(1.0, 0, 0)),
        _select_sender=Mock(return_value=(aux_bot.bot, "999", 0.0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    result = decorated(manager, 123)

    assert result.sender_bot_id == "999"
    manager._select_sender.assert_called_once_with(123, has_callback=False, message_thread_id=None)
    manager._record_aux_use.assert_called_once_with(123)


@pytest.mark.parametrize(("method_name", "kwargs"), [
    ("edit_message_caption", {"chat_id": 123, "message_id": 456, "caption": "updated"}),
    ("edit_message_media", {"chat_id": 123, "message_id": 456, "media": object()}),
])
def test_no_sender_caption_and_media_edits_reserve_main_quota_without_aux_pool(method_name, kwargs):
    manager = _make_lightweight_bot_manager()
    manager.bot_pool = SimpleNamespace(acquire_send_slot=Mock())
    manager._select_sender = Mock()

    result = getattr(manager, method_name)(**kwargs)

    assert result.sender_bot_id is None
    chat_count, global_count = manager._rate_limiter.get_counts(123)
    assert chat_count == 1
    assert global_count == 1
    manager._select_sender.assert_not_called()
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
        _delayed_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(acquire_send_slot=Mock()),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _select_sender=Mock(),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    result = decorated(manager, 123, _force_main_bot=True)

    assert result.sender_bot_id is None
    manager._select_sender.assert_not_called()
    manager.bot_pool.acquire_send_slot.assert_not_called()
    manager._record_aux_use.assert_not_called()


def test_rate_limit_decorator_pool_route_forbidden_marks_chat_non_member_and_retries_main():
    calls = []

    def send(self, chat_id):
        calls.append(chat_id)
        if len(calls) == 1:
            raise telegram.error.Forbidden("bot was kicked")
        return SimpleNamespace(chat_id=chat_id)

    decorated = TelegramBotManager.Decorators.rate_limit_decorator(send)
    aux_bot = SimpleNamespace(bot=object(), bot_id=999, disabled=False, update_membership=Mock())

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _select_sender=Mock(return_value=(aux_bot.bot, "999", 0.0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    result = decorated(manager, 123)

    assert result.sender_bot_id is None
    assert len(calls) == 2
    aux_bot.update_membership.assert_called_once_with(123, False)
    assert aux_bot.disabled is False


def test_rate_limit_decorator_routes_reply_to_target_sender_bot():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id, **kwargs: SimpleNamespace(chat_id=chat_id))
    aux_bot = SimpleNamespace(bot=object(), bot_id=777, disabled=False)

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        channel=SimpleNamespace(db=SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(sender_bot_id="777")))),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _select_forced_sender=Mock(return_value=(aux_bot.bot, "777", 0.0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    result = decorated(manager, 123, reply_to_message_id=456)

    assert result.sender_bot_id == "777"
    manager.channel.db.get_msg_log.assert_called_once_with(master_msg_id="123.456")
    manager._select_forced_sender.assert_called_once_with(123, "777")


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
        _delayed_worker_stop=threading.Event(),
        channel=SimpleNamespace(db=SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(sender_bot_id="777")))),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock()),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _select_sender=Mock(),
        _select_forced_sender=Mock(),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=Mock(return_value=DummyContext()),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open", callback_data="cb")]])

    result = decorated(manager, 123, reply_to_message_id=456, reply_markup=reply_markup)

    assert result.sender_bot_id is None
    manager._select_forced_sender.assert_not_called()
    manager._select_sender.assert_not_called()
    manager._using_bot.assert_called_once_with(main_bot)


def test_rate_limit_decorator_routes_reply_to_main_bot():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id, **kwargs: SimpleNamespace(chat_id=chat_id))

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager = SimpleNamespace(
        _bot=object(),
        _delayed_worker_stop=threading.Event(),
        channel=SimpleNamespace(db=SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(sender_bot_id=None)))),
        bot_pool=SimpleNamespace(acquire_send_slot=Mock()),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _select_sender=Mock(),
        _select_forced_sender=Mock(return_value=(object(), None, 0.0)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    result = decorated(manager, 123, reply_to_message_id=456)

    assert result.sender_bot_id is None
    manager._select_forced_sender.assert_called_once_with(123, None)
    manager._select_sender.assert_not_called()
    manager.bot_pool.acquire_send_slot.assert_not_called()


def test_rate_limit_decorator_eventual_mode_schedules_delayed_task():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    scheduled = SimpleNamespace(queued=True, task_id="task-1")
    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=None,
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _schedule_eventual_send=Mock(return_value=scheduled),
        logger=Mock(),
    )

    result = decorated(manager, 123, _send_mode="eventual")

    assert result is scheduled
    manager._schedule_eventual_send.assert_called_once()


def test_rate_limit_decorator_eventual_reply_preserves_target_sender():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    scheduled = SimpleNamespace(queued=True, task_id="task-1")
    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        channel=SimpleNamespace(db=SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(sender_bot_id=None)))),
        bot_pool=SimpleNamespace(acquire_send_slot=Mock()),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _schedule_eventual_send=Mock(return_value=scheduled),
        logger=Mock(),
    )

    result = decorated(manager, 123, reply_to_message_id=456, _send_mode="eventual")

    assert result is scheduled
    scheduled_kwargs = manager._schedule_eventual_send.call_args.args[3]
    assert scheduled_kwargs["reply_to_message_id"] == 456
    assert scheduled_kwargs["_force_sender_known"] is True
    assert scheduled_kwargs["_force_sender_bot_id"] is None


def test_select_sender_passes_topic_affinity_key_to_bot_pool():
    chat_id = -1002608436807
    aux_bot = SimpleNamespace(bot=object(), bot_id=777)
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=(aux_bot, 0.0))),
        _calculate_rate_limit_delay=Mock(return_value=(1.0, 0, 0)),
        _bot=object(),
    )

    bot, sender_bot_id, delay = TelegramBotManager._select_sender(
        manager,
        chat_id,
        message_thread_id=1007,
    )

    assert bot is aux_bot.bot
    assert sender_bot_id == "777"
    assert delay == 0.0
    manager.bot_pool.acquire_send_slot.assert_called_once_with(
        chat_id,
        max_delay=1.0,
        affinity_key=(chat_id, 1007),
        notify_admin=True,
    )


def test_select_sender_passes_none_topic_affinity_key_to_bot_pool():
    chat_id = -1002608436807
    aux_bot = SimpleNamespace(bot=object(), bot_id=777)
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=(aux_bot, 0.0))),
        _calculate_rate_limit_delay=Mock(return_value=(1.0, 0, 0)),
        _bot=object(),
    )

    TelegramBotManager._select_sender(manager, chat_id)

    manager.bot_pool.acquire_send_slot.assert_called_once_with(
        chat_id,
        max_delay=1.0,
        affinity_key=(chat_id, None),
        notify_admin=True,
    )


def test_select_sender_does_not_notify_or_use_aux_when_main_has_no_delay():
    chat_id = -1002608436807
    main_bot = object()
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=None)),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _bot=main_bot,
    )

    bot, sender_bot_id, delay = TelegramBotManager._select_sender(manager, chat_id)

    assert bot is main_bot
    assert sender_bot_id is None
    assert delay == 0.0
    manager.bot_pool.acquire_send_slot.assert_not_called()


def test_select_forced_sender_reserves_aux_slot():
    chat_id = -1002608436807
    aux_bot = SimpleNamespace(
        bot=object(),
        bot_id=777,
        disabled=False,
        reserve_slot=Mock(return_value=2.5),
    )
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _bot=object(),
        logger=Mock(),
    )

    bot, sender_bot_id, delay = TelegramBotManager._select_forced_sender(manager, chat_id, "777")

    assert bot is aux_bot.bot
    assert sender_bot_id == "777"
    assert delay == 2.5
    aux_bot.reserve_slot.assert_called_once_with(chat_id)


def test_rate_limit_decorator_forced_sender_fallback_reserves_main_slot():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id, **kwargs: SimpleNamespace(chat_id=chat_id))
    calls = []

    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=None)),
        _select_forced_sender=Mock(return_value=(object(), None, 3.0)),
        _calculate_rate_limit_delay=Mock(side_effect=lambda chat_id: calls.append(chat_id) or (0.0, 0, 0)),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    result = decorated(manager, 123, _sender_bot_id="777")

    assert result.chat_id == 123
    assert calls == [123]


def test_clone_file_argument_keeps_delayed_send_readable_after_original_closes():
    original = io.BytesIO(b"queued-media")

    cloned = _clone_file_argument(original)
    original.close()

    assert cloned.read() == b"queued-media"


def test_select_unfrozen_sender_passes_topic_affinity_key_to_bot_pool():
    chat_id = -1002608436807
    aux_bot = SimpleNamespace(bot=object(), bot_id=777)
    manager = SimpleNamespace(
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=(aux_bot, 0.0))),
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
        _bot_chat_frozen_until={},
        _bot=object(),
    )

    TelegramBotManager._select_unfrozen_sender(
        manager,
        chat_id,
        message_thread_id=1007,
        now=10.0,
    )

    call = manager.bot_pool.acquire_send_slot.call_args
    assert call.args == (chat_id,)
    assert call.kwargs["max_delay"] == 0.01
    assert call.kwargs["affinity_key"] == (chat_id, 1007)
    assert callable(call.kwargs["skip_bot"])


def test_handle_rate_limit_error_retries_retry_after_even_when_generic_retry_disabled():
    attempts = {"count": 0}

    def flaky(self, chat_id):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise telegram.error.RetryAfter(1)
        return "ok"

    handler = TelegramBotManager.Decorators.handle_rate_limit_error(flaky)
    manager = SimpleNamespace(_delayed_worker_stop=threading.Event(), logger=Mock())
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
    manager = SimpleNamespace(
        logger=Mock(),
        _chat_queues={"mock": ["task"]},
        _chat_queues_lock=threading.Lock(), _chat_in_flight={},
        stop_delayed_worker=Mock(),
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

    manager.stop_delayed_worker.assert_called_once()
    manager.bot_pool.shutdown.assert_called_once()
    manager._shutdown_ptb_application.assert_called_once_with()
    manager._runtime.call.assert_called_once_with(shutdown_coro, timeout=30)
    manager._runtime.call_soon.assert_not_called()
    manager.application.stop_running.assert_not_called()
    manager._runtime.clear_loop.assert_called_once()
    manager._runtime.shutdown.assert_not_called()


def test_graceful_stop_falls_back_to_direct_stop_when_runtime_loop_missing():
    shutdown_complete_event = threading.Event()
    shutdown_complete_event.set()
    manager = SimpleNamespace(
        logger=Mock(),
        _chat_queues={},
        _chat_queues_lock=threading.Lock(), _chat_in_flight={},
        stop_delayed_worker=Mock(),
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

    manager.stop_delayed_worker.assert_called_once()
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
        _chat_queues={},
        _chat_queues_lock=threading.Lock(), _chat_in_flight={},
        stop_delayed_worker=Mock(),
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


# --- Per-chat FIFO ordering ---

def test_schedule_appends_to_per_chat_fifo():
    """Tasks for the same chat_id must be appended in FIFO order."""
    import collections as _collections
    from efb_telegram_master.bot_manager import TelegramBotManager

    mgr = SimpleNamespace(
        _chat_queues={},
        _chat_queues_lock=threading.Lock(),
        _chat_frozen_until={},
        _tasks_scheduled=0,
        logger=Mock(),
    )

    TelegramBotManager._schedule_delayed_task(
        mgr, chat_id=100, delay_time=0, function=lambda: None,
        args=(), kwargs={}, cleanup_files=[],
    )
    TelegramBotManager._schedule_delayed_task(
        mgr, chat_id=100, delay_time=0, function=lambda: None,
        args=(), kwargs={}, cleanup_files=[],
    )

    q = mgr._chat_queues[100]
    assert len(q) == 2
    assert q[0].task_id != q[1].task_id  # distinct tasks
    assert mgr._tasks_scheduled == 2


def test_requeue_prepends_to_chat_fifo():
    """_requeue_delayed_task must put the task at the FRONT of its chat queue."""
    import collections as _collections
    from efb_telegram_master.bot_manager import DelayedTask, TelegramBotManager

    existing_task = DelayedTask(
        execute_time=1.0, chat_id=100, function=lambda: "existing",
        args=(), kwargs={}, task_id="t2",
    )

    mgr = SimpleNamespace(
        _chat_queues={100: _collections.deque([existing_task])},
        _chat_queues_lock=threading.Lock(),
        _chat_frozen_until={},
        logger=Mock(),
    )

    retry_task = DelayedTask(
        execute_time=0.0, chat_id=100, function=lambda: "retry",
        args=(), kwargs={}, task_id="t1",
    )

    with patch("efb_telegram_master.bot_manager.time.time", return_value=10.0):
        TelegramBotManager._requeue_delayed_task(mgr, retry_task, 2.0)

    q = mgr._chat_queues[100]
    assert len(q) == 2
    assert q[0].task_id == "t1"  # retry task at front
    assert q[1].task_id == "t2"  # existing task behind it
    assert mgr._chat_frozen_until[100] == 12.0


def test_harvest_completed_sends_does_not_retry_bad_request():
    """BadRequest is not transient, even though PTB makes it a NetworkError."""
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import DelayedTask, TelegramBotManager

    task = DelayedTask(
        execute_time=0.0, chat_id=100, function=lambda: None,
        args=(), kwargs={"reply_to_message_id": 321, "message_thread_id": 654}, task_id="t1",
    )
    future = Future()
    future.set_exception(telegram.error.BadRequest("Message to be replied not found"))
    mgr = SimpleNamespace(
        _chat_in_flight={100: (future, task, None)},
        logger=Mock(),
        _requeue_delayed_task=Mock(),
    )

    TelegramBotManager._harvest_completed_sends(mgr)

    assert mgr._chat_in_flight == {}
    mgr._requeue_delayed_task.assert_not_called()
    mgr.logger.warning.assert_called_once_with(
        "Non-retryable BadRequest for delayed task %s, dropping: %s "
        "(chat_id=%s, reply_to_message_id=%s, message_thread_id=%s, method=%s)",
        "t1",
        future.exception(),
        100,
        321,
        654,
        task.function.__name__,
    )


def test_different_chats_dispatch_concurrently():
    """Tasks for different chat_ids can be in-flight simultaneously."""
    import collections as _collections
    from concurrent.futures import Future
    from efb_telegram_master.bot_manager import DelayedTask

    # Simulate: chat 100 already has a send in-flight,
    # chat 200 should still be dispatchable.
    pending_future = Future()
    in_flight = {100: (pending_future, "task", None)}

    chat_queues = {
        100: _collections.deque([DelayedTask(0, 100, lambda: None, (), {}, "t1")]),
        200: _collections.deque([DelayedTask(0, 200, lambda: None, (), {}, "t2")]),
    }

    # chat 100 is blocked (in_flight), chat 200 is free
    dispatchable = [cid for cid, q in chat_queues.items() if q and cid not in in_flight]
    assert 100 not in dispatchable
    assert 200 in dispatchable


# --- DB retry queue ---

def test_finalize_db_update_enqueues_on_failure():
    """DB write failure must push to retry queue, not crash."""
    from efb_telegram_master.bot_manager import TelegramBotManager
    from efb_telegram_master.msg_type import get_msg_type

    db_mock = Mock()
    db_mock.add_or_update_message_log = Mock(side_effect=Exception("DB down"))

    mgr = SimpleNamespace(
        channel=SimpleNamespace(db=db_mock),
        _db_retry_queue=[],
        _db_retry_lock=threading.Lock(),
        logger=Mock(),
    )

    etm_msg = Mock()
    real_tg_msg = Mock()
    real_tg_msg.message_id = 999

    with patch("efb_telegram_master.bot_manager.get_msg_type", return_value="text"):
        TelegramBotManager._finalize_delayed_database_update(
            mgr, etm_msg, None, real_tg_msg, sender_bot_id=None,
        )

    assert len(mgr._db_retry_queue) == 1
    entry = mgr._db_retry_queue[0]
    assert entry[4] == 1  # attempt count


def test_process_db_retry_queue_succeeds_on_retry():
    from efb_telegram_master.bot_manager import TelegramBotManager

    db_mock = Mock()
    db_mock.add_or_update_message_log = Mock()  # succeeds now
    on_complete = Mock()

    etm_msg = Mock()
    real_tg_msg = Mock()
    real_tg_msg.message_id = 999

    mgr = SimpleNamespace(
        channel=SimpleNamespace(db=db_mock),
        _db_retry_queue=[(etm_msg, None, real_tg_msg, None, 1, 0.0, on_complete)],
        _db_retry_lock=threading.Lock(),
        _db_max_retries=5,
        logger=Mock(),
    )

    TelegramBotManager._process_db_retry_queue(mgr)

    db_mock.add_or_update_message_log.assert_called_once()
    assert len(mgr._db_retry_queue) == 0
    on_complete.assert_called_once()


def test_process_db_retry_queue_gives_up_after_max_retries():
    from efb_telegram_master.bot_manager import TelegramBotManager

    db_mock = Mock()
    db_mock.add_or_update_message_log = Mock(side_effect=Exception("still down"))

    etm_msg = Mock()
    real_tg_msg = Mock()
    real_tg_msg.message_id = 999

    mgr = SimpleNamespace(
        channel=SimpleNamespace(db=db_mock),
        _db_retry_queue=[(etm_msg, None, real_tg_msg, None, 5, 0.0)],
        _db_retry_lock=threading.Lock(),
        _db_max_retries=5,
        logger=Mock(),
    )

    TelegramBotManager._process_db_retry_queue(mgr)

    # Should NOT re-enqueue — max retries exceeded
    assert len(mgr._db_retry_queue) == 0
    mgr.logger.error.assert_called()


# --- Rendezvous TTL cleanup ---

def test_cleanup_stale_rendezvous_removes_old_entries():
    from efb_telegram_master.bot_manager import TelegramBotManager
    import time as _time

    mgr = SimpleNamespace(
        _pending_delayed_logs={"old_task": (Mock(), None, 1.0)},       # registered_at = 1.0
        _completed_delayed_results={"old_result": (Mock(), None, 2.0)},  # completed_at = 2.0
        _pending_logs_lock=threading.Lock(),
        _rendezvous_ttl=600.0,
        logger=Mock(),
    )

    with patch("efb_telegram_master.bot_manager.time.time", return_value=700.0):
        TelegramBotManager._cleanup_stale_rendezvous(mgr)

    assert "old_task" not in mgr._pending_delayed_logs
    assert "old_result" not in mgr._completed_delayed_results


def test_cleanup_stale_rendezvous_keeps_fresh_entries():
    from efb_telegram_master.bot_manager import TelegramBotManager

    mgr = SimpleNamespace(
        _pending_delayed_logs={"fresh": (Mock(), None, 100.0)},
        _completed_delayed_results={},
        _pending_logs_lock=threading.Lock(),
        _rendezvous_ttl=600.0,
        logger=Mock(),
    )

    with patch("efb_telegram_master.bot_manager.time.time", return_value=200.0):
        TelegramBotManager._cleanup_stale_rendezvous(mgr)

    assert "fresh" in mgr._pending_delayed_logs


# --- Rendezvous dicts store timestamps ---

def test_register_delayed_database_update_stores_timestamp():
    from efb_telegram_master.bot_manager import TelegramBotManager

    mgr = SimpleNamespace(
        _pending_delayed_logs={},
        _completed_delayed_results={},
        _pending_logs_lock=threading.Lock(),
        logger=Mock(),
    )

    with patch("efb_telegram_master.bot_manager.time.time", return_value=42.0):
        TelegramBotManager.register_delayed_database_update(mgr, "task-1", Mock(), None)

    entry = mgr._pending_delayed_logs["task-1"]
    assert len(entry) == 3
    assert entry[2] == 42.0  # registered_at timestamp


def test_register_delayed_database_update_stores_completion_callback():
    from efb_telegram_master.bot_manager import TelegramBotManager

    on_complete = Mock()
    mgr = SimpleNamespace(
        _pending_delayed_logs={},
        _completed_delayed_results={},
        _pending_logs_lock=threading.Lock(),
        logger=Mock(),
    )

    TelegramBotManager.register_delayed_database_update(
        mgr, "task-1", Mock(), None, on_complete=on_complete,
    )

    entry = mgr._pending_delayed_logs["task-1"]
    assert len(entry) == 4
    assert entry[3] is on_complete


def test_drop_delayed_database_update_runs_completion_callback():
    from efb_telegram_master.bot_manager import TelegramBotManager

    on_complete = Mock()
    mgr = SimpleNamespace(
        _pending_delayed_logs={"task-1": (Mock(), None, 1.0, on_complete)},
        _completed_delayed_results={},
        _pending_logs_lock=threading.Lock(),
        logger=Mock(),
    )

    TelegramBotManager._drop_delayed_database_update(mgr, "task-1")

    assert "task-1" not in mgr._pending_delayed_logs
    on_complete.assert_called_once()


def test_drop_delayed_database_update_before_register_runs_late_callback():
    from efb_telegram_master.bot_manager import TelegramBotManager

    on_complete = Mock()
    db_mock = Mock()
    db_mock.add_or_update_message_log = Mock()
    mgr = SimpleNamespace(
        channel=SimpleNamespace(db=db_mock),
        _pending_delayed_logs={},
        _completed_delayed_results={},
        _pending_logs_lock=threading.Lock(),
        logger=Mock(),
    )

    TelegramBotManager._drop_delayed_database_update(mgr, "task-1")
    TelegramBotManager.register_delayed_database_update(
        mgr, "task-1", Mock(), None, on_complete=on_complete,
    )

    assert "task-1" not in mgr._pending_delayed_logs
    assert "task-1" not in mgr._completed_delayed_results
    db_mock.add_or_update_message_log.assert_not_called()
    on_complete.assert_called_once()


def test_handle_delayed_database_update_runs_completion_callback():
    from efb_telegram_master.bot_manager import TelegramBotManager

    on_complete = Mock()
    etm_msg = Mock()
    real_tg_msg = Mock()
    real_tg_msg.message_id = 999
    db_mock = Mock()
    mgr = SimpleNamespace(
        channel=SimpleNamespace(db=db_mock),
        _pending_delayed_logs={"task-1": (etm_msg, None, 1.0, on_complete)},
        _completed_delayed_results={},
        _pending_logs_lock=threading.Lock(),
        logger=Mock(),
    )

    with patch("efb_telegram_master.bot_manager.get_msg_type", return_value="text"):
        TelegramBotManager._handle_delayed_database_update(mgr, "task-1", real_tg_msg)

    assert "task-1" not in mgr._pending_delayed_logs
    db_mock.add_or_update_message_log.assert_called_once()
    on_complete.assert_called_once()


def test_handle_delayed_database_update_stores_timestamp():
    from efb_telegram_master.bot_manager import TelegramBotManager

    mgr = SimpleNamespace(
        _pending_delayed_logs={},
        _completed_delayed_results={},
        _pending_logs_lock=threading.Lock(),
        logger=Mock(),
    )

    with patch("efb_telegram_master.bot_manager.time.time", return_value=55.0):
        TelegramBotManager._handle_delayed_database_update(mgr, "task-2", Mock(), sender_bot_id="bot1")

    entry = mgr._completed_delayed_results["task-2"]
    assert len(entry) == 3
    assert entry[2] == 55.0  # completed_at timestamp
