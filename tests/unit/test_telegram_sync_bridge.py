from unittest.mock import Mock, patch

import pytest

from efb_telegram_master.transport.telegram_sync_bridge import AsyncTelegramRuntime


def test_async_runtime_call_uses_bound_loop_without_starting_background_loop() -> None:
    runtime = AsyncTelegramRuntime(Mock())
    runtime._ready = Mock()
    runtime._ready.wait.return_value = True
    runtime._loop = object()
    runtime._loop_thread_id = -1
    runtime._ensure_background_loop = Mock()
    future = Mock()
    future.result.return_value = "ok"

    async def coroutine_function() -> None:
        return None

    coroutine = coroutine_function()

    try:
        with patch("efb_telegram_master.transport.telegram_sync_bridge.asyncio.run_coroutine_threadsafe", return_value=future) as runner:
            assert runtime.call(coroutine, timeout=7) == "ok"
    finally:
        coroutine.close()

    runtime._ready.wait.assert_called_once_with(timeout=2.0)
    runtime._ensure_background_loop.assert_not_called()
    runner.assert_called_once_with(coroutine, runtime._loop)
    future.result.assert_called_once_with(7)


def test_async_runtime_call_starts_background_loop_when_no_loop_is_ready() -> None:
    runtime = AsyncTelegramRuntime(Mock())
    runtime._ready = Mock()
    runtime._ready.wait.return_value = False
    background_loop = object()
    runtime._loop = None
    runtime._loop_thread_id = None

    def ensure_background_loop() -> None:
        runtime._loop = background_loop
        runtime._loop_thread_id = -1

    runtime._ensure_background_loop = Mock(side_effect=ensure_background_loop)
    future = Mock()
    future.result.return_value = "ok"

    async def coroutine_function() -> None:
        return None

    coroutine = coroutine_function()

    try:
        with patch("efb_telegram_master.transport.telegram_sync_bridge.asyncio.run_coroutine_threadsafe", return_value=future) as runner:
            assert runtime.call(coroutine) == "ok"
    finally:
        coroutine.close()

    runtime._ready.wait.assert_called_once_with(timeout=2.0)
    runtime._ensure_background_loop.assert_called_once_with()
    runner.assert_called_once_with(coroutine, background_loop)
    future.result.assert_called_once_with(None)


def test_async_runtime_delivery_shutdown_cancels_active_calls_and_rejects_new_calls() -> None:
    runtime = AsyncTelegramRuntime(Mock())
    future = Mock()
    runtime._active_calls.add(future)

    runtime.begin_delivery_shutdown()

    future.cancel.assert_called_once_with()

    async def coroutine_function() -> None:
        return None

    coroutine = coroutine_function()
    with pytest.raises(RuntimeError, match="runtime is stopping"):
        runtime.call(coroutine)


def test_async_runtime_stale_loop_clear_does_not_remove_rebound_loop() -> None:
    runtime = AsyncTelegramRuntime(Mock())
    current_loop = object()
    runtime._loop = current_loop
    runtime._loop_thread_id = -1
    runtime._loop_thread = Mock()
    runtime._owns_loop_thread = False
    runtime._ready.set()

    runtime.clear_loop(object())
