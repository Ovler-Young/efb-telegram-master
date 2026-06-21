from datetime import datetime
from types import SimpleNamespace

from pytest import fixture
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from ehforwarderbot import Message, Chat, MsgType
from ehforwarderbot.types import MessageID
from efb_telegram_master import utils
from ehforwarderbot.types import ReactionName
from efb_telegram_master.constants import Emoji
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.slave_message import SlaveMessageProcessor
from efb_telegram_master.utils import TelegramChatID


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


def build_dummy_message(chat: Chat, author: Chat) -> Message:
    message = Message()
    message.chat = chat
    message.author = author
    return message


def test_slave_message_generate_common_private(generate_message_template, private):
    message = build_dummy_message(private, private)
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
    message = build_dummy_message(private, private)
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
    msg = build_dummy_message(private, private)
    keyboard = build_inline_keyboard(msg, "", "", None)
    seq = keyboard_to_sequence(keyboard)
    assert seq == '[]'


def test_build_inline_keyboard_full(build_inline_keyboard, private):
    msg = build_dummy_message(private, private)
    msg.text = "__text__"
    keyboard = build_inline_keyboard(msg, "__template__", "__reactions__", None)
    seq = keyboard_to_sequence(keyboard)
    assert "__text__" in seq
    assert "__template__" in seq
    assert "__reactions__" in seq


def test_build_inline_keyboard_existing_buttons(build_inline_keyboard, private):
    msg = build_dummy_message(private, private)
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


def _log_message(channel, chat, author, *, text, master_msg_id, slave_msg_id, msg_type=MsgType.Text,
                 tg_msg_type=TGMsgType.Text):
    cached_chat = channel.chat_manager.update_chat_obj(chat, full_update=True)
    etm_msg = ETMMsg(
        uid=MessageID(slave_msg_id),
        chat=cached_chat,
        author=channel.chat_manager.get_or_enrol_member(cached_chat, author),
        text=text,
        type=msg_type,
        type_telegram=tg_msg_type,
        deliver_to=channel,
    )
    chat_id, message_id = master_msg_id.split(".")
    channel.db.add_or_update_message_log(
        etm_msg,
        SimpleNamespace(chat_id=int(chat_id), message_id=int(message_id)),
    )
    row = channel.db.get_msg_log(master_msg_id=master_msg_id)
    row.time = datetime.now()
    row.save()
    return row


def test_wechat_text_quote_resolves_unique_sender_target(channel, slave):
    processor = SlaveMessageProcessor(channel)
    chat = slave.group
    quoted_author = chat.members[0]
    reply_author = chat.members[1]
    original = _log_message(
        channel,
        chat,
        quoted_author,
        text="这边1点半上班",
        master_msg_id="12345.10",
        slave_msg_id="quoted-text",
    )
    try:
        msg = Message(
            uid=MessageID("reply-text"),
            chat=chat,
            author=reply_author,
            text=f"{reply_author.alias or reply_author.name}:\n"
                 f"「{quoted_author.alias or quoted_author.name}：这边1点半上班」\n"
                 "- - - - - - - - - - - - - - -\n"
                 "中间休息时间短，应该下班时间早吧",
            type=MsgType.Text,
        )

        target = processor._find_wechat_quote_target(msg, TelegramChatID(12345))

        assert target == 10
        assert getattr(msg, "_expandable_quote", False) is True
    finally:
        original.delete_instance()


def test_wechat_short_text_quote_requires_unique_candidate(channel, slave):
    processor = SlaveMessageProcessor(channel)
    chat = slave.group
    quoted_author = chat.members[0]
    rows = [
        _log_message(channel, chat, quoted_author, text="测试一下", master_msg_id="12345.20", slave_msg_id="short-1"),
        _log_message(channel, chat, quoted_author, text="测试", master_msg_id="12345.21", slave_msg_id="short-2"),
    ]
    try:
        msg = Message(
            uid=MessageID("reply-short"),
            chat=chat,
            author=chat.members[1],
            text=f"「{quoted_author.alias or quoted_author.name}：测试」\n"
                 "- - - - - - - - - - - - - - -\n"
                 "回复",
            type=MsgType.Text,
        )

        assert processor._find_wechat_quote_target(msg, TelegramChatID(12345)) is None
        assert not getattr(msg, "_expandable_quote", False)
    finally:
        for row in rows:
            row.delete_instance()


def test_wechat_media_quote_requires_unique_candidate(channel, slave):
    processor = SlaveMessageProcessor(channel)
    chat = slave.group
    quoted_author = chat.members[0]
    rows = [
        _log_message(channel, chat, quoted_author, text="first image", master_msg_id="12345.30",
                     slave_msg_id="image-1", msg_type=MsgType.Image, tg_msg_type=TGMsgType.Photo),
        _log_message(channel, chat, quoted_author, text="second image", master_msg_id="12345.31",
                     slave_msg_id="image-2", msg_type=MsgType.Image, tg_msg_type=TGMsgType.Photo),
    ]
    try:
        msg = Message(
            uid=MessageID("reply-image"),
            chat=chat,
            author=chat.members[1],
            text=f"「{quoted_author.alias or quoted_author.name}：[图片]」\n"
                 "- - - - - - - - - - - - - - -\n"
                 "感觉",
            type=MsgType.Text,
        )

        assert processor._find_wechat_quote_target(msg, TelegramChatID(12345)) is None
        assert not getattr(msg, "_expandable_quote", False)
    finally:
        for row in rows:
            row.delete_instance()


def test_add_or_update_message_log_persists_member_display_name(channel, slave):
    chat = slave.group
    author = chat.members[0]
    row = _log_message(
        channel,
        chat,
        author,
        text="display name",
        master_msg_id="12345.40",
        slave_msg_id="display-name",
    )
    try:
        assert row.slave_member_display_name == (author.alias or author.name)
        assert utils.chat_id_to_str(chat=author) in channel.db.find_member_uids_by_display_name(
            utils.chat_id_to_str(chat=chat),
            author.alias or author.name,
        )
    finally:
        row.delete_instance()
