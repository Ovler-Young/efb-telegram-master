from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from telegram.ext import ConversationHandler

from efb_telegram_master import utils
from efb_telegram_master.constants import Flags
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID
from tests.unit.chat_binding_support import callback_update, create_link_manager, store_callback_session


@pytest.mark.parametrize(
    ("linked", "callback", "expected_state"),
    [
        (False, "manual_link 0", Flags.LINK_EXEC),
        (True, "unlink 0", ConversationHandler.END),
    ],
)
def test_link_actions_accept_zero_index_after_second_page_selection(linked, callback, expected_state):
    manager = create_link_manager()
    storage_id = (TelegramChatID(1), TelegramMessageID(207))
    chats = [SimpleNamespace(module_id="tests.mocks.slave", full_name=f"Test chat {index}", linked=False) for index in range(10)]
    selected_chat = SimpleNamespace(module_id="tests.mocks.slave", full_name="Selected chat", linked=linked, unlink=Mock())
    chats.append(selected_chat)
    store_callback_session(manager, manager._conversation_handler, Flags.LINK_CONFIRM, storage_id, chats)
    manager.callback_sessions.lookup(storage_id).offset = manager.channel.flag("chats_per_page")
    manager.logger = Mock()

    with patch("efb_telegram_master.link_actions.get_bot_user", return_value=SimpleNamespace(username="test_bot")):
        assert manager.confirm(callback_update(*storage_id, "chat 10"), None) == Flags.LINK_EXEC

    assert manager.callback_sessions.lookup(storage_id).chats == [selected_chat]
    with patch.object(manager.bot, "answer_callback_query"):
        assert manager.execute(callback_update(*storage_id, callback), None) == expected_state

    if linked:
        selected_chat.unlink.assert_called_once_with()
    else:
        assert manager.callback_sessions.lookup(storage_id) is not None


@pytest.mark.parametrize("callback", ["manual_link 10", "unlink 10"])
def test_link_actions_reject_stale_second_page_index_after_selection(callback):
    manager = create_link_manager()
    storage_id = (TelegramChatID(1), TelegramMessageID(208))
    selected_chat = SimpleNamespace(module_id="tests.mocks.slave", full_name="Selected chat", linked=False, unlink=Mock())
    store_callback_session(manager, manager._conversation_handler, Flags.LINK_EXEC, storage_id, [selected_chat])
    manager.callback_sessions.lookup(storage_id).offset = manager.channel.flag("chats_per_page")

    with patch.object(manager.bot, "edit_message_text"), patch.object(manager.bot, "answer_callback_query"):
        assert manager.execute(callback_update(*storage_id, callback), None) == ConversationHandler.END

    selected_chat.unlink.assert_not_called()
    assert manager.callback_sessions.lookup(storage_id) is None


def test_full_chat_pagination(channel, slave):
    storage_id = (TelegramChatID(0), TelegramMessageID(1))
    legends, buttons = channel.recipient_suggestions.render_chat_list(storage_id, 1)
    legend = "\n".join(legends)
    assert slave.channel_emoji in legend
    assert slave.channel_name in legend
    assert min(channel.flag("chats_per_page"), len(slave.get_chats())) == len(buttons) - 1


def test_source_chat_pagination(channel, slave):
    storage_id = (TelegramChatID(0), TelegramMessageID(3))
    source_chats = [utils.chat_id_to_str(chat=slave.group)]
    legends, buttons = channel.recipient_suggestions.render_chat_list(storage_id, 1, source_chats=source_chats)
    legend = "\n".join(legends)
    assert slave.channel_emoji in legend
    assert slave.channel_name in legend
    assert len(buttons) == 2


def test_chat_pagination_filters_groups_users_and_invalid_regex(channel, slave):
    _, buttons = channel.recipient_suggestions.render_chat_list((TelegramChatID(0), TelegramMessageID(4)), 1, pattern="wonderland")
    names = [button.text for row in buttons[:-1] for button in row]
    assert names and all("Wonderland" in name for name in names)

    for message_id, (pattern, chat_type) in enumerate((("type: group", "GroupChat"), ("type: private", "PrivateChat")), start=5):
        _, buttons = channel.recipient_suggestions.render_chat_list((TelegramChatID(0), TelegramMessageID(message_id)), 1, pattern=pattern)
        names = [button.text for row in buttons[:-1] for button in row]
        expected = slave.get_chats_by_criteria(chat_type=chat_type)
        assert names and all(any(chat.display_name in name for chat in expected) for name in names)

    _, buttons = channel.recipient_suggestions.render_chat_list((TelegramChatID(0), TelegramMessageID(7)), 1, pattern="(")
    assert len(buttons) == 1
