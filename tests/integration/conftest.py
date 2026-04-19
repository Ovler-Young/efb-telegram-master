import asyncio
import logging
import threading
import time
from collections.abc import AsyncGenerator
from typing import Set

import pytest
from telethon import TelegramClient

from .helper.helper import TelegramIntegrationTestHelper
from ..bot import get_user_session

pytest.register_assert_rewrite("tests.integration.utils")


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


@pytest.fixture(scope="session")
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


@pytest.fixture(scope="function", autouse=True)
async def rate_limit_delay():
    """
    Telegram Bot API rate limits are easy to hit in CI.
    Add a small delay between integration tests to reduce flakiness.
    """
    yield
    await asyncio.sleep(6)


@pytest.fixture(scope="session")
def poll_bot_factory():
    def start(channel):
        polling_errors = []

        def runner():
            try:
                channel.bot_manager.polling(drop_pending_updates=True)
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
            if channel.bot_manager._runtime._ready.wait(timeout=0.1):
                time.sleep(1)
                return polling_thread, polling_errors

        raise RuntimeError("Telegram bot polling did not become ready in time.")

    return start


@pytest.fixture(scope="module")
def poll_bot(channel, poll_bot_factory):
    logging.root.setLevel(logging.DEBUG)
    polling_thread, polling_errors = poll_bot_factory(channel)
    yield channel.bot_manager
    channel.bot_manager.graceful_stop()
    polling_thread.join(timeout=10)
    if polling_thread.is_alive():
        raise RuntimeError("Telegram bot polling thread did not stop in time.")
    if polling_errors:
        raise polling_errors[0]


@pytest.fixture(scope="session")
async def client(helper_wrap) -> AsyncGenerator[TelegramClient, None]:
    yield helper_wrap.client
