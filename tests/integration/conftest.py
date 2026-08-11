import asyncio
import inspect
import logging
import os
import sys
import threading
import time
import traceback
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Dict, Set

import pytest
import pytest_asyncio
from telethon import TelegramClient

from efb_telegram_master import TelegramChannel

from ..bot import get_user_session
from .helper.helper import TelegramIntegrationTestHelper, wait_for_private_response

pytest.register_assert_rewrite("tests.integration.utils")

POLLING_START_TIMEOUT = 30.0


def pytest_collection_modifyitems(items):
    """Keep the shared Telethon client and its consumers on one event loop."""
    integration_directory = Path(__file__).parent
    for item in items:
        if integration_directory in item.path.parents and inspect.iscoroutinefunction(item.obj):
            item.add_marker(pytest.mark.asyncio(loop_scope="session"), append=False)


@pytest.fixture(scope="session")
def user_session_info():
    return get_user_session()


@pytest.fixture(scope="session")
def user_session(user_session_info) -> str:
    return user_session_info["user_session"]


@pytest.fixture(scope="session")
def api_id(user_session_info) -> int:
    return user_session_info["api_id"]


@pytest.fixture(scope="session")
def api_hash(user_session_info) -> str:
    return user_session_info["api_hash"]


@pytest.fixture(scope="session")
def integration_postgres_config() -> Dict[str, object]:
    required = (
        "TEST_POSTGRES_HOST",
        "TEST_POSTGRES_PORT",
        "TEST_POSTGRES_DB",
        "TEST_POSTGRES_USER",
        "TEST_POSTGRES_PASSWORD",
    )
    if any(not os.environ.get(name) for name in required):
        pytest.skip("PostgreSQL integration environment is not configured")
    return {
        "type": "postgresql",
        "host": os.environ["TEST_POSTGRES_HOST"],
        "port": int(os.environ["TEST_POSTGRES_PORT"]),
        "database": os.environ["TEST_POSTGRES_DB"],
        "user": os.environ["TEST_POSTGRES_USER"],
        "password": os.environ["TEST_POSTGRES_PASSWORD"],
    }


@pytest.fixture(scope="session")
def filter_chats(bot_id, bot_groups, bot_channels, bot_topic_group) -> Set[int]:
    """Only receive updates from the following chats"""
    chats = set()
    chats.add(bot_id)
    chats = chats.union(bot_groups)
    chats = chats.union(bot_channels)
    if bot_topic_group is not None:
        chats.add(bot_topic_group)
    return chats


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def helper_wrap(user_session, api_id, api_hash, bot_id, filter_chats, aux_bot_ids) -> AsyncGenerator[TelegramIntegrationTestHelper, None]:
    loop = asyncio.get_running_loop()
    async with TelegramIntegrationTestHelper(user_session, api_id, api_hash, loop, [bot_id, *aux_bot_ids], chats=filter_chats) as helper:
        yield helper


@pytest.fixture(scope="function")
async def helper(helper_wrap, slave) -> AsyncGenerator[TelegramIntegrationTestHelper, None]:
    """Clean the message queue before each test."""
    helper_wrap.clear_queue()
    assert helper_wrap.queue.empty()
    slave.clear_messages()
    assert slave.messages.empty()
    slave.clear_statuses()
    assert slave.statuses.empty()
    try:
        yield helper_wrap
    finally:
        helper_wrap.clear_queue()


@pytest.fixture(scope="session")
def poll_bot_factory():
    state = {
        "channel": None,
        "thread": None,
        "errors": None,
        "lock": threading.Lock(),
    }

    def stop(expected_channel=None):
        channel = state["channel"]
        polling_thread = state["thread"]
        polling_errors = state["errors"]

        if channel is None or polling_thread is None:
            return
        if expected_channel is not None and channel is not expected_channel:
            return

        shutdown_error = None
        try:
            channel.stop_polling()
        except BaseException as error:
            shutdown_error = error
        finally:
            polling_thread.join(timeout=30)

        if polling_thread.is_alive():
            frame = sys._current_frames().get(polling_thread.ident)
            stack = "".join(traceback.format_stack(frame)) if frame is not None else "stack unavailable"
            print(f"Telegram bot polling thread did not stop: name={polling_thread.name!r} ident={polling_thread.ident!r}\n{stack}")
            raise RuntimeError("Telegram bot polling thread did not stop in time.") from shutdown_error
        state["channel"] = None
        state["thread"] = None
        state["errors"] = None
        if shutdown_error is not None:
            raise shutdown_error

        if polling_errors:
            raise polling_errors[0]

    def start(channel):
        with state["lock"]:
            if state["channel"] is channel and state["thread"] is not None:
                return

            stop()

            polling_errors = []

            def runner():
                try:
                    # Keep long polling short in tests so teardown can release the slot quickly.
                    channel.telegram_runtime.poll(drop_pending_updates=True, timeout=1)
                except BaseException as exc:  # pragma: no cover - test bootstrap path
                    polling_errors.append(exc)

            polling_thread = threading.Thread(
                target=runner,
                name=f"pytest-poll-bot-{channel.channel_id}",
            )
            polling_thread.start()
            state["channel"] = channel
            state["thread"] = polling_thread
            state["errors"] = polling_errors

            try:
                deadline = time.monotonic() + POLLING_START_TIMEOUT
                while time.monotonic() < deadline:
                    if polling_errors:
                        raise polling_errors[0]
                    remaining = deadline - time.monotonic()
                    runtime_ready = channel.telegram_runtime.async_runtime._ready.wait(timeout=min(0.1, remaining))
                    application = channel.telegram_runtime.application
                    updater = application.updater
                    if runtime_ready and updater is not None and updater.running and application.running:
                        if polling_errors:
                            raise polling_errors[0]
                        return
                raise RuntimeError("Telegram bot polling did not become ready in time.")
            except BaseException as startup_error:
                try:
                    stop(channel)
                except BaseException as cleanup_error:
                    print(f"Telegram bot polling startup cleanup failed: {cleanup_error!r}")
                raise startup_error

    class PollBotFactory:
        def start(self, channel):
            start(channel)

        def stop(self, channel=None):
            with state["lock"]:
                stop(channel)

    return PollBotFactory()


@pytest.fixture(scope="module")
def poll_bot(channel, poll_bot_factory):
    logging.root.setLevel(logging.DEBUG)
    poll_bot_factory.start(channel)
    yield channel.bot_manager
    poll_bot_factory.stop(channel)


@pytest.fixture(scope="function")
async def client(helper_wrap) -> AsyncGenerator[TelegramClient, None]:
    yield helper_wrap.client


def _primary_bot_limiter_delay(channel: TelegramChannel, target_chat_id: int) -> float:
    """Expose the production queue's main-bot limiter to response tests."""
    return channel.bot_manager.api._outbound_queue._sender_policy._main_rate_limiter.peek_delay(target_chat_id)


@pytest.fixture
def private_response(channel: TelegramChannel, bot_id: int, helper_wrap: TelegramIntegrationTestHelper):
    """Use the response deadline that includes the primary-bot rate-limit wait."""

    async def wait(trigger, receive, *, source_channel: TelegramChannel = channel, target_chat_id: int = bot_id):
        return await wait_for_private_response(
            lambda: _primary_bot_limiter_delay(source_channel, target_chat_id),
            trigger,
            receive,
            response_cursor=helper_wrap.event_cursor,
        )

    return wait
