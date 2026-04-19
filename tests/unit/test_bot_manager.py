import string
import random
import threading
from typing import IO, Iterator, BinaryIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import telegram.error

from efb_telegram_master.bot_manager import SendReceipt, TelegramBotManager


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
    aux_bot = SimpleNamespace(bot=object(), bot_id=777, disabled=False)
    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=aux_bot)),
        _using_bot=lambda bot: SimpleNamespace(__enter__=lambda *a: None, __exit__=lambda *a: None),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    class DummyContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    manager._using_bot = lambda bot: DummyContext()

    result = decorated(manager, 123, _sender_bot_id="777")

    assert result.sender_bot_id == "777"
    manager.bot_pool.get_bot_by_id.assert_called_once_with("777")


def test_rate_limit_decorator_falls_back_to_main_bot_when_sender_missing():
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(lambda self, chat_id: SimpleNamespace(chat_id=chat_id))
    manager = SimpleNamespace(
        _delayed_worker_stop=threading.Event(),
        bot_pool=SimpleNamespace(get_bot_by_id=Mock(return_value=None)),
        _make_send_receipt=lambda message, sender_bot_id=None, queued=False, task_id=None: SendReceipt(
            message=message, sender_bot_id=sender_bot_id, queued=queued, task_id=task_id
        ),
        logger=Mock(),
    )

    result = decorated(manager, 123, _sender_bot_id="777")

    assert result.chat_id == 123


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
    manager._select_sender.assert_called_once_with(123, has_callback=False)
    manager._record_aux_use.assert_called_once_with(123)


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


def test_graceful_stop_requests_application_stop_on_runtime_loop():
    manager = SimpleNamespace(
        logger=Mock(),
        _delayed_queue=[("when", 0, "task")],
        _delayed_queue_lock=threading.Lock(),
        stop_delayed_worker=Mock(),
        bot_pool=SimpleNamespace(shutdown=Mock()),
        application=SimpleNamespace(stop_running=Mock()),
        _runtime=SimpleNamespace(call_soon=Mock(return_value=True), shutdown=Mock(), _owns_loop_thread=False),
    )

    TelegramBotManager.graceful_stop(manager)

    manager.stop_delayed_worker.assert_called_once()
    manager.bot_pool.shutdown.assert_called_once()
    manager._runtime.call_soon.assert_called_once_with(manager.application.stop_running)
    manager.application.stop_running.assert_not_called()
    manager._runtime.shutdown.assert_not_called()


def test_graceful_stop_falls_back_to_direct_stop_when_runtime_loop_missing():
    manager = SimpleNamespace(
        logger=Mock(),
        _delayed_queue=[],
        _delayed_queue_lock=threading.Lock(),
        stop_delayed_worker=Mock(),
        bot_pool=None,
        application=SimpleNamespace(stop_running=Mock()),
        _runtime=SimpleNamespace(call_soon=Mock(return_value=False), shutdown=Mock(), _owns_loop_thread=False),
    )

    TelegramBotManager.graceful_stop(manager)

    manager.stop_delayed_worker.assert_called_once()
    manager._runtime.call_soon.assert_called_once_with(manager.application.stop_running)
    manager.application.stop_running.assert_called_once()
    manager._runtime.shutdown.assert_not_called()


def test_polling_passes_custom_timeout_to_run_polling():
    manager = SimpleNamespace(
        webhook=False,
        application=SimpleNamespace(run_polling=Mock()),
    )

    TelegramBotManager.polling(manager, drop_pending_updates=True, timeout=1)

    manager.application.run_polling.assert_called_once_with(
        timeout=1,
        drop_pending_updates=True,
        close_loop=True,
        stop_signals=None,
    )
