from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from telegram import Update
from telegram.ext import ConversationHandler

from efb_telegram_master import utils
from efb_telegram_master.chat_binding import ChatBindingManager, ChatListStorage
from efb_telegram_master.constants import Flags
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID


def _callback_update(chat_id, message_id, data):
    return Update.de_json(
        {
            "update_id": 1,
            "callback_query": {
                "id": "callback-id",
                "from": {"id": 1, "is_bot": False, "first_name": "Tester"},
                "chat_instance": "instance",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": 1,
                    "chat": {"id": chat_id, "type": "private", "first_name": "Tester"},
                },
            },
        },
        None,
    )


@pytest.fixture
def callback_manager():
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.bot = Mock()
    manager.channel = SimpleNamespace(_=lambda text: text, flag=lambda _name: 10, master_message_inbound=Mock())
    manager.msg_storage = {}
    manager.link_handler = SimpleNamespace(_conversations={})
    manager.suggestion_handler = SimpleNamespace(_conversations={})
    manager.chat_head_handler = SimpleNamespace(_conversations={})
    return manager


@pytest.fixture
def callback_chat():
    return SimpleNamespace(module_id="tests.mocks.slave", full_name="Test chat", linked=False)


def _store_callback_session(manager, handler, state, storage_id, chats):
    manager.msg_storage[storage_id] = ChatListStorage(chats)
    manager._set_conversation_state(handler, storage_id, state)


@pytest.mark.parametrize(
    ("handler_name", "state", "callback"),
    [
        ("link_handler", Flags.LINK_EXEC, "manual_link 0"),
        ("suggestion_handler", Flags.SUGGEST_RECIPIENTS, "chat 0"),
        ("chat_head_handler", Flags.CHAT_HEAD_CONFIRM, "offset 0"),
    ],
)
def test_callback_handlers_expire_missing_sessions(callback_manager, handler_name, state, callback):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(201))
    handler = getattr(manager, handler_name)
    manager._set_conversation_state(handler, storage_id, state)
    update = _callback_update(*storage_id, callback)
    method = {
        "link_handler": manager.link_chat_exec,
        "suggestion_handler": manager.suggested_recipient,
        "chat_head_handler": manager.make_chat_head,
    }[handler_name]

    with patch.object(manager.bot, "edit_message_text"), patch.object(manager.bot, "answer_callback_query") as answer_callback_query:
        assert method(update, None) == ConversationHandler.END

    assert storage_id not in handler._conversations
    answer_callback_query.assert_called_once_with("callback-id")


@pytest.mark.parametrize("callback", ["manual_link", "manual_link 0 extra", "manual_link nope"])
def test_link_exec_rejects_malformed_callback_tokens(callback_manager, callback_chat, callback):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(202))
    _store_callback_session(manager, manager.link_handler, Flags.LINK_EXEC, storage_id, [callback_chat])

    with patch.object(manager.bot, "edit_message_text"), patch.object(manager.bot, "answer_callback_query"):
        assert manager.link_chat_exec(_callback_update(*storage_id, callback), None) == ConversationHandler.END

    assert storage_id not in manager.msg_storage
    assert storage_id not in manager.link_handler._conversations


@pytest.mark.parametrize("callback", ["offset", "offset nope", "offset 999", "chat nope", "chat 4"])
def test_link_confirmation_rejects_invalid_callback_indexes(callback_manager, callback_chat, callback):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(206))
    _store_callback_session(manager, manager.link_handler, Flags.LINK_CONFIRM, storage_id, [callback_chat])

    with patch.object(manager.bot, "edit_message_text"), patch.object(manager.bot, "answer_callback_query"), patch.object(manager, "link_chat_gen_list") as generate:
        assert manager.link_chat_confirm(_callback_update(*storage_id, callback), None) == ConversationHandler.END

    generate.assert_not_called()
    assert storage_id not in manager.msg_storage
    assert storage_id not in manager.link_handler._conversations


@pytest.mark.parametrize("callback", ["chat", "chat 0 extra", "chat nope", "chat 4"])
def test_suggested_recipient_rejects_invalid_or_stale_selection(callback_manager, callback_chat, callback):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(203))
    _store_callback_session(manager, manager.suggestion_handler, Flags.SUGGEST_RECIPIENTS, storage_id, [callback_chat])

    with patch.object(manager.bot, "edit_message_text"), patch.object(manager.bot, "answer_callback_query"), patch.object(manager.channel.master_message_inbound, "process_telegram_message") as process:
        assert manager.suggested_recipient(_callback_update(*storage_id, callback), None) == ConversationHandler.END

    process.assert_not_called()
    assert storage_id not in manager.msg_storage
    assert storage_id not in manager.suggestion_handler._conversations


@pytest.mark.parametrize("callback", ["offset", "offset 0 extra", "offset nope", "offset -1", "offset 999", "chat 4"])
def test_chat_head_rejects_invalid_or_out_of_range_callback_indexes(callback_manager, callback_chat, callback):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(204))
    _store_callback_session(manager, manager.chat_head_handler, Flags.CHAT_HEAD_CONFIRM, storage_id, [callback_chat])

    with patch.object(manager.bot, "edit_message_text"), patch.object(manager.bot, "answer_callback_query"), patch.object(manager, "chat_head_req_generate") as generate:
        assert manager.make_chat_head(_callback_update(*storage_id, callback), None) == ConversationHandler.END

    generate.assert_not_called()
    assert storage_id not in manager.msg_storage
    assert storage_id not in manager.chat_head_handler._conversations


def test_link_exec_keeps_valid_manual_link_callback_active(callback_manager, callback_chat):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(205))
    _store_callback_session(manager, manager.link_handler, Flags.LINK_EXEC, storage_id, [callback_chat])

    with patch.object(manager, "build_link_action_message") as build_link_action_message, patch.object(manager.bot, "answer_callback_query") as answer_callback_query:
        assert manager.link_chat_exec(_callback_update(*storage_id, "manual_link 0"), None) == Flags.LINK_EXEC

    build_link_action_message.assert_not_called()
    answer_callback_query.assert_not_called()
    assert storage_id in manager.msg_storage
    assert storage_id in manager.link_handler._conversations


@pytest.mark.parametrize(
    ("linked", "callback", "expected_state"),
    [
        (False, "manual_link 0", Flags.LINK_EXEC),
        (True, "unlink 0", ConversationHandler.END),
    ],
)
def test_link_actions_accept_zero_index_after_second_page_selection(callback_manager, linked, callback, expected_state):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(207))
    chats = [SimpleNamespace(module_id="tests.mocks.slave", full_name=f"Test chat {index}", linked=False) for index in range(10)]
    selected_chat = SimpleNamespace(module_id="tests.mocks.slave", full_name="Selected chat", linked=linked, unlink=Mock())
    chats.append(selected_chat)
    _store_callback_session(manager, manager.link_handler, Flags.LINK_CONFIRM, storage_id, chats)
    manager.msg_storage[storage_id].offset = manager.channel.flag("chats_per_page")
    manager.logger = Mock()

    with patch.object(manager, "_get_bot_user", return_value=SimpleNamespace(username="test_bot")):
        assert manager.link_chat_confirm(_callback_update(*storage_id, "chat 10"), None) == Flags.LINK_EXEC

    assert manager.msg_storage[storage_id].chats == [selected_chat]
    with patch.object(manager.bot, "answer_callback_query"):
        assert manager.link_chat_exec(_callback_update(*storage_id, callback), None) == expected_state

    if linked:
        selected_chat.unlink.assert_called_once_with()
    else:
        assert storage_id in manager.msg_storage


@pytest.mark.parametrize("callback", ["manual_link 10", "unlink 10"])
def test_link_actions_reject_stale_second_page_index_after_selection(callback_manager, callback):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(208))
    selected_chat = SimpleNamespace(module_id="tests.mocks.slave", full_name="Selected chat", linked=False, unlink=Mock())
    _store_callback_session(manager, manager.link_handler, Flags.LINK_EXEC, storage_id, [selected_chat])
    manager.msg_storage[storage_id].offset = manager.channel.flag("chats_per_page")

    with patch.object(manager.bot, "edit_message_text"), patch.object(manager.bot, "answer_callback_query"):
        assert manager.link_chat_exec(_callback_update(*storage_id, callback), None) == ConversationHandler.END

    selected_chat.unlink.assert_not_called()
    assert storage_id not in manager.msg_storage


def test_full_chat_pagination(channel, slave):
    storage_id = (TelegramChatID(0), TelegramMessageID(1))
    legends, buttons = channel.chat_binding.slave_chats_pagination(storage_id)
    legend = "\n".join(legends)
    assert slave.channel_emoji in legend
    assert slave.channel_name in legend
    assert min(channel.flag("chats_per_page"), len(slave.get_chats())) == len(buttons) - 1


def test_source_chat_pagination(channel, slave):
    storage_id = (TelegramChatID(0), TelegramMessageID(3))
    source_chats = [utils.chat_id_to_str(chat=slave.group)]
    legends, buttons = channel.chat_binding.slave_chats_pagination(storage_id, source_chats=source_chats)
    legend = "\n".join(legends)
    assert slave.channel_emoji in legend
    assert slave.channel_name in legend
    assert len(buttons) == 2


def test_chat_pagination_filters_groups_users_and_invalid_regex(channel, slave):
    _, buttons = channel.chat_binding.slave_chats_pagination((TelegramChatID(0), TelegramMessageID(4)), pattern="wonderland")
    names = [button.text for row in buttons[:-1] for button in row]
    assert names and all("Wonderland" in name for name in names)

    for message_id, (pattern, chat_type) in enumerate((("type: group", "GroupChat"), ("type: private", "PrivateChat")), start=5):
        _, buttons = channel.chat_binding.slave_chats_pagination((TelegramChatID(0), TelegramMessageID(message_id)), pattern=pattern)
        names = [button.text for row in buttons[:-1] for button in row]
        expected = slave.get_chats_by_criteria(chat_type=chat_type)
        assert names and all(any(chat.display_name in name for chat in expected) for name in names)

    _, buttons = channel.chat_binding.slave_chats_pagination((TelegramChatID(0), TelegramMessageID(7)), pattern="(")
    assert len(buttons) == 1


def test_truncate_ellipsis(channel):
    truncate_ellipsis = channel.chat_binding.truncate_ellipsis
    short_text = "short text"
    long_text = "This is a long text. Cursus pellentesque cras maecenas hac malesuada porttitor nullam, dignissim enim feugiat placerat eget quisque, dui sem dictum fames sapien mauris. Feugiat euismod nisi donec nunc cras aliquam diam, arcu fames pretium pellentesque faucibus phasellus, in montes felis elit lacinia auctor. Commodo curae nibh donec vel ipsum sociosqu maecenas pellentesque scelerisque suspendisse blandit himenaeos rutrum ad, nec dictum porttitor non luctus fringilla feugiat volutpat adipiscing cubilia vitae lacus. Tempor iaculis facilisis maecenas quam nisl pulvinar magnis lacus, sodales porta quisque rutrum habitasse metus purus ante libero, malesuada mollis est donec cubilia accumsan parturient. Parturient libero gravida imperdiet massa praesent habitant scelerisque pellentesque mollis elit, urna quisque tellus in nostra aliquet montes natoque fermentum, condimentum enim magna odio vestibulum mauris viverra sagittis iaculis."
    assert truncate_ellipsis(short_text, len(short_text) + 10) == short_text
    truncated = truncate_ellipsis(long_text, 256)
    assert len(truncated) <= 256
    assert truncated.endswith("…")


# All other methods are to be tested with integration testing.
