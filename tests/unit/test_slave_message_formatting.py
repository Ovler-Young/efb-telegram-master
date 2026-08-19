from io import BytesIO

import pytest
from ehforwarderbot import Chat, Message
from ehforwarderbot.chat import ChatMember
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from efb_telegram_master.constants import Emoji
from efb_telegram_master.slave_delivery_helpers import chat_info_keyboard, reactions_footer
from tests.mocks.slave import MockSlaveChannel


def test_reaction_footer_omits_empty_reactions() -> None:
    assert reactions_footer({}) == ""
    assert reactions_footer({"ok": [object(), object()], "empty": []}) == "[ok\u00d72]"


def test_mock_slave_message_snapshot_survives_producer_file_close() -> None:
    message = Message(file=BytesIO(b"image-bytes"), filename="image.png", mime="image/png")

    snapshot = MockSlaveChannel._snapshot_message(message)
    message.file.close()

    assert snapshot is not message
    assert snapshot.filename == "image.png"
    assert snapshot.mime == "image/png"
    assert snapshot.file.read() == b"image-bytes"


@pytest.fixture(scope="module")
def generate_message_template(channel):
    return channel.message_service.router.generate_message_template


@pytest.fixture(scope="module")
def private(slave):
    return slave.chat_with_alias


@pytest.fixture(scope="module")
def group(slave):
    return slave.group


@pytest.fixture(scope="module")
def group_member(slave):
    return next(member for member in slave.group.members if member.alias)


def _template_message(chat: Chat, author: ChatMember) -> Message:
    message = Message()
    message.chat = chat
    message.author = author
    return message


@pytest.mark.parametrize(
    ("chat_fixture", "author_kind", "linked", "expected", "absent"),
    [
        ("private", "other", False, (Emoji.USER,), ()),
        ("private", "self", False, (Emoji.USER,), ()),
        ("private", "other", True, (), (Emoji.USER,)),
        ("private", "self", True, (), (Emoji.USER,)),
        ("group", "member", False, (Emoji.GROUP,), ()),
        ("group", "self", False, (Emoji.GROUP,), ()),
        ("group", "member", True, (), (Emoji.GROUP,)),
        ("group", "self", True, (), (Emoji.GROUP,)),
    ],
)
def test_message_templates_cover_private_group_and_linked_variants(request, generate_message_template, group_member, chat_fixture, author_kind, linked, expected, absent) -> None:
    chat = request.getfixturevalue(chat_fixture)
    author = group_member if author_kind == "member" else getattr(chat, author_kind)
    template = generate_message_template(_template_message(chat, author), linked)

    for value in expected:
        assert value in template
    for value in absent:
        assert value not in template
    if linked and author_kind == "other":
        assert template == ""


@pytest.fixture(scope="module")
def build_inline_keyboard(channel):
    return chat_info_keyboard


@pytest.mark.parametrize("existing", [None, InlineKeyboardMarkup.from_row([InlineKeyboardButton("existing")])])
def test_inline_keyboard_preserves_existing_buttons_and_metadata(build_inline_keyboard, private, existing) -> None:
    message = _template_message(private, private.other)
    message.text = "text"
    keyboard = build_inline_keyboard(message, "template", "reactions", existing)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "text" in labels
    assert "template" in labels
    assert "reactions" in labels
    if existing:
        assert "existing" in labels


def test_inline_keyboard_is_empty_without_metadata(build_inline_keyboard, private) -> None:
    assert build_inline_keyboard(_template_message(private, private.other), "", "", None).inline_keyboard == ()
