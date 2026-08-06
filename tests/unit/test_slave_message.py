import threading
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ehforwarderbot import Chat, Message
from ehforwarderbot.chat import ChatMember
from ehforwarderbot.constants import MsgType
from ehforwarderbot.types import MessageID
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from efb_telegram_master import TelegramChannel
from efb_telegram_master.constants import Emoji
from efb_telegram_master.slave_message import SlaveMessageProcessor
from tests.mocks.slave import MockSlaveChannel


def _processor() -> SlaveMessageProcessor:
    processor = object.__new__(SlaveMessageProcessor)
    processor.bot = Mock()
    processor.bot._cleanup_tls = SimpleNamespace(pending_cleanup=[])
    processor.flag = Mock(side_effect=lambda name: "emoji" if name == "default_media_prompt" else False)
    processor.logger = Mock()
    processor.channel = SimpleNamespace(config={"admins": [1]}, flag=lambda _name: False)
    processor._make_send_kwargs = Mock(return_value={"_slave_id": "tests.slave chat"})
    return processor


def _message(uid="message"):
    return SimpleNamespace(
        uid=uid,
        edit=False,
        type=MsgType.Text,
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
    )


def _dedupe_processor() -> SlaveMessageProcessor:
    processor = object.__new__(SlaveMessageProcessor)
    processor.db = Mock()
    processor.logger = Mock()
    processor.get_slave_msg_dest = Mock(return_value=("template", (123, None)))
    processor.is_silent = Mock(return_value=False)
    processor.dispatch_message = Mock()
    processor._pending_slave_messages = set()
    processor._pending_slave_messages_lock = threading.Lock()
    return processor


def test_reaction_footer_omits_empty_reactions() -> None:
    assert SlaveMessageProcessor.build_reactions_footer({}) == ""
    assert SlaveMessageProcessor.build_reactions_footer({"ok": [object(), object()], "empty": []}) == "[ok\u00d72]"


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
    return channel.slave_messages.generate_message_template


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
    return channel.slave_messages.build_chat_info_inline_keyboard


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


def test_channel_stopping_gate_drops_messages_and_statuses() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.slave_messages = Mock()
    channel.bot_manager = SimpleNamespace(_stopping=False)
    channel._stop_polling_called = True

    message = SimpleNamespace(uid="late")
    assert TelegramChannel.send_message(channel, message) is message
    assert TelegramChannel.send_status(channel, SimpleNamespace()) is None
    channel.slave_messages.send_message.assert_not_called()
    channel.slave_messages.send_status.assert_not_called()


def test_channel_manager_stopping_gate_drops_message() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.slave_messages = Mock()
    channel.bot_manager = SimpleNamespace(_stopping=True)
    channel._stop_polling_called = False

    message = SimpleNamespace(uid="late")
    assert TelegramChannel.send_message(channel, message) is message
    channel.slave_messages.send_message.assert_not_called()


def test_forum_destination_uses_cached_chat_info_until_ttl() -> None:
    processor = object.__new__(SlaveMessageProcessor)
    chat_uid = "tests.slave chat"
    tg_chat = "telegram -100123"

    def get_chat_assoc(*, slave_uid=None, master_uid=None):
        return [tg_chat] if slave_uid == chat_uid else [chat_uid] if master_uid == tg_chat else []

    processor.channel = SimpleNamespace(config={"admins": [1]}, topic_group=-100999, chat_binding=SimpleNamespace(create_topic=Mock(return_value=55)))
    processor.bot = SimpleNamespace(get_chat_info=Mock(return_value=SimpleNamespace(is_forum=True)))
    processor.db = SimpleNamespace(get_chat_assoc=Mock(side_effect=get_chat_assoc), get_topic_thread_id=Mock(return_value=55))
    processor.chat_manager = SimpleNamespace(update_chat_obj=lambda chat: chat, get_or_enrol_member=lambda chat, author: author)
    processor.chat_dest_cache = SimpleNamespace(get=Mock(return_value=chat_uid), remove=Mock())
    processor.generate_message_template = Mock(return_value="template")
    processor.logger = Mock()
    processor._known_forum_chat_ids = {}
    processor._known_forum_chat_ids_lock = threading.Lock()

    first = SimpleNamespace(uid="one", chat=SimpleNamespace(module_id="tests.slave", uid="chat"), author=SimpleNamespace())
    second = SimpleNamespace(uid="two", chat=SimpleNamespace(module_id="tests.slave", uid="chat"), author=SimpleNamespace())
    assert processor.get_slave_msg_dest(first) == ("template", (-100123, 55))
    assert processor.get_slave_msg_dest(second) == ("template", (-100123, 55))
    processor.bot.get_chat_info.assert_called_once_with(-100123)

    processor._known_forum_chat_ids[-100123] = time.monotonic() - processor.FORUM_CHAT_CACHE_TTL - 1
    assert processor.get_slave_msg_dest(second) == ("template", (-100123, 55))
    assert processor.bot.get_chat_info.call_count == 2


def test_new_slave_message_claims_memory_dedupe_without_db_lookup() -> None:
    processor = _dedupe_processor()
    message = _message()

    assert processor.send_message(message) is message
    assert ("tests.slave chat", "message") in processor._pending_slave_messages
    processor.db.get_msg_log.assert_not_called()
    processor.dispatch_message.assert_called_once_with(message, "template", None, 123, None, False, dedupe_key=("tests.slave chat", "message"))


def test_pending_duplicate_and_muted_message_do_not_dispatch() -> None:
    processor = _dedupe_processor()
    processor._pending_slave_messages.add(("tests.slave chat", "message"))
    assert processor.send_message(_message()) is not None
    processor.dispatch_message.assert_not_called()

    processor = _dedupe_processor()
    processor.is_silent.return_value = None
    assert processor.send_message(_message()) is not None
    assert not processor._pending_slave_messages
    processor.dispatch_message.assert_not_called()


def test_ingested_message_edit_has_no_telegram_side_effect() -> None:
    processor = _dedupe_processor()
    processor.db.get_msg_log.return_value = SimpleNamespace(provenance="mtproto_ingested")
    message = _message("mtproto-ingested:100.1")
    message.edit = True

    assert processor.send_message(message) is message
    processor.get_slave_msg_dest.assert_not_called()
    processor.dispatch_message.assert_not_called()


def test_send_kwargs_preserve_slave_routing_identity() -> None:
    processor = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    message = SimpleNamespace(chat=SimpleNamespace(module_id="tests.slave", uid="chat"))

    assert processor._make_send_kwargs(message) == {"_slave_id": "tests.slave chat"}


def _remote_image(url="https://example.com/images/photo.jpg"):
    return SimpleNamespace(
        uid=MessageID("remote"),
        type=MsgType.Image,
        text="remote <image>",
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
        file=None,
        path=None,
        mime=None,
        filename=None,
        edit_media=False,
        commands=None,
        substitutions=None,
        vendor_specific={SlaveMessageProcessor.REMOTE_IMAGE_URL_VENDOR_KEY: url},
    )


def test_remote_image_send_and_edit_use_url() -> None:
    processor = _processor()
    processor.bot.send_photo.return_value = "sent"
    assert processor.slave_message_image(_remote_image(), 100, 7, "header", "footer", target_msg_id=9, silent=True) == "sent"
    assert processor.bot.send_photo.call_args.args == (100, "https://example.com/images/photo.jpg")
    assert processor.bot.send_photo.call_args.kwargs["reply_to_message_id"] == 9

    processor = _processor()
    processor.bot.edit_message_media.return_value = "edited"
    message = _remote_image()
    message.text = ""
    message.edit_media = True
    assert processor.slave_message_image(message, 100, None, "", "", old_msg_id=(100, 10)) == "edited"
    assert isinstance(processor.bot.edit_message_media.call_args.kwargs["media"], InputMediaPhoto)


def test_remote_image_and_document_url_failures_send_placeholder() -> None:
    processor = _processor()
    processor.bot.send_photo.side_effect = [BadRequest("failed to get HTTP URL content"), "placeholder"]
    assert processor.slave_message_image(_remote_image(), 100, None, "header", "footer") == "placeholder"
    assert processor.bot.send_photo.call_count == 2
    assert hasattr(processor.bot.send_photo.call_args_list[1].args[1], "read")

    processor = _processor()
    processor.bot.send_document.side_effect = [BadRequest("failed to get HTTP URL content"), "placeholder"]
    assert processor.slave_message_file(_remote_image(), 100, None, "header", "footer") == "placeholder"
    assert hasattr(processor.bot.send_document.call_args_list[1].args[1], "read")


def test_remote_image_as_document_uses_url_filename() -> None:
    processor = _processor()
    processor.bot.send_document.return_value = "sent"
    assert processor.slave_message_file(_remote_image("https://example.com/images/photo;1.jpg"), 100, None, "header", "footer") == "sent"
    assert processor.bot.send_document.call_args.args == (100, "https://example.com/images/photo;1.jpg")
    assert processor.bot.send_document.call_args.kwargs["filename"] == "photo 1.jpg"


def test_oversized_file_sends_current_bot_api_notice_for_new_message() -> None:
    processor = _processor()
    processor.bot.send_message.return_value = "sent"
    message = SimpleNamespace(edit_media=True)
    with patch("efb_telegram_master.slave_message.sync_reply_text") as reply:
        sent, edit_media = processor._send_oversized_file_notice(message, "Attachment exceeds the limit.", 100, 7, "header", "footer", "caption", None, 9, None, True, None)
    assert (sent, edit_media) == ("sent", True)
    assert processor.bot.send_message.call_args.kwargs["text"] == "caption"
    reply.assert_called_once_with(processor.bot, "sent", "Attachment exceeds the limit.", quote=True)


def test_oversized_file_edit_replies_with_notice_only() -> None:
    processor = _processor()
    message = SimpleNamespace(edit_media=True)
    assert processor._send_oversized_file_notice(message, "Attachment exceeds the limit.", 100, None, "header", "footer", "caption", (200, 10), None, None, False, None) == (None, False)
    processor.bot.send_message.assert_called_once_with(chat_id=200, reply_to_message_id=10, text="Attachment exceeds the limit.")


@pytest.mark.parametrize(
    ("delivered_to", "expected"),
    [("blueset.telegram", "100.10"), ("tests.slave", "100.11")],
)
def test_reaction_target_selects_primary_or_bot_reply(delivered_to, expected) -> None:
    message = SimpleNamespace(type=MsgType.Text, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id=delivered_to))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11")
    assert SlaveMessageProcessor._reaction_target_message_id(message, row) == expected


def _reaction_processor(row, message, *, side_effect=None):
    processor = object.__new__(SlaveMessageProcessor)
    processor.REACTION_DB_WAIT_TIMEOUT = 0
    processor.chat_manager = Mock()
    processor.get_slave_msg_dest = Mock(return_value=("template", (100, None)))
    processor.logger = Mock()
    processor.db = SimpleNamespace(get_msg_log=Mock(return_value=row))
    processor.dispatch_message = Mock(side_effect=side_effect)
    return processor, SimpleNamespace(chat=SimpleNamespace(module_id="tests.slave", uid="chat"), msg_id="message", reactions={"R": [object()]})


def test_reaction_from_telegram_origin_creates_bot_reply_to_user_message() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt=None, sender_bot_id=None, build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)
    processor.update_reactions(status)
    processor.dispatch_message.assert_called_once_with(message, "template", None, 100, None, database_old_msg_id=(100, 10), target_msg_id_override=10)


def test_reaction_update_waits_for_message_log_write() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="blueset.telegram"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt=None, sender_bot_id=None, build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)
    processor.REACTION_DB_WAIT_TIMEOUT = 0.1
    processor.REACTION_DB_WAIT_INTERVAL = 0
    processor.db.get_msg_log.side_effect = [None, row]

    processor.update_reactions(status)

    assert processor.db.get_msg_log.call_count == 2
    processor.dispatch_message.assert_called_once_with(message, "template", (100, 10), 100, None)


def test_reaction_update_edits_existing_bot_reply_and_persists_sender() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11", sender_bot_id="777", build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)
    processor.update_reactions(status)
    processor.dispatch_message.assert_called_once_with(message, "template", (100, 11), 100, None)
    assert message.vendor_specific == {"_sender_bot_id": "777"}


def test_missing_reaction_edit_target_creates_replacement_reply() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11", sender_bot_id="777", build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message, side_effect=[BadRequest("Message to edit not found"), None])
    processor.update_reactions(status)
    assert processor.dispatch_message.call_args_list[1].args == (message, "template", None, 100, None)
    assert processor.dispatch_message.call_args_list[1].kwargs == {"database_old_msg_id": (100, 11), "target_msg_id_override": 10}


@pytest.mark.parametrize("error", [BadRequest("Not enough rights"), RetryAfter(1), NetworkError("transport"), TelegramError("other")])
def test_nonmissing_reaction_edit_errors_are_propagated(error) -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11", sender_bot_id=None, build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message, side_effect=error)
    with pytest.raises(type(error)):
        processor.update_reactions(status)


def test_reaction_retries_bot_reply_until_database_records_alternate() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt=None, sender_bot_id=None, build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)

    def record_alternate(*_args, **_kwargs):
        if processor.dispatch_message.call_count == 2:
            row.master_msg_id_alt = "100.12"
            row.sender_bot_id = "888"

    processor.dispatch_message.side_effect = record_alternate
    for reaction in ("R0", "R1", "R2"):
        status.reactions = {reaction: [object()]}
        processor.update_reactions(status)

    assert [call.args[2] for call in processor.dispatch_message.call_args_list] == [None, None, (100, 12)]


def test_reaction_retries_missing_alternate_until_replacement_is_recorded() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11", sender_bot_id="777", build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)
    replies = 0

    def replace_after_second_reply(_message, _template, old_msg_id, *_args, **_kwargs):
        nonlocal replies
        if old_msg_id == (100, 11):
            raise BadRequest("Message to edit not found")
        replies += 1
        if replies == 2:
            row.master_msg_id_alt = "100.13"
            row.sender_bot_id = "999"

    processor.dispatch_message.side_effect = replace_after_second_reply
    for reaction in ("R0", "R1", "R2"):
        status.reactions = {reaction: [object()]}
        processor.update_reactions(status)

    assert [call.args[2] for call in processor.dispatch_message.call_args_list] == [(100, 11), None, (100, 11), None, (100, 13)]
