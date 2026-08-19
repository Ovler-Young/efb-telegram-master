import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def poll_bot(channel_with_auxiliary_bots, poll_bot_factory):
    poll_bot_factory.start(channel_with_auxiliary_bots)
    yield channel_with_auxiliary_bots.bot_manager
    poll_bot_factory.stop(channel_with_auxiliary_bots)


async def test_auxiliary_bot_pool_initializes(poll_bot, channel_with_auxiliary_bots, aux_bot_ids):
    pool = channel_with_auxiliary_bots.bot_manager.api.bot_pool
    assert pool is not None
    assert len(pool.bots) == len(aux_bot_ids)
