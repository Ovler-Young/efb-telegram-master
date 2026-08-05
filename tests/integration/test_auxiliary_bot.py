import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def poll_bot(channel_with_auxiliary_bots, poll_bot_factory):
    poll_bot_factory.start(channel_with_auxiliary_bots)
    yield channel_with_auxiliary_bots.bot_manager
    poll_bot_factory.stop(channel_with_auxiliary_bots)


@pytest.fixture(scope="function")
async def helper(helper_wrap, slave_with_auxiliary_bots):
    helper_wrap.clear_queue()
    assert helper_wrap.queue.empty()
    slave_with_auxiliary_bots.clear_messages()
    assert slave_with_auxiliary_bots.messages.empty()
    slave_with_auxiliary_bots.clear_statuses()
    assert slave_with_auxiliary_bots.statuses.empty()
    yield helper_wrap


async def test_auxiliary_bot_pool_initializes(channel_with_auxiliary_bots, aux_bot_ids):
    pool = channel_with_auxiliary_bots.bot_manager.api.bot_pool
    assert pool is not None
    assert len(pool.bots) == len(aux_bot_ids)


@pytest.mark.xfail(strict=False, reason="Depends on live Telegram throttling characteristics")
async def test_auxiliary_bot_sender_id_can_be_recorded(poll_bot, channel_with_auxiliary_bots, slave_with_auxiliary_bots):
    chat = slave_with_auxiliary_bots.chat_with_alias

    slave_with_auxiliary_bots.send_text_message(chat, chat.other)

    recent = channel_with_auxiliary_bots.db.get_last_message(chat.channel_id + "." + chat.uid)
    assert recent is not None
    assert recent.sender_bot_id is not None
