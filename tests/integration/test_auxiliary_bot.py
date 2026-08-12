import pytest

pytestmark = pytest.mark.asyncio


async def test_auxiliary_bot_pool_initializes(channel_with_auxiliary_bots, aux_bot_ids):
    pool = channel_with_auxiliary_bots.bot_manager.api.bot_pool
    assert pool is not None
    assert len(pool.bots) == len(aux_bot_ids)
