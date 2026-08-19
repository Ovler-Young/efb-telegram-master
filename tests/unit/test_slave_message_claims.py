from types import SimpleNamespace

import pytest
from ehforwarderbot.constants import MsgType

from efb_telegram_master.slave_message_claims import SlaveMessageClaimLifecycle


@pytest.mark.parametrize(
    "message",
    [
        SimpleNamespace(edit=True, uid="message", type=MsgType.Text),
        SimpleNamespace(edit=False, uid=None, type=MsgType.Text),
        SimpleNamespace(edit=False, uid="message", type=MsgType.Status),
    ],
)
def test_dedupe_key_excludes_nondeliverable_message_forms(message) -> None:
    assert SlaveMessageClaimLifecycle.dedupe_key(message, "tests.slave chat") is None
