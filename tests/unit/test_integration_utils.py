from types import SimpleNamespace
from typing import cast

import pytest

from efb_telegram_master import utils as etm_utils
from efb_telegram_master.chat_destination_cache import ChatDestinationCache
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID
from tests.integration import test_master_message_destination as destination_tests
from tests.integration import utils as integration_utils


def test_decode_start_link_token_rejects_a_mismatched_owner() -> None:
    start_token = etm_utils.b64en(etm_utils.message_id_to_str(TelegramChatID(1), TelegramMessageID(27023)))

    with pytest.raises(AssertionError, match="does not match expected owner"):
        integration_utils.decode_start_link_token(start_token, expected_owner=TelegramChatID(2))


def test_link_chats_restores_captured_associations_after_a_context_failure() -> None:
    associations = Associations({"blueset.telegram 500": ["slave old-a", "slave old-b"], "other.master": ["slave.other"]})
    channel = cast(integration_utils.TelegramChannel, SimpleNamespace(channel_id="blueset.telegram", chat_associations=associations))
    slave_chats = (SimpleNamespace(module_id="slave", uid="new-a"), SimpleNamespace(module_id="slave", uid="new-b"))

    with pytest.raises(RuntimeError, match="context failed"):
        with integration_utils.link_chats(channel, slave_chats, 500):
            associations.add_chat_assoc("blueset.telegram 500", "slave added-during-test", multiple_slave=True)
            raise RuntimeError("context failed")

    assert associations.state == {"blueset.telegram 500": ["slave old-a", "slave old-b"], "other.master": ["slave.other"]}


def test_expired_destination_lookup_restores_the_cache_snapshot() -> None:
    cache = ChatDestinationCache("enabled", size=3)
    cache.set("unrelated-first", "slave first")
    cache.set("expired", "slave expired")
    cache.set("unrelated-last", "slave last")
    original_weak_items = tuple(cache.weak.items())
    original_strong_entries = tuple(cache.strong)
    expired = cache.weak["expired"]
    original_expiry = expired.expiry

    with destination_tests.preserve_destination_cache(cache):
        expired.expiry = 0
        assert cache.get("expired") is None
        cache.set("created-during-test", "slave created")

    assert tuple(cache.weak.items()) == original_weak_items
    assert tuple(cache.strong) == original_strong_entries
    assert cache.weak["expired"] is expired
    assert expired.expiry == original_expiry
    assert "created-during-test" not in cache.weak


class Associations:
    def __init__(self, state: dict[str, list[str]]) -> None:
        self.state = {master: list(slaves) for master, slaves in state.items()}

    def get_chat_assoc(self, *, master_uid: str) -> list[str]:
        return list(self.state.get(master_uid, []))

    def remove_chat_assoc(self, *, master_uid: str) -> None:
        self.state.pop(master_uid, None)

    def add_chat_assoc(self, master_uid: str, slave_uid: str, *, multiple_slave: bool) -> None:
        assert multiple_slave
        self.state.setdefault(master_uid, []).append(slave_uid)
