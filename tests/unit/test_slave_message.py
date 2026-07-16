import threading
import time
import pytest
from pytest import fixture
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from typing import cast
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot import Message, Chat
from ehforwarderbot.constants import MsgType
from ehforwarderbot.chat import ChatMember
from ehforwarderbot.types import MessageID, ReactionName
from efb_telegram_master import TelegramChannel
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


REMOTE_IMAGE_URL = "https://example.com/images/photo.jpg"


def build_remote_image_message(remote_image_url: str = REMOTE_IMAGE_URL) -> Message:
    message = Message()
    message.uid = MessageID("__remote_image_msg__")
    message.type = MsgType.Image
    message.text = "remote <image>"
    message.chat = SimpleNamespace(module_id="tests.mocks.slave", uid="__chat_id__")
    message.file = None
    message.path = None
    message.mime = None
    message.filename = None
    message.edit_media = False
    message.commands = None
    message.substitutions = None
    message.vendor_specific = {
        SlaveMessageProcessor.REMOTE_IMAGE_URL_VENDOR_KEY: remote_image_url,
    }
    return message


def build_slave_message_processor() -> SlaveMessageProcessor:
    processor = object.__new__(SlaveMessageProcessor)
    processor.bot = Mock()
    processor.bot._cleanup_tls = SimpleNamespace(pending_cleanup=[])
    processor.flag = Mock(side_effect=lambda flag_name: "emoji" if flag_name == "default_media_prompt" else False)
    processor.logger = Mock()
    processor.channel = cast(TelegramChannel, SimpleNamespace(config={"admins": [1]}, flag=processor.flag))
    processor._make_send_kwargs = Mock(return_value={
        "_send_mode": "blocking",
        "_slave_id": "tests.mocks.slave __chat_id__",
    })
    return processor


def build_duplicate_test_processor() -> SlaveMessageProcessor:
    processor = object.__new__(SlaveMessageProcessor)
    processor.db = Mock()
    processor.logger = Mock()
    setattr(processor, "get_slave_msg_dest", Mock(return_value=("__template__", (123, None))))
    setattr(processor, "is_silent", Mock(return_value=False))
    setattr(processor, "dispatch_message", Mock())
    processor._pending_slave_messages = set()
    processor._pending_slave_messages_lock = threading.Lock()
    return processor


def build_duplicate_test_message(uid: str = "__msg_id__") -> SimpleNamespace:
    return SimpleNamespace(
        uid=uid,
        edit=False,
        type=MsgType.Text,
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat_id__"),
    )


def build_stopping_channel() -> TelegramChannel:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.logger = Mock()
    channel.slave_messages = Mock()
    channel.bot_manager = SimpleNamespace(_stopping=False)
    channel._stop_polling_called = True
    return channel


def test_channel_drops_slave_message_while_stopping():
    channel = build_stopping_channel()
    message = SimpleNamespace(uid="__late_msg__")

    result = TelegramChannel.send_message(channel, message)

    assert result is message
    channel.slave_messages.send_message.assert_not_called()


def test_channel_drops_slave_status_while_stopping():
    channel = build_stopping_channel()
    status = SimpleNamespace()

    result = TelegramChannel.send_status(channel, status)

    assert result is None
    channel.slave_messages.send_status.assert_not_called()


def test_channel_drops_slave_message_after_bot_manager_shutdown_starts():
    channel = build_stopping_channel()
    channel._stop_polling_called = False
    channel.bot_manager._stopping = True
    message = SimpleNamespace(uid="__late_msg__")

    result = TelegramChannel.send_message(channel, message)

    assert result is message
    channel.slave_messages.send_message.assert_not_called()


def test_make_send_kwargs_use_slave_target_for_new_message():
    processor = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    message = SimpleNamespace(
        vendor_specific={},
        commands=[],
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat_id__"),
    )

    kwargs = SlaveMessageProcessor._make_send_kwargs(processor, message, None)

    assert kwargs == {
        "_send_mode": "eventual",
        "_slave_id": "tests.mocks.slave __chat_id__",
    }


def test_make_send_kwargs_preserve_forced_send_mode():
    processor = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    message = SimpleNamespace(
        vendor_specific={"_force_send_mode": "blocking"},
        commands=[],
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat_id__"),
    )

    kwargs = SlaveMessageProcessor._make_send_kwargs(processor, message, None)

    assert kwargs == {
        "_send_mode": "blocking",
        "_slave_id": "tests.mocks.slave __chat_id__",
    }


def test_make_send_kwargs_attach_db_context_when_dispatch_sets_callback():
    processor = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    processor.chat_manager = Mock()
    on_complete = Mock()
    message = SimpleNamespace(
        vendor_specific={},
        commands=[],
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat_id__"),
    )
    etm_msg = Mock()

    with patch("efb_telegram_master.slave_message.ETMMsg.from_efbmsg", return_value=etm_msg):
        kwargs = SlaveMessageProcessor._make_send_kwargs(processor, message, None, on_complete=on_complete)

    db_context = kwargs["_queued_db_log_context"]
    assert kwargs["_send_mode"] == "eventual"
    assert db_context.etm_msg is etm_msg
    assert db_context.old_msg_id is None
    assert db_context.on_complete is on_complete


def test_make_send_kwargs_keep_slave_target_for_blocking_mode():
    processor = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    message = SimpleNamespace(
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat_id__"),
    )

    kwargs = SlaveMessageProcessor._make_send_kwargs(processor, message, mode='blocking')

    assert kwargs == {
        "_send_mode": "blocking",
        "_slave_id": "tests.mocks.slave __chat_id__",
    }


def test_get_slave_msg_dest_caches_known_forum_chat_info():
    processor = SlaveMessageProcessor.__new__(SlaveMessageProcessor)
    chat_uid = "tests.mocks.slave __chat_id__"
    tg_chat = "telegram -100123"

    def get_chat_assoc(*, slave_uid=None, master_uid=None):
        if slave_uid == chat_uid:
            return [tg_chat]
        if master_uid == tg_chat:
            return [chat_uid]
        return []

    processor.channel = SimpleNamespace(
        config={"admins": [1]},
        topic_group=-100999,
        chat_binding=SimpleNamespace(create_topic=Mock(return_value=55)),
    )
    processor.bot = SimpleNamespace(get_chat_info=Mock(return_value=SimpleNamespace(is_forum=True)))
    processor.db = SimpleNamespace(
        get_chat_assoc=Mock(side_effect=get_chat_assoc),
        get_topic_thread_id=Mock(return_value=55),
    )
    processor.chat_manager = SimpleNamespace(
        update_chat_obj=lambda chat: chat,
        get_or_enrol_member=lambda chat, author: author,
    )
    processor.chat_dest_cache = SimpleNamespace(get=Mock(return_value=chat_uid), remove=Mock())
    processor.generate_message_template = Mock(return_value="template")
    processor.logger = Mock()
    processor._known_forum_chat_ids = {}
    processor._known_forum_chat_ids_lock = threading.Lock()

    message_1 = SimpleNamespace(
        uid="message-1",
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat_id__"),
        author=SimpleNamespace(),
    )
    message_2 = SimpleNamespace(
        uid="message-2",
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat_id__"),
        author=SimpleNamespace(),
    )

    assert processor.get_slave_msg_dest(message_1) == ("template", (-100123, 55))
    assert processor.get_slave_msg_dest(message_2) == ("template", (-100123, 55))

    processor.bot.get_chat_info.assert_called_once_with(-100123)
    assert processor.channel.chat_binding.create_topic.call_count == 2
    assert -100123 in processor._known_forum_chat_ids

    processor._known_forum_chat_ids[-100123] = time.monotonic() - processor.FORUM_CHAT_CACHE_TTL - 1
    message_3 = SimpleNamespace(
        uid="message-3",
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat_id__"),
        author=SimpleNamespace(),
    )

    assert processor.get_slave_msg_dest(message_3) == ("template", (-100123, 55))
    assert processor.bot.get_chat_info.call_count == 2
    assert processor.channel.chat_binding.create_topic.call_count == 3


def test_new_slave_message_does_not_query_sent_message_log_for_dedupe():
    processor = build_duplicate_test_processor()
    processor.db.get_msg_log.return_value = SimpleNamespace(master_msg_id="100.10")
    message = build_duplicate_test_message()
    key = ("tests.mocks.slave __chat_id__", "__msg_id__")

    result = SlaveMessageProcessor.send_message(processor, message)

    assert result is message
    assert key in processor._pending_slave_messages
    processor.db.get_msg_log.assert_not_called()
    processor.get_slave_msg_dest.assert_called_once_with(message)
    processor.dispatch_message.assert_called_once_with(
        message, "__template__", None, 123, None, False, dedupe_key=key,
    )


def test_duplicate_slave_message_pending_is_skipped():
    processor = build_duplicate_test_processor()
    message = build_duplicate_test_message()
    key = ("tests.mocks.slave __chat_id__", "__msg_id__")
    processor._pending_slave_messages.add(key)

    result = SlaveMessageProcessor.send_message(processor, message)

    assert result is message
    processor.db.get_msg_log.assert_not_called()
    processor.get_slave_msg_dest.assert_not_called()
    processor.dispatch_message.assert_not_called()


def test_new_slave_message_claims_pending_before_dispatch():
    processor = build_duplicate_test_processor()
    message = build_duplicate_test_message()
    key = ("tests.mocks.slave __chat_id__", "__msg_id__")

    result = SlaveMessageProcessor.send_message(processor, message)

    assert result is message
    assert key in processor._pending_slave_messages
    processor.db.get_msg_log.assert_not_called()
    processor.dispatch_message.assert_called_once_with(
        message, "__template__", None, 123, None, False, dedupe_key=key,
    )


def test_undelivered_slave_message_releases_pending_claim():
    processor = build_duplicate_test_processor()
    processor.is_silent.return_value = None
    message = build_duplicate_test_message()
    key = ("tests.mocks.slave __chat_id__", "__msg_id__")

    result = SlaveMessageProcessor.send_message(processor, message)

    assert result is message
    assert key not in processor._pending_slave_messages
    processor.db.get_msg_log.assert_not_called()
    processor.dispatch_message.assert_not_called()


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


def test_slave_message_image_sends_remote_image_url():
    processor = build_slave_message_processor()
    processor.bot.send_photo.return_value = "__telegram_message__"
    msg = build_remote_image_message()

    result = processor.slave_message_image(
        msg, 100, 200, "__template__", "__reactions__",
        target_msg_id=300, silent=True
    )

    assert result == "__telegram_message__"
    processor.bot.send_photo.assert_called_once_with(
        100,
        REMOTE_IMAGE_URL,
        prefix="__template__",
        suffix="__reactions__",
        caption="remote &lt;image&gt;",
        parse_mode="HTML",
        reply_to_message_id=300,
        message_thread_id=200,
        reply_markup=None,
        disable_notification=True,
        _fallback_to_document=False,
        _send_mode="blocking",
        _slave_id="tests.mocks.slave __chat_id__",
    )
    processor.bot.send_document.assert_not_called()


def test_slave_message_image_sends_placeholder_when_remote_image_url_fails():
    processor = build_slave_message_processor()
    processor.bot.send_photo.side_effect = [
        BadRequest("failed to get HTTP URL content"),
        "__placeholder_message__",
    ]
    msg = build_remote_image_message()

    result = processor.slave_message_image(msg, 100, None, "__template__", "__reactions__")

    assert result == "__placeholder_message__"
    assert processor.bot.send_photo.call_count == 2
    first_call, second_call = processor.bot.send_photo.call_args_list
    assert first_call.args[1] == REMOTE_IMAGE_URL
    assert first_call.kwargs["_fallback_to_document"] is False
    assert first_call.kwargs["_slave_id"] == "tests.mocks.slave __chat_id__"
    assert hasattr(second_call.args[1], "read")
    assert second_call.kwargs["caption"] == "remote &lt;image&gt;"
    assert second_call.kwargs["_send_mode"] == "blocking"
    processor.bot.send_document.assert_not_called()


def test_slave_message_image_edits_remote_image_url_media():
    processor = build_slave_message_processor()
    processor.bot.edit_message_media.return_value = "__edited_message__"
    msg = build_remote_image_message()
    msg.text = ""
    msg.edit_media = True

    result = processor.slave_message_image(msg, 100, None, "", "", old_msg_id=(100, 10))

    assert result == "__edited_message__"
    media = processor.bot.edit_message_media.call_args.kwargs["media"]
    assert isinstance(media, InputMediaPhoto)
    assert media.media == REMOTE_IMAGE_URL
    processor.bot.edit_message_caption.assert_not_called()


def test_slave_message_file_sends_remote_image_url_as_document():
    remote_image_url = "https://example.com/images/photo;1.jpg"
    processor = build_slave_message_processor()
    processor.bot.send_document.return_value = "__telegram_message__"
    msg = build_remote_image_message(remote_image_url)

    result = processor.slave_message_file(
        msg, 100, None, "__template__", "__reactions__",
        target_msg_id=300, silent=True
    )

    assert result == "__telegram_message__"
    processor.bot.send_document.assert_called_once_with(
        100,
        remote_image_url,
        prefix="__template__",
        suffix="__reactions__",
        caption="remote &lt;image&gt;",
        parse_mode="HTML",
        filename="photo 1.jpg",
        reply_to_message_id=300,
        message_thread_id=None,
        reply_markup=None,
        disable_notification=True,
        _send_mode="blocking",
        _slave_id="tests.mocks.slave __chat_id__",
    )


def test_slave_message_file_sends_placeholder_when_remote_document_url_fails():
    processor = build_slave_message_processor()
    processor.bot.send_document.side_effect = [
        BadRequest("failed to get HTTP URL content"),
        "__placeholder_message__",
    ]
    msg = build_remote_image_message()

    result = processor.slave_message_file(msg, 100, None, "__template__", "__reactions__")

    assert result == "__placeholder_message__"
    assert processor.bot.send_document.call_count == 2
    first_call, second_call = processor.bot.send_document.call_args_list
    assert first_call.args[1] == REMOTE_IMAGE_URL
    assert hasattr(second_call.args[1], "read")
    assert second_call.kwargs["filename"] == "remote-image-placeholder.png"
    assert second_call.kwargs["_send_mode"] == "blocking"


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


def test_update_reactions_waits_for_queued_database_log():
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


def test_first_telegram_origin_reaction_replies_to_user_message():
    processor = Mock(spec=SlaveMessageProcessor)
    processor.REACTION_DB_WAIT_TIMEOUT = 0
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
        deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
    )
    old_msg_db = SimpleNamespace(
        master_msg_id="100.10",
        master_msg_id_alt=None,
        sender_bot_id=None,
        build_etm_msg=Mock(return_value=old_msg),
    )
    processor.db = Mock()
    processor.db.get_msg_log.return_value = old_msg_db
    status = SimpleNamespace(
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat__"),
        msg_id="__msg_id_reaction__",
        reactions={"R0": [object()]},
    )

    SlaveMessageProcessor.update_reactions(processor, status)

    processor.dispatch_message.assert_called_once_with(
        old_msg,
        "__template__",
        None,
        100,
        None,
        database_old_msg_id=(100, 10),
        target_msg_id_override=10,
    )
    assert old_msg.vendor_specific["_force_send_mode"] == "blocking"


def test_telegram_origin_reaction_updates_and_retraction_edit_bot_alternate():
    processor = Mock(spec=SlaveMessageProcessor)
    processor.REACTION_DB_WAIT_TIMEOUT = 0
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
        deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
    )
    old_msg_db = SimpleNamespace(
        master_msg_id="100.10",
        master_msg_id_alt="100.11",
        sender_bot_id=None,
        build_etm_msg=Mock(return_value=old_msg),
    )
    processor.db = Mock()
    processor.db.get_msg_log.return_value = old_msg_db

    for reactions in ({"R0": [object()]}, {}):
        status = SimpleNamespace(
            chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat__"),
            msg_id="__msg_id_reaction__",
            reactions=reactions,
        )
        SlaveMessageProcessor.update_reactions(processor, status)

    expected = [
        ((old_msg, "__template__", (100, 11), 100, None), {}),
        ((old_msg, "__template__", (100, 11), 100, None), {}),
    ]
    assert [(call.args, call.kwargs) for call in processor.dispatch_message.call_args_list] == expected
    assert old_msg.reactions == {}


def test_restarted_reactions_route_edits_to_persisted_auxiliary_sender():
    cases = (
        ("tests.mocks.slave", "100.11", 11),
        ("blueset.telegram", "100.11", 10),
    )

    for delivered_to, alternate_id, expected_message_id in cases:
        processor = build_slave_message_processor()
        processor.REACTION_DB_WAIT_TIMEOUT = 0
        processor.chat_manager = Mock()
        processor.get_slave_msg_dest = Mock(return_value=("__template__", (100, None)))
        processor.html_substitutions = Mock(return_value="message")
        old_msg = SimpleNamespace(
            uid=MessageID("__msg_id_reaction__"),
            type=MsgType.Text,
            reactions={},
            vendor_specific=None,
            chat=SimpleNamespace(module_id="tests.mocks.slave"),
            deliver_to=SimpleNamespace(channel_id=delivered_to),
        )
        old_msg_db = SimpleNamespace(
            master_msg_id="100.10",
            master_msg_id_alt=alternate_id,
            sender_bot_id="777",
            build_etm_msg=Mock(return_value=old_msg),
        )
        processor.db = Mock()
        processor.db.get_msg_log.return_value = old_msg_db

        def edit_reaction(msg, template, old_msg_id, destination, thread_id):
            return processor.slave_message_text(
                msg, destination, thread_id, template, "__reactions__", old_msg_id=old_msg_id,
            )

        processor.dispatch_message = Mock(side_effect=edit_reaction)
        status = SimpleNamespace(
            chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat__"),
            msg_id="__msg_id_reaction__",
            reactions={"R0": [object()]},
        )

        SlaveMessageProcessor.update_reactions(processor, status)

        old_msg_db.build_etm_msg.assert_called_once_with(chat_manager=processor.chat_manager)
        processor.bot.edit_message_text.assert_called_once_with(
            chat_id=100,
            message_id=expected_message_id,
            text="message",
            prefix="__template__",
            suffix="__reactions__",
            parse_mode="HTML",
            reply_markup=None,
            _sender_bot_id="777",
        )


def build_reaction_failure_processor(error):
    processor = Mock(spec=SlaveMessageProcessor)
    processor.REACTION_DB_WAIT_TIMEOUT = 0
    processor.chat_manager = Mock()
    processor.get_slave_msg_dest = Mock(return_value=("__template__", (100, None)))
    processor.logger = Mock()
    processor._reaction_target_message_id = SlaveMessageProcessor._reaction_target_message_id
    processor._reaction_edit_target_missing = SlaveMessageProcessor._reaction_edit_target_missing
    old_msg = SimpleNamespace(
        type=MsgType.Text,
        reactions={},
        vendor_specific=None,
        chat=SimpleNamespace(module_id="tests.mocks.slave"),
        deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
    )
    old_msg_db = SimpleNamespace(
        master_msg_id="100.10",
        master_msg_id_alt="100.11",
        sender_bot_id="777",
        build_etm_msg=Mock(return_value=old_msg),
    )
    processor.db = Mock()
    processor.db.get_msg_log.return_value = old_msg_db
    processor.dispatch_message = Mock(side_effect=error)
    status = SimpleNamespace(
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="__chat__"),
        msg_id="__msg_id_reaction__",
        reactions={"R0": [object()]},
    )
    return processor, old_msg, old_msg_db, status


def test_missing_reaction_alternate_creates_one_replacement_reply():
    processor, old_msg, old_msg_db, status = build_reaction_failure_processor(
        BadRequest("Message to edit not found")
    )
    processor.dispatch_message.side_effect = [BadRequest("Message to edit not found"), None]

    SlaveMessageProcessor.update_reactions(processor, status)

    assert processor.dispatch_message.call_args_list[0].args[2] == (100, 11)
    assert processor.dispatch_message.call_args_list[1].args == (
        old_msg, "__template__", None, 100, None,
    )
    assert processor.dispatch_message.call_args_list[1].kwargs == {
        "database_old_msg_id": (100, 11),
        "target_msg_id_override": 10,
    }
    assert old_msg.vendor_specific == {"_sender_bot_id": "777", "_force_send_mode": "blocking"}
    assert (old_msg_db.master_msg_id, old_msg_db.master_msg_id_alt) == ("100.10", "100.11")


def test_nonrecoverable_reaction_edit_failures_do_not_create_reply():
    errors = (
        BadRequest("Not enough rights to edit this message"),
        BadRequest("Can't parse entities"),
        BadRequest("Bot is not a member of the supergroup chat"),
        RetryAfter(1),
        NetworkError("transport failed"),
        TelegramError("other edit failure"),
    )

    for error in errors:
        processor, _old_msg, old_msg_db, status = build_reaction_failure_processor(error)
        with pytest.raises(type(error)):
            SlaveMessageProcessor.update_reactions(processor, status)

        assert processor.dispatch_message.call_count == 1
        assert (old_msg_db.master_msg_id, old_msg_db.master_msg_id_alt) == ("100.10", "100.11")
