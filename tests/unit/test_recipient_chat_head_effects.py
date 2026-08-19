from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest
from telegram.ext import ConversationHandler

from efb_telegram_master.chat.chat_head import ChatHeadService
from efb_telegram_master.core.constants import Flags
from efb_telegram_master.core.utils import TelegramChatID, TelegramMessageID
from efb_telegram_master.link.callback_sessions import CallbackSessionStore, ChatListStorage
from efb_telegram_master.link.recipient_suggestions import RecipientSuggestionService
from tests.unit.chat_binding_support import callback_update


def test_recipient_selection_delivers_the_stored_update_to_the_selected_chat():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    message_delivery = Mock()
    handler = SimpleNamespace(_conversations={})
    service = RecipientSuggestionService(bot, callback_sessions, Mock(), message_delivery, lambda: 10, lambda text: text, Mock(), handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(220))
    selected_chat = SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat")
    storage = ChatListStorage([selected_chat])
    original_update = Mock()
    storage.set_chat_suggestion(original_update)
    callback_sessions.start(handler, storage_id, Flags.SUGGEST_RECIPIENTS, 1, storage)

    assert service.suggested_recipient(callback_update(*storage_id, "chat 0"), Mock()) == ConversationHandler.END

    message_delivery.deliver.assert_called_once_with(original_update, ANY, "tests.mocks.slave chat")
    assert callback_sessions.lookup(storage_id) is None
    assert storage_id not in handler._conversations


def test_chat_head_selection_records_a_reply_target_and_cleans_its_session():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    chat = SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat", self=Mock(), add_self=Mock())
    msglogs = Mock()
    handler = SimpleNamespace(_conversations={})
    service = ChatHeadService(bot, callback_sessions, Mock(), Mock(), SimpleNamespace(channel_id="blueset.telegram"), msglogs, Mock(), lambda text: text, handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(221))
    callback_sessions.start(handler, storage_id, Flags.CHAT_HEAD_CONFIRM, 1, ChatListStorage([chat]))

    assert service.make_chat_head(callback_update(*storage_id, "chat 0"), Mock()) == ConversationHandler.END

    msglogs.add_or_update_message_log.assert_called_once()
    assert callback_sessions.lookup(storage_id) is None
    assert storage_id not in handler._conversations


@pytest.mark.parametrize("callback", ["chat", "chat 0 extra", "chat nope", "chat 4"])
def test_recipient_selection_rejects_malformed_or_stale_callbacks(callback):
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    delivery = Mock()
    handler = SimpleNamespace(_conversations={})
    service = RecipientSuggestionService(bot, callback_sessions, Mock(), delivery, lambda: 10, lambda text: text, Mock(), handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(222))
    storage = ChatListStorage([SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat")])
    storage.set_chat_suggestion(Mock())
    callback_sessions.start(handler, storage_id, Flags.SUGGEST_RECIPIENTS, 1, storage)

    assert service.suggested_recipient(callback_update(*storage_id, callback), Mock()) == ConversationHandler.END

    delivery.deliver.assert_not_called()
    assert callback_sessions.lookup(storage_id) is None
    assert storage_id not in handler._conversations


@pytest.mark.parametrize("callback", ["offset", "offset 0 extra", "offset nope", "offset -1", "offset 999", "chat 4"])
def test_chat_head_rejects_malformed_or_out_of_range_callbacks(callback):
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    msglogs = Mock()
    handler = SimpleNamespace(_conversations={})
    service = ChatHeadService(bot, callback_sessions, Mock(), Mock(), SimpleNamespace(channel_id="blueset.telegram"), msglogs, Mock(), lambda text: text, handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(224))
    callback_sessions.start(handler, storage_id, Flags.CHAT_HEAD_CONFIRM, 1, ChatListStorage([SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat")]))

    with patch.object(service, "render_chat_head") as render_chat_head:
        assert service.make_chat_head(callback_update(*storage_id, callback), Mock()) == ConversationHandler.END

    render_chat_head.assert_not_called()
    msglogs.add_or_update_message_log.assert_not_called()
    assert callback_sessions.lookup(storage_id) is None
    assert storage_id not in handler._conversations
