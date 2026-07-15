import asyncio
import inspect
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

from efb_telegram_master.bot_manager import SendReceipt, SyncBotFacade, TelegramBotManager
from efb_telegram_master.bot_manager import AsyncTelegramRuntime


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


def test_sync_bot_facade_preserves_telegram_method_signature():
    async def send_message(chat_id, text):
        return chat_id, text

    facade = SyncBotFacade(SimpleNamespace(send_message=send_message), Mock())

    assert inspect.signature(facade.send_message).bind(42, "message").arguments == {
        "chat_id": 42,
        "text": "message",
    }


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


def _make_queueing_manager():
    manager = SimpleNamespace(
        _cleanup_tls=SimpleNamespace(pending_cleanup=[]),
        _enqueue_eventual_send=Mock(),
        logger=Mock(),
    )
    manager._normalize_telegram_chat_id = TelegramBotManager._normalize_telegram_chat_id
    _bind_blocking_enqueue_helper(manager)
    return manager


def send_message(self, chat_id, **kwargs):
    return SimpleNamespace(chat_id=chat_id, kwargs=kwargs)


def edit_message_text(self, chat_id, **kwargs):
    return SimpleNamespace(chat_id=chat_id, kwargs=kwargs)


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


def test_rate_limit_decorator_queues_new_message_without_required_sender():
    manager = _make_queueing_manager()
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(send_message)

    decorated(manager, 123, _sender_bot_id="777", reply_to_message_id=456)

    args = manager._enqueue_blocking_send_and_wait.call_args.args
    assert args[0] is None
    assert args[1] == 123
    assert args[2] is send_message
    assert args[3] == (manager, 123)
    assert args[4] == {"reply_to_message_id": 456}


@pytest.mark.parametrize("sender_bot_id", [None, "777"])
def test_rate_limit_decorator_queues_edits_with_required_sender(sender_bot_id):
    manager = _make_queueing_manager()
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(edit_message_text)
    kwargs = {"text": "updated"}
    if sender_bot_id is not None:
        kwargs["_sender_bot_id"] = sender_bot_id

    decorated(manager, 123, **kwargs)

    expected_sender = sender_bot_id or "__main__"
    assert manager._enqueue_blocking_send_and_wait.call_args.args[4] == {
        "text": "updated", "_required_sender_bot_id": expected_sender
    }


def test_rate_limit_decorator_routes_callback_keyboard_through_main_bot():
    manager = _make_queueing_manager()
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(send_message)
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open", callback_data="cb")]])

    decorated(manager, 123, reply_markup=reply_markup, _send_mode="eventual", _slave_id="slave.chat")

    manager._enqueue_eventual_send.assert_not_called()
    assert manager._enqueue_blocking_send_and_wait.call_args.args[4]["_required_sender_bot_id"] == "__main__"


def test_rate_limit_decorator_queues_eventual_new_message_with_slave_affinity():
    manager = _make_queueing_manager()
    queued_receipt = SimpleNamespace(queued=True, task_id=1)
    manager._enqueue_eventual_send.return_value = queued_receipt
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(send_message)

    result = decorated(manager, 123, text="queued", _send_mode="eventual", _slave_id="slave.chat")

    assert result is queued_receipt
    assert manager._enqueue_eventual_send.call_args.args == (
        "slave.chat", 123, send_message, (manager, 123), {"text": "queued"}
    )


def test_rate_limit_decorator_uses_blocking_queue_without_slave_affinity():
    manager = _make_queueing_manager()
    decorated = TelegramBotManager.Decorators.rate_limit_decorator(send_message)

    decorated(manager, 123, _send_mode="eventual")

    manager._enqueue_eventual_send.assert_not_called()
    assert manager._enqueue_blocking_send_and_wait.call_args.args[:2] == (None, 123)


def test_rate_limit_decorator_keeps_nonqueued_operations_direct():
    manager = _make_queueing_manager()
    manager._make_send_receipt = Mock(return_value="receipt")

    def get_me(self):
        return "message"

    decorated = TelegramBotManager.Decorators.rate_limit_decorator(get_me)

    assert decorated(manager) == "receipt"
    manager._make_send_receipt.assert_called_once_with("message")
    manager._enqueue_blocking_send_and_wait.assert_not_called()


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
