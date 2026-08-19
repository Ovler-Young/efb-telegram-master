import pickle
import re
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot.chat import GroupChat, PrivateChat, SystemChat
from pytest import fixture

from efb_telegram_master.chat import chat as chat_module
from efb_telegram_master.chat.chat_codec import convert_chat, unpickle
from efb_telegram_master.chat.chat_member import ETMChatMember, ETMSelfChatMember, ETMSystemChatMember
from efb_telegram_master.chat.chat_types import ETMGroupChat, ETMPrivateChat, ETMSystemChat


@fixture(scope="module")
def db(channel):
    return channel.db


def test_etm_chat_name(db, slave):
    with_alias = convert_chat(db, slave.chat_with_alias)
    with_alias_full_name = with_alias.full_name
    assert with_alias.name in with_alias_full_name
    assert with_alias.alias in with_alias_full_name
    assert slave.channel_emoji in with_alias_full_name
    assert slave.channel_name in with_alias_full_name

    without_alias = convert_chat(db, slave.chat_without_alias)
    without_alias_full_name = without_alias.full_name
    assert without_alias.name in without_alias_full_name
    assert str(without_alias.alias) not in without_alias_full_name
    assert slave.channel_emoji in without_alias_full_name
    assert slave.channel_name in without_alias_full_name


def test_etm_chat_conversion_private(db, slave):
    private_chat = slave.get_chat_by_criteria(chat_type="PrivateChat")
    assert isinstance(private_chat, PrivateChat)
    etm_private_chat = convert_chat(db, private_chat)
    assert isinstance(etm_private_chat, ETMPrivateChat)
    assert isinstance(etm_private_chat.other, ETMChatMember)
    assert not isinstance(etm_private_chat.other, ETMSelfChatMember)
    assert isinstance(etm_private_chat.self, ETMSelfChatMember)
    assert etm_private_chat.other in etm_private_chat.members
    assert etm_private_chat.self in etm_private_chat.members
    assert all(isinstance(i, ETMChatMember) for i in etm_private_chat.members)
    assert len(etm_private_chat.members) == len(private_chat.members)


def test_etm_chat_conversion_system(db, slave):
    system_chat = slave.get_chat_by_criteria(chat_type="SystemChat")
    assert isinstance(system_chat, SystemChat)
    etm_system_chat = convert_chat(db, system_chat)
    assert isinstance(etm_system_chat, ETMSystemChat)
    assert isinstance(etm_system_chat.other, ETMSystemChatMember)
    assert isinstance(etm_system_chat.self, ETMSelfChatMember)
    assert etm_system_chat.other in etm_system_chat.members
    assert etm_system_chat.self in etm_system_chat.members
    assert all(isinstance(i, ETMChatMember) for i in etm_system_chat.members)
    assert len(etm_system_chat.members) == len(system_chat.members)


def test_etm_chat_conversion_group(db, slave):
    group_chat = slave.get_chat_by_criteria(chat_type="GroupChat")
    assert isinstance(group_chat, GroupChat)
    etm_group_chat = convert_chat(db, group_chat)
    assert isinstance(etm_group_chat, ETMGroupChat)
    assert isinstance(etm_group_chat.self, ETMSelfChatMember)
    assert etm_group_chat.self in etm_group_chat.members
    assert all(isinstance(i, ETMChatMember) for i in etm_group_chat.members)
    assert len(etm_group_chat.members) == len(group_chat.members)


def test_etm_chat_type_title_differ(db, slave):
    chat_name = "Chat Name"

    user = ETMPrivateChat(db, channel=slave, uid="__id__", name=chat_name)
    user_title = user.chat_title

    group = ETMGroupChat(db, channel=slave, uid="__id__", name=chat_name)
    group_title = group.chat_title

    sys = ETMSystemChat(db, channel=slave, uid="__id__", name=chat_name)
    sys_title = sys.chat_title

    assert len({user_title, group_title, sys_title}) == 3


def test_last_message_time_uses_ttl_cache():
    db = Mock()
    first_time = datetime(2026, 1, 1, 0, 0, 0)
    second_time = datetime(2026, 1, 1, 0, 1, 1)
    db.msglogs.get_last_message.side_effect = [
        SimpleNamespace(time=first_time),
        SimpleNamespace(time=second_time),
    ]
    chat = ETMPrivateChat(
        db,
        module_id="tests.mocks.slave",
        uid="__chat_id__",
        name="Chat",
    )

    with patch("efb_telegram_master.chat.chat.time.time", return_value=100.0):
        assert chat.last_message_time == first_time
        assert chat.last_message_time == first_time

    assert db.msglogs.get_last_message.call_count == 1

    with patch("efb_telegram_master.chat.chat.time.time", return_value=160.0):
        assert chat.last_message_time == first_time

    assert db.msglogs.get_last_message.call_count == 1

    with patch("efb_telegram_master.chat.chat.time.time", return_value=161.0):
        assert chat.last_message_time == second_time

    assert db.msglogs.get_last_message.call_count == 2


def test_etm_chat_instance_title_differ(db, slave):
    chat_name = "Chat Name"

    default_instance = ETMSystemChat(db, channel=slave, uid="__id__", name=chat_name)
    default_title = default_instance.chat_title

    custom_instance = default_instance.copy()
    custom_instance.module_id += "#custom"
    custom_title = custom_instance.chat_title

    assert default_title != custom_title


def test_etm_chat_match(db, slave):
    chat = convert_chat(db, slave.chat_with_alias)
    assert chat.match(chat.name)
    assert chat.match(chat.alias)
    assert chat.match(chat.module_name)
    assert chat.match(chat.uid)
    assert chat.match("type: private"), "case insensitive search"
    assert chat.match(re.compile("Channel ID: .+mock")), "re compile object search"
    assert chat.match("Mode: \n")

    assert chat.match(re.compile(f"Channel: {slave.channel_name}.*Type: Private", re.DOTALL | re.IGNORECASE)), "docs example #0"
    assert not chat.match("Alias: None"), "docs example #1"
    assert chat.match(re.compile(r"(?=.*Chat)(?=.*Channel)", re.DOTALL | re.IGNORECASE)), "docs example #2"

    no_alias = convert_chat(db, slave.chat_without_alias)
    assert no_alias.match("Alias: None")


def test_etm_chat_pickle(db, slave):
    chat = convert_chat(db, chat=slave.chat_with_alias)
    recovered = unpickle(chat.pickle, db)
    attributes = ("module_id", "module_name", "channel_emoji", "uid", "name", "alias", "notification", "vendor_specific", "full_name", "long_name", "chat_title")
    for i in attributes:
        assert getattr(chat, i) == getattr(recovered, i)
    assert chat.db is recovered.db


def test_etm_chat_unpickles_legacy_concrete_class_path(db, slave, monkeypatch):
    chat = convert_chat(db, chat=slave.chat_with_alias)
    with monkeypatch.context() as patcher:
        patcher.setattr(ETMPrivateChat, "__module__", "efb_telegram_master.chat")
        patcher.setattr(chat_module, "ETMPrivateChat", ETMPrivateChat, raising=False)
        legacy_pickle = pickle.dumps(chat)

    recovered = unpickle(legacy_pickle, db)

    assert type(recovered) is ETMPrivateChat
    assert not hasattr(chat_module, "ETMPrivateChat")


def test_etm_chat_unpickles_legacy_chat_type_and_member_paths(db, slave, monkeypatch):
    chat = convert_chat(db, chat=slave.chat_with_alias)
    legacy_types = ModuleType("efb_telegram_master.chat_types")
    legacy_members = ModuleType("efb_telegram_master.chat_member")
    legacy_types.ETMPrivateChat = ETMPrivateChat
    legacy_members.ETMChatMember = ETMChatMember
    legacy_members.ETMSelfChatMember = ETMSelfChatMember
    monkeypatch.setitem(sys.modules, legacy_types.__name__, legacy_types)
    monkeypatch.setitem(sys.modules, legacy_members.__name__, legacy_members)
    with monkeypatch.context() as patcher:
        patcher.setattr(ETMPrivateChat, "__module__", legacy_types.__name__)
        patcher.setattr(ETMChatMember, "__module__", legacy_members.__name__)
        patcher.setattr(ETMSelfChatMember, "__module__", legacy_members.__name__)
        legacy_pickle = pickle.dumps(chat)

    recovered = unpickle(legacy_pickle, db)

    assert type(recovered) is ETMPrivateChat
    assert type(recovered.other) is ETMChatMember
    assert type(recovered.self) is ETMSelfChatMember


def test_etm_chat_copy(db, slave):
    chat = convert_chat(db, chat=slave.chat_with_alias)
    copied = chat.copy()
    attributes = ("module_id", "module_name", "channel_emoji", "uid", "name", "alias", "notification", "vendor_specific", "full_name", "long_name", "chat_title")
    for i in attributes:
        assert getattr(chat, i) == getattr(copied, i)
    assert chat.db is copied.db
