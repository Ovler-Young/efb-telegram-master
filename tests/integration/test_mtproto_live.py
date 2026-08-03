import os

import pytest


@pytest.mark.asyncio
async def test_mtproto_bot_request_contract_live():
    """Reserved for credentials supplied only through the explicit live environment."""
    required = (
        "ETM_MTPROTO_LIVE",
        "ETM_MTPROTO_API_ID",
        "ETM_MTPROTO_API_HASH",
        "ETM_MTPROTO_BOT_TOKEN",
        "ETM_MTPROTO_CHANNEL",
    )
    if os.environ.get("ETM_MTPROTO_LIVE") != "1":
        pytest.skip("set ETM_MTPROTO_LIVE=1 to enable live MTProto validation")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.skip("missing MTProto live-test environment values: " + ", ".join(missing))

    pytest.fail("live MTProto request assertion has not been configured")
