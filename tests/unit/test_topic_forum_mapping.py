"""A ``topic_group`` mapping sends each chat type to its own forum."""
from itertools import count
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ehforwarderbot import Message
from ehforwarderbot.chat import PrivateChat
from ehforwarderbot.types import ChatID
from telegram import Update

from efb_telegram_master import utils
from efb_telegram_master.member_selection import current_source_route
from efb_telegram_master.utils import (
    ExperimentalFlagsManager,
    TelegramChatID,
    TelegramTopicID,
)

PRIVATE_FORUM = -1001000000001
GROUP_FORUM = -1001000000002
OFFICIAL_FORUM = -1001000000003
OLD_FORUM = -1001000000009
MAPPING = {"private": PRIVATE_FORUM, "group": GROUP_FORUM, "official_account": OFFICIAL_FORUM}

_thread_ids = count(5000)


@pytest.fixture
def chats(slave):
    official = PrivateChat(channel=slave, name="Official Account", uid=ChatID("official-account-chat"),
                           vendor_specific={"is_mp": True})
    return SimpleNamespace(
        private=slave.chats_by_chat_type["PrivateChat"][0],
        group=slave.chats_by_chat_type["GroupChat"][0],
        official=official,
        system=slave.chats_by_chat_type["SystemChat"][0],
    )


@pytest.fixture
def forum(channel, chats):
    """Mapping config with a recording fake of the Telegram forum-topic call; real database rows."""
    created = []

    def create_forum_topic(chat_id, name, **kwargs):
        created.append(chat_id)
        return SimpleNamespace(message_thread_id=next(_thread_ids))

    for chat in (chats.private, chats.group, chats.official, chats.system):
        channel.db.remove_topic_assoc(slave_uid=utils.chat_id_to_str(chat=chat))
        channel.db.remove_chat_assoc(slave_uid=utils.chat_id_to_str(chat=chat))
    with patch.object(channel.bot_manager, "get_chat_info", return_value=SimpleNamespace(is_forum=True)), \
         patch.object(channel.bot_manager, "create_forum_topic", side_effect=create_forum_topic), \
         patch.object(channel, "topic_group", MAPPING):
        channel.slave_messages._known_forum_chat_ids.clear()
        yield SimpleNamespace(created=created)
    for chat in (chats.private, chats.group, chats.official, chats.system):
        channel.db.remove_topic_assoc(slave_uid=utils.chat_id_to_str(chat=chat))


def _send(channel, chat):
    msg = Message()
    msg.uid = "forum-mapping-test"
    msg.chat = chat
    msg.author = chat.self
    msg.text = "text"
    _, (tg_dest, thread_id) = channel.slave_messages.get_slave_msg_dest(msg)
    return tg_dest, thread_id


def _route(channel, chat):
    return current_source_route(channel, utils.chat_id_to_str(chat=chat))


@pytest.mark.parametrize("kind,expected_forum", [
    ("private", PRIVATE_FORUM), ("group", GROUP_FORUM), ("official", OFFICIAL_FORUM)])
def test_each_chat_type_gets_a_topic_in_its_own_forum(channel, chats, forum, kind, expected_forum):
    chat = getattr(chats, kind)
    slave_uid = utils.chat_id_to_str(chat=chat)

    tg_dest, thread_id = _send(channel, chat)

    assert tg_dest == expected_forum
    assert thread_id is not None
    assert channel.db.get_topic_thread_id(slave_uid, TelegramChatID(expected_forum)) == thread_id
    assert channel.db.get_topic_chat_id(slave_uid) == expected_forum
    assert _route(channel, chat) == (expected_forum, str(thread_id))

    assert _send(channel, chat) == (expected_forum, thread_id)
    assert forum.created == [expected_forum]


def test_system_chat_stays_in_main_chat(channel, chats, forum):
    tg_dest, thread_id = _send(channel, chats.system)

    assert tg_dest == channel.config["admins"][0]
    assert thread_id is None
    assert forum.created == []
    assert _route(channel, chats.system) == (channel.config["admins"][0], None)


def test_missing_type_falls_back_to_private_then_to_main_chat(channel, chats, forum):
    with patch.object(channel, "topic_group", {"private": PRIVATE_FORUM}):
        assert _send(channel, chats.group)[0] == PRIVATE_FORUM
        assert _send(channel, chats.official)[0] == PRIVATE_FORUM
    for chat in (chats.group, chats.official):
        channel.db.remove_topic_assoc(slave_uid=utils.chat_id_to_str(chat=chat))

    with patch.object(channel, "topic_group", {"group": GROUP_FORUM}):
        tg_dest, thread_id = _send(channel, chats.private)
        assert (tg_dest, thread_id) == (channel.config["admins"][0], None)
        assert _route(channel, chats.private) == (channel.config["admins"][0], None)


def test_existing_topic_stays_when_config_becomes_a_mapping(channel, chats, forum):
    slave_uid = utils.chat_id_to_str(chat=chats.private)
    channel.db.add_topic_assoc(TelegramChatID(OLD_FORUM), TelegramTopicID(777), slave_uid)

    tg_dest, thread_id = _send(channel, chats.private)

    assert (tg_dest, thread_id) == (OLD_FORUM, 777)
    assert forum.created == []
    assert _route(channel, chats.private) == (OLD_FORUM, "777")
    # A chat without a topic still follows the mapping.
    assert _send(channel, chats.group)[0] == GROUP_FORUM


def test_single_forum_config_still_uses_one_forum_for_every_chat(channel, chats, forum):
    with patch.object(channel, "topic_group", PRIVATE_FORUM):
        assert _send(channel, chats.group)[0] == PRIVATE_FORUM
        assert _route(channel, chats.group)[0] == PRIVATE_FORUM


def _start_update(channel, chat_id):
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "text": "/start token",
                "chat": {"id": chat_id, "type": "supergroup", "title": "Group"},
                "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
            },
        },
        channel.bot_manager._async_bot,
    )


@pytest.mark.parametrize("chat_id,accepted", [
    (PRIVATE_FORUM, False), (GROUP_FORUM, False), (OFFICIAL_FORUM, False), (-1002000000000, True)])
def test_start_link_refused_in_every_configured_forum(channel, chat_id, accepted):
    update = _start_update(channel, chat_id)
    with patch.object(channel, "topic_group", MAPPING), \
         patch.object(channel.chat_binding, "link_chat") as link_chat, \
         patch.object(channel.bot_manager, "send_message") as send_message:
        channel.start(update, SimpleNamespace(args=["token"]))

    assert link_chat.called is accepted
    assert send_message.called is not accepted


@pytest.mark.parametrize("value", [
    None, 123, {}, {"private": 1}, MAPPING, {"group": -5, "official_account": -6}])
def test_topic_group_flag_accepts_chat_id_or_type_mapping(channel, value):
    with patch.dict(channel.config, {"flags": {"topic_group": value}}):
        assert ExperimentalFlagsManager(channel)("topic_group") == value


@pytest.mark.parametrize("value", [
    {"system": 1}, {"unknown": 1}, {"group": "x"}, {"group": 1.5}, {"group": True}, {"private": None},
    [1, 2], [], {1: -100}, True])
def test_topic_group_flag_rejects_bad_shapes(channel, value):
    with patch.dict(channel.config, {"flags": {"topic_group": value}}), \
         pytest.raises(ValueError, match="topic_group"):
        ExperimentalFlagsManager(channel)


@pytest.mark.parametrize("config", [PRIVATE_FORUM, MAPPING], ids=["single_forum", "mapping"])
def test_route_of_a_chat_that_can_no_longer_be_resolved_matches_the_sender(channel, chats, forum, config):
    with patch.object(channel, "topic_group", config):
        sent = _send(channel, chats.official)
        channel.chat_manager.cache.pop((chats.official.module_id, chats.official.uid), None)
        channel.db.delete_slave_chat_info(chats.official.module_id, chats.official.uid)

        assert _route(channel, chats.official) == (sent[0], str(sent[1]))


def test_every_database_metric_label_is_allowed():
    import re
    from pathlib import Path

    from efb_telegram_master import db, etm_metrics
    labels = re.findall(r'@observe_database_method\("(\w+)"\)', Path(db.__file__).read_text())
    assert labels
    assert set(labels) <= etm_metrics._DATABASE_METHODS
