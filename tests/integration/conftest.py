import asyncio
import inspect
import logging
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Set

import pytest
import pytest_asyncio
from telethon import TelegramClient

from .helper.helper import TelegramIntegrationTestHelper
from ..bot import get_user_session

pytest.register_assert_rewrite("tests.integration.utils")


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
    return user_session_info['user_session']


@pytest.fixture(scope="session")
def api_id(user_session_info) -> int:
    return user_session_info['api_id']


@pytest.fixture(scope="session")
def api_hash(user_session_info) -> str:
    return user_session_info['api_hash']


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
async def helper_wrap(user_session, api_id, api_hash, bot_id,
                      filter_chats, aux_bot_ids) -> AsyncGenerator[TelegramIntegrationTestHelper, None]:
    loop = asyncio.get_running_loop()
    async with TelegramIntegrationTestHelper(
            user_session, api_id, api_hash, loop, [bot_id, *aux_bot_ids],
            chats=filter_chats
    ) as helper:
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
    yield helper_wrap


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

        still_alive = False
        try:
            channel.bot_manager.graceful_stop()
            polling_thread.join(timeout=30)
            still_alive = polling_thread.is_alive()
        finally:
            state["channel"] = None
            state["thread"] = None
            state["errors"] = None

        if still_alive:
            raise RuntimeError("Telegram bot polling thread did not stop in time.")

        # Telegram may take a moment to release the previous long-poll slot.
        time.sleep(2)

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
                    channel.bot_manager.polling(drop_pending_updates=True, timeout=1)
                except BaseException as exc:  # pragma: no cover - test bootstrap path
                    polling_errors.append(exc)

            polling_thread = threading.Thread(
                target=runner,
                name=f"pytest-poll-bot-{channel.channel_id}",
                daemon=True,
            )
            polling_thread.start()

            deadline = time.time() + 30
            while time.time() < deadline:
                if polling_errors:
                    raise polling_errors[0]
                runtime_ready = channel.bot_manager._runtime._ready.wait(timeout=0.1)
                application = channel.bot_manager.application
                updater = application.updater
                if (
                    runtime_ready
                    and updater is not None
                    and updater.running
                    and application.running
                ):
                    if polling_errors:
                        raise polling_errors[0]
                    state["channel"] = channel
                    state["thread"] = polling_thread
                    state["errors"] = polling_errors
                    return

            raise RuntimeError("Telegram bot polling did not become ready in time.")

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
