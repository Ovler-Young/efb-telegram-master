import threading
import time
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest

from efb_telegram_master.request_configuration import RequestConfiguration
from efb_telegram_master.telegram_runtime import TelegramPollingRuntime, TelegramRuntimeShutdownTimeout, build_telegram_polling_runtime
from efb_telegram_master.telegram_sync_bridge import AsyncTelegramRuntime


def _runtime(
    *,
    application: object | None = None,
    async_runtime: AsyncTelegramRuntime | None = None,
    webhook: dict[str, object] | None = None,
) -> TelegramPollingRuntime:
    return TelegramPollingRuntime(
        Mock(),
        application,
        Mock(),
        async_runtime or AsyncTelegramRuntime(Mock()),
        AsyncMock(),
        AsyncMock(),
        webhook,
    )


@pytest.mark.parametrize(
    ("environment", "expected"),
    [(None, 16), ("3", 24), ("0.5", 4), ("invalid", 16), ("0", 16)],
)
def test_default_connection_pool_size_uses_worker_count_multiplier(monkeypatch: pytest.MonkeyPatch, environment: str | None, expected: int) -> None:
    if environment is None:
        monkeypatch.delenv("ETM_HTTPX_POOL_MULTIPLIER", raising=False)
    else:
        monkeypatch.setenv("ETM_HTTPX_POOL_MULTIPLIER", environment)

    assert TelegramPollingRuntime._default_connection_pool_size({}) == expected


def test_build_runtime_passes_local_mode_and_independent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = [Mock(name="request"), Mock(name="get_updates_request")]
    application = Mock()
    builder = Mock()
    builder.bot.return_value = builder
    builder.job_queue.return_value = builder
    builder.post_init.return_value = builder
    builder.post_shutdown.return_value = builder
    builder.build.return_value = application
    channel = Mock()
    channel.flag.side_effect = {"local_tdlib_api": True, "api_base_url": "http://localhost:8081/bot", "api_base_file_url": "file:///var/lib/telegram-bot-api"}.get

    with patch("efb_telegram_master.telegram_runtime.build_request", side_effect=requests) as build_request:
        with patch("efb_telegram_master.telegram_runtime.telegram.Bot") as bot_cls:
            with patch("efb_telegram_master.telegram_runtime.Application.builder", return_value=builder):
                runtime = build_telegram_polling_runtime({"token": "123:token"}, channel, Mock(), AsyncMock(), AsyncMock())

    bot_cls.assert_called_once_with(
        token="123:token",
        local_mode=True,
        base_url="http://localhost:8081/bot",
        base_file_url="file:///var/lib/telegram-bot-api",
        request=requests[0],
        get_updates_request=requests[1],
    )
    assert build_request.call_count == 2
    assert [call.args[0] for call in build_request.call_args_list] == [
        RequestConfiguration(connection_pool_size=16, read_timeout=15.0),
        RequestConfiguration(connection_pool_size=16, read_timeout=15.0),
    ]
    assert runtime.application is application


@pytest.mark.parametrize("webhook", [False, [], "invalid"])
def test_build_runtime_rejects_non_mapping_webhook_config(webhook: object) -> None:
    with pytest.raises(ValueError, match="webhook must be a mapping"):
        build_telegram_polling_runtime({"token": "123:token", "webhook": webhook}, Mock(), Mock(), AsyncMock(), AsyncMock())


def test_build_runtime_rejects_non_mapping_request_config() -> None:
    with pytest.raises(ValueError, match="request_kwargs must be a mapping"):
        build_telegram_polling_runtime({"token": "123:token", "request_kwargs": []}, Mock(), Mock(), AsyncMock(), AsyncMock())


def test_webhook_stop_waits_for_blocked_ptb_teardown_before_shutting_down_runtime() -> None:
    async_runtime = Mock()
    teardown_started = threading.Event()
    release_teardown = threading.Event()
    application = Mock()

    def run_webhook(**_kwargs: object) -> None:
        teardown_started.set()
        release_teardown.wait()

    application.run_webhook.side_effect = run_webhook
    runtime = _runtime(application=application, async_runtime=async_runtime, webhook={"start_webhook": {}})
    poll_thread = threading.Thread(target=runtime.poll)
    poll_thread.start()
    try:
        assert teardown_started.wait(timeout=1)

        with pytest.raises(TelegramRuntimeShutdownTimeout):
            runtime.stop(time.monotonic() + 0.01)

        async_runtime.shutdown.assert_not_called()
    finally:
        release_teardown.set()
        poll_thread.join(timeout=1)
    assert not poll_thread.is_alive()

    runtime.stop(time.monotonic() + 0.1)

    async_runtime.shutdown.assert_called_once_with(ANY)


def test_webhook_runtime_forwards_typed_start_arguments() -> None:
    application = Mock()
    runtime = _runtime(
        application=application,
        webhook={"start_webhook": {"listen": "0.0.0.0", "port": 8443, "allowed_updates": ["message"], "secret_token": "secret"}},
    )

    runtime.poll(drop_pending_updates=True)

    application.run_webhook.assert_called_once_with(
        listen="0.0.0.0",
        port=8443,
        allowed_updates=["message"],
        secret_token="secret",
        drop_pending_updates=True,
        close_loop=True,
        stop_signals=None,
    )


@pytest.mark.parametrize("argument", ["port", "bootstrap_retries", "max_connections"])
def test_webhook_runtime_rejects_boolean_numeric_start_arguments(argument: str) -> None:
    application = Mock()
    runtime = _runtime(application=application, webhook={"start_webhook": {argument: True}})

    with pytest.raises(ValueError, match="must be an integer"):
        runtime.poll()

    application.run_webhook.assert_not_called()


def test_invalid_webhook_configuration_does_not_leave_lifecycle_active() -> None:
    async_runtime = Mock()
    runtime = _runtime(application=Mock(), async_runtime=async_runtime, webhook={"start_webhook": None})

    with pytest.raises(ValueError, match="webhook.start_webhook must be a mapping"):
        runtime.poll()

    runtime.stop(time.monotonic() + 0.01)

    async_runtime.shutdown.assert_called_once_with(ANY)
