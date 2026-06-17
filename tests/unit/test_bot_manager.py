import string
import random
import threading
from collections import defaultdict, deque
from typing import IO, Iterator, BinaryIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import telegram.error

from efb_telegram_master.bot_manager import TelegramBotManager


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
        _delayed_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
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

    assert result._sender_bot_id == "777"
    assert used_bots == [aux_bot.bot]
    manager.bot_pool.get_bot_by_id.assert_called_once_with("777")
    aux_bot.reserve_slot.assert_called_once_with(123)


def test_rate_limit_decorator_falls_back_to_main_bot_when_sender_missing():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    bot_pool = SimpleNamespace(get_bot_by_id=Mock(return_value=None), acquire_send_slot=Mock())
    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=bot_pool,
        _calculate_rate_limit_delay=Mock(return_value=(0.0, 0, 0)),
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
    manager._rate_limit_lock = threading.Lock()
    manager._global_timestamps = []
    manager._chat_timestamps = defaultdict(deque)
    manager.GLOBAL_LIMIT = 30
    manager.GLOBAL_WINDOW = 1.0
    manager.CHAT_LIMIT = 20
    manager.CHAT_WINDOW = 60.0
    manager._cleanup_tls = SimpleNamespace(pending_cleanup=[])
    manager.logger = Mock()
    manager._delayed_queue = []
    manager._delayed_queue_lock = threading.Lock()
    manager._task_counter = 0
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

    with patch("efb_telegram_master.bot_manager.time.time", return_value=100.0):
        result = getattr(manager, method_name)(**kwargs)

    assert result.chat_id == 123
    assert len(manager._global_timestamps) == 1
    assert list(manager._chat_timestamps[123]) == [100.0]
    bot_method.assert_called_once()


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
        bot_pool=SimpleNamespace(acquire_send_slot=Mock(return_value=(aux_bot, 0.0))),
        _calculate_rate_limit_delay=Mock(return_value=(1.0, 0, 0)),
        _using_bot=lambda bot: DummyContext(),
        _record_aux_use=Mock(),
        logger=Mock(),
    )

    result = decorated(manager, 123)

    assert result._sender_bot_id == "999"
    manager.bot_pool.acquire_send_slot.assert_called_once_with(123, max_delay=1.0)
    manager._record_aux_use.assert_called_once_with(123)


@pytest.mark.parametrize(("method_name", "kwargs"), [
    ("edit_message_caption", {"chat_id": 123, "message_id": 456, "caption": "updated"}),
    ("edit_message_media", {"chat_id": 123, "message_id": 456, "media": object()}),
])
def test_no_sender_caption_and_media_edits_reserve_main_quota_without_aux_pool(method_name, kwargs):
    manager = _make_lightweight_bot_manager()
    manager.CHAT_LIMIT = 3
    manager.bot_pool = SimpleNamespace(acquire_send_slot=Mock(return_value=(SimpleNamespace(), 0.0)))
    manager._chat_timestamps[123].append(100.0)

    with patch("efb_telegram_master.bot_manager.time.time", return_value=159.0):
        result = getattr(manager, method_name)(**kwargs)

    assert result.is_delayed is True
    assert len(manager._global_timestamps) == 1
    assert list(manager._chat_timestamps[123]) == [100.0, 160.0]
    manager.bot_pool.acquire_send_slot.assert_not_called()
    assert manager._delayed_queue[0][2].allow_aux_routing is False


def test_rate_limit_decorator_schedules_delayed_task_when_main_bot_is_limited():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=None,
        _calculate_rate_limit_delay=Mock(return_value=(5.0, 1, 1)),
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _schedule_delayed_task=Mock(return_value="task-1"),
        _create_delayed_message_placeholder=Mock(return_value=SimpleNamespace(is_delayed=True, task_id="task-1")),
        logger=Mock(),
    )

    result = decorated(manager, 123)

    assert result.is_delayed is True
    manager._schedule_delayed_task.assert_called_once()
    manager._create_delayed_message_placeholder.assert_called_once_with(123, 5.0, "task-1")


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


def test_graceful_stop_stops_worker_pool_and_updater():
    manager = SimpleNamespace(
        logger=Mock(),
        _delayed_queue=[("when", 0, "task")],
        _delayed_queue_lock=threading.Lock(),
        stop_delayed_worker=Mock(),
        bot_pool=SimpleNamespace(shutdown=Mock()),
        updater=SimpleNamespace(stop=Mock()),
    )

    TelegramBotManager.graceful_stop(manager)

    manager.stop_delayed_worker.assert_called_once()
    manager.bot_pool.shutdown.assert_called_once()
    manager.updater.stop.assert_called_once()
