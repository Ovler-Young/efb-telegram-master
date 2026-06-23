import io

from pytest import fixture
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot import Message, Chat
from ehforwarderbot.constants import MsgType
from ehforwarderbot.chat import ChatMember
from ehforwarderbot.types import ReactionName
from efb_telegram_master.constants import Emoji
from efb_telegram_master.slave_message import SlaveMessageProcessor


def test_slave_message_reaction_footer(slave):
    # No content should be returned if no reaction is available
    assert not SlaveMessageProcessor.build_reactions_footer({})

    # Footer should contain the reaction name and number of reactors
    reactions = {
        ReactionName("__reaction_a__"):
            [slave.chat_with_alias, slave.chat_without_alias],
        ReactionName("__reaction_b__"):
            [slave.chat_with_alias],
        ReactionName("__reaction_c__"): []
    }
    footer = SlaveMessageProcessor.build_reactions_footer(reactions)
    assert "__reaction_a__" in footer
    assert "2" in footer
    assert "__reaction_b__" in footer
    assert "1" in footer
    assert "__reaction_c__" not in footer

    # Footer should be empty if no reaction name gives any value.
    footer = SlaveMessageProcessor.build_reactions_footer({
        ReactionName("__reaction_x__"): []
    })
    assert not footer


@fixture(scope="module")
def generate_message_template(channel):
    return channel.slave_messages.generate_message_template


@fixture(scope="module")
def private(slave):
    return slave.chat_with_alias


@fixture(scope="module")
def group(slave):
    return slave.group


@fixture(scope="module")
def group_member(slave):
    # Ensure the chat should have an alias
    for i in slave.group.members:
        if i.alias:
            return i
    return slave.group.members[0]


def build_dummy_message(chat: Chat, author: ChatMember) -> Message:
    message = Message()
    message.chat = chat
    message.author = author
    return message


def test_slave_message_generate_common_private(generate_message_template, private):
    message = build_dummy_message(private, private.other)
    header = generate_message_template(message, False)
    assert private.name in header
    assert private.alias in header
    assert private.channel_emoji in header
    assert Emoji.USER in header


def test_slave_message_generate_common_private_self(generate_message_template, private):
    message = build_dummy_message(private, private.self)
    header = generate_message_template(message, False)
    assert private.name in header
    assert private.alias in header
    assert private.channel_emoji in header
    assert private.self.name in header
    assert Emoji.USER in header


def test_slave_message_generate_common_linked(generate_message_template, private):
    message = build_dummy_message(private, private.other)
    header = generate_message_template(message, True)
    assert not header


def test_slave_message_generate_common_linked_self(generate_message_template, private):
    message = build_dummy_message(private, private.self)
    header = generate_message_template(message, True)
    assert private.name not in header
    assert private.alias not in header
    assert private.channel_emoji not in header
    assert private.self.name in header
    assert Emoji.USER not in header


def test_slave_message_generate_group_private(generate_message_template, group, group_member):
    message = build_dummy_message(group, group_member)
    header = generate_message_template(message, False)
    assert group.name in header
    assert group.alias in header
    assert group.channel_emoji in header
    assert group_member.name in header
    assert group_member.alias in header
    assert Emoji.GROUP in header


def test_slave_message_generate_group_private_self(generate_message_template, group):
    message = build_dummy_message(group, group.self)
    header = generate_message_template(message, False)
    assert group.name in header
    assert group.alias in header
    assert group.channel_emoji in header
    assert group.self.name in header
    assert Emoji.GROUP in header


def test_slave_message_generate_group_linked(generate_message_template, group, group_member):
    message = build_dummy_message(group, group_member)
    header = generate_message_template(message, True)
    assert group.name not in header
    assert group.alias not in header
    assert group.channel_emoji not in header
    assert Emoji.GROUP not in header
    assert group_member.name in header
    assert group_member.alias in header


def test_slave_message_generate_group_linked_with_user_avatar_emoji(channel, group, group_member):
    message = build_dummy_message(group, group_member)
    old_user_emoji = channel.config.get("user_emoji")

    try:
        channel.config["user_emoji"] = {
            "enabled": True,
        }
        with patch.object(
            channel.slave_messages,
            "_user_avatar_custom_emoji_prefix",
            return_value="\x00ETM_CUSTOM_EMOJI:12345\x00",
        ):
            header = channel.slave_messages.generate_message_template(message, True)
    finally:
        if old_user_emoji is None:
            channel.config.pop("user_emoji", None)
        else:
            channel.config["user_emoji"] = old_user_emoji

    assert header.startswith("\x00ETM_CUSTOM_EMOJI:12345\x00 ")
    assert group_member.name in header
    assert group_member.alias in header


def test_user_avatar_custom_emoji_prefix_passes_chat_member_picture_loader(channel, slave, group, group_member):
    message = build_dummy_message(group, group_member)
    old_user_emoji = channel.config.get("user_emoji")
    picture = io.BytesIO(b"avatar")

    try:
        channel.config["user_emoji"] = {
            "enabled": True,
        }
        with patch.object(slave, "get_chat_member_picture", return_value=picture, create=True) as get_chat_member_picture, \
             patch.object(slave, "get_chat_picture") as get_chat_picture, \
             patch.object(
                 channel.chat_binding,
                 "resolve_user_avatar_custom_emoji_id_lazy",
                 return_value="12345",
             ) as resolve:
            prefix = channel.slave_messages._user_avatar_custom_emoji_prefix(message)
    finally:
        if old_user_emoji is None:
            channel.config.pop("user_emoji", None)
        else:
            channel.config["user_emoji"] = old_user_emoji

    assert prefix == "\x00ETM_CUSTOM_EMOJI:12345\x00"
    resolve.assert_called_once()
    assert resolve.call_args.args[0] == f"{group_member.module_id} {group_member.uid}"
    loaded_picture, picture_source = resolve.call_args.args[1]()
    assert loaded_picture is picture
    assert picture_source == "member"
    get_chat_member_picture.assert_called_once_with(group_member)
    get_chat_picture.assert_not_called()


def test_user_avatar_custom_emoji_prefix_passes_chat_picture_loader(channel, slave, group, group_member):
    message = build_dummy_message(group, group_member)
    old_user_emoji = channel.config.get("user_emoji")
    picture = io.BytesIO(b"avatar")
    old_get_chat_member_picture = getattr(slave, "get_chat_member_picture", None)

    try:
        channel.config["user_emoji"] = {
            "enabled": True,
        }
        if hasattr(slave, "get_chat_member_picture"):
            delattr(slave, "get_chat_member_picture")
        with patch.object(slave, "get_chat_picture", return_value=picture) as get_chat_picture, \
             patch.object(
                 channel.chat_binding,
                 "resolve_user_avatar_custom_emoji_id_lazy",
                 return_value="12345",
             ) as resolve:
            prefix = channel.slave_messages._user_avatar_custom_emoji_prefix(message)
    finally:
        if old_get_chat_member_picture is not None:
            slave.get_chat_member_picture = old_get_chat_member_picture
        if old_user_emoji is None:
            channel.config.pop("user_emoji", None)
        else:
            channel.config["user_emoji"] = old_user_emoji

    assert prefix == "\x00ETM_CUSTOM_EMOJI:12345\x00"
    resolve.assert_called_once()
    assert resolve.call_args.args[0] == f"{group_member.module_id} {group_member.uid}"
    loaded_picture, picture_source = resolve.call_args.args[1]()
    assert loaded_picture is picture
    assert picture_source == "chat"
    get_chat_picture.assert_called_once_with(group_member)


def test_user_avatar_custom_emoji_prefix_uses_default_user_emoji_config(channel, slave, group, group_member):
    message = build_dummy_message(group, group_member)
    old_user_emoji = channel.config.get("user_emoji")
    picture = io.BytesIO(b"avatar")

    try:
        channel.config.pop("user_emoji", None)
        with patch.object(slave, "get_chat_member_picture", return_value=picture, create=True) as get_chat_member_picture, \
             patch.object(
                 channel.chat_binding,
                 "resolve_user_avatar_custom_emoji_id_lazy",
                 return_value="12345",
             ) as resolve:
            prefix = channel.slave_messages._user_avatar_custom_emoji_prefix(message)
    finally:
        if old_user_emoji is not None:
            channel.config["user_emoji"] = old_user_emoji

    assert prefix == "\x00ETM_CUSTOM_EMOJI:12345\x00"
    resolve.assert_called_once()
    loaded_picture, picture_source = resolve.call_args.args[1]()
    assert loaded_picture is picture
    assert picture_source == "member"
    get_chat_member_picture.assert_called_once_with(group_member)


def test_slave_message_generate_private_with_user_avatar_emoji(channel, private):
    message = build_dummy_message(private, private.other)

    with patch.object(
        channel.slave_messages,
        "_user_avatar_custom_emoji_prefix",
        return_value="\x00ETM_CUSTOM_EMOJI:12345\x00",
    ):
        header = channel.slave_messages.generate_message_template(message, False)

    assert "\x00ETM_CUSTOM_EMOJI:12345\x00" in header
    assert private.other.long_name in header


def test_slave_message_generate_group_linked_self(generate_message_template, group):
    message = build_dummy_message(group, group.self)
    header = generate_message_template(message, True)
    assert group.name not in header
    assert group.alias not in header
    assert group.channel_emoji not in header
    assert Emoji.GROUP not in header
    assert group.self.name in header


@fixture(scope="module")
def build_inline_keyboard(channel):
    return channel.slave_messages.build_chat_info_inline_keyboard


def keyboard_to_sequence(markup: InlineKeyboardMarkup) -> str:
    x = []
    for row in markup.inline_keyboard:
        x.append(f"[{', '.join(button.text for button in row)}]")
    return f"[{', '.join(x)}]"


def test_build_inline_keyboard_empty(build_inline_keyboard, private):
    msg = build_dummy_message(private, private.other)
    keyboard = build_inline_keyboard(msg, "", "", None)
    seq = keyboard_to_sequence(keyboard)
    assert seq == '[]'


def test_build_inline_keyboard_full(build_inline_keyboard, private):
    msg = build_dummy_message(private, private.other)
    msg.text = "__text__"
    keyboard = build_inline_keyboard(msg, "__template__", "__reactions__", None)
    seq = keyboard_to_sequence(keyboard)
    assert "__text__" in seq
    assert "__template__" in seq
    assert "__reactions__" in seq


def test_build_inline_keyboard_existing_buttons(build_inline_keyboard, private):
    msg = build_dummy_message(private, private.other)
    msg.text = "__text__"
    markup = InlineKeyboardMarkup.from_row([
        InlineKeyboardButton("__button_a__"),
        InlineKeyboardButton("__button_b__"),
    ])
    keyboard = build_inline_keyboard(msg, "__template__", "__reactions__", markup)
    seq = keyboard_to_sequence(keyboard)
    assert "__button_a__" in seq
    assert "__button_b__" in seq
    assert "__text__" in seq
    assert "__template__" in seq
    assert "__reactions__" in seq


def test_reaction_target_prefers_primary_message_for_slave_origin_text():
    old_msg = SimpleNamespace(
        type=MsgType.Text,
        chat=SimpleNamespace(module_id="tests.mocks.slave"),
        deliver_to=SimpleNamespace(channel_id="blueset.telegram"),
    )
    old_msg_db = SimpleNamespace(
        master_msg_id="100.10",
        master_msg_id_alt="100.11",
    )

    effective = SlaveMessageProcessor._reaction_target_message_id(old_msg, old_msg_db)

    assert effective == "100.10"


def test_reaction_target_prefers_alt_message_for_telegram_origin_text():
    old_msg = SimpleNamespace(
        type=MsgType.Text,
        chat=SimpleNamespace(module_id="tests.mocks.slave"),
        deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
    )
    old_msg_db = SimpleNamespace(
        master_msg_id="100.10",
        master_msg_id_alt="100.11",
    )

    effective = SlaveMessageProcessor._reaction_target_message_id(old_msg, old_msg_db)

    assert effective == "100.11"


def test_update_reactions_waits_for_delayed_database_log():
    processor = Mock(spec=SlaveMessageProcessor)
    processor.REACTION_DB_WAIT_TIMEOUT = 1.0
    processor.REACTION_DB_WAIT_INTERVAL = 0.01
    processor.chat_manager = Mock()
    processor.dispatch_message = Mock()
    processor.get_slave_msg_dest = Mock(return_value=("__template__", (100, None)))
    processor.logger = Mock()
    processor._reaction_target_message_id = SlaveMessageProcessor._reaction_target_message_id

    old_msg = SimpleNamespace(
        type=MsgType.Text,
        reactions={},
        vendor_specific=None,
        chat=SimpleNamespace(module_id="tests.mocks.slave"),
        deliver_to=SimpleNamespace(channel_id="blueset.telegram"),
    )
    old_msg_db = SimpleNamespace(
        master_msg_id="100.10",
        master_msg_id_alt=None,
        sender_bot_id=None,
        build_etm_msg=Mock(return_value=old_msg),
    )
    processor.db = Mock()
    processor.db.get_msg_log.side_effect = [None, old_msg_db]

    status = SimpleNamespace(
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat__"),
        msg_id="__msg_id_reaction__",
        reactions={"R0": [object()]},
    )

    SlaveMessageProcessor.update_reactions(processor, status)

    assert processor.db.get_msg_log.call_count == 2
    assert old_msg.reactions == status.reactions
    processor.dispatch_message.assert_called_once_with(old_msg, "__template__", (100, 10), 100, None)
    processor.logger.error.assert_not_called()
