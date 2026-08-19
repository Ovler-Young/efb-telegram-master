from types import SimpleNamespace
from unittest.mock import ANY, Mock

import pytest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ConversationHandler

from efb_telegram_master.chat.chat_head import ChatHeadService
from efb_telegram_master.core.constants import Flags
from efb_telegram_master.core.utils import TelegramChatID, TelegramMessageID
from efb_telegram_master.link.callback_sessions import CallbackSessionStore, ChatListStorage
from efb_telegram_master.link.recipient_suggestions import RecipientSuggestionService
from tests.unit.chat_binding_support import callback_chat, callback_update, create_link_manager, dispatch_callback


def test_channel_injects_conversation_handlers_into_binding_services(channel):
    assert channel.link_service._conversation_handler is channel.link_handler
    assert channel.link_completion._conversation_handler is channel.link_handler
    assert channel.chat_head._conversation_handler is channel.chat_head_handler
    assert channel.recipient_suggestions._conversation_handler is channel.suggestion_handler


@pytest.mark.asyncio
async def test_conversation_handler_keeps_chat_head_session_for_unauthorized_callbacks():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    chat = SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat", self=Mock(), add_self=Mock())
    msglogs = Mock()
    storage_id = (TelegramChatID(1), TelegramMessageID(228))

    async def make_chat_head(update, context):
        return service.make_chat_head(update, context)

    handler = ConversationHandler(entry_points=[], states={Flags.CHAT_HEAD_CONFIRM: [CallbackQueryHandler(make_chat_head)]}, fallbacks=[], per_message=True, per_chat=True, per_user=False)
    service = ChatHeadService(bot, callback_sessions, Mock(), Mock(), SimpleNamespace(channel_id="blueset.telegram"), msglogs, Mock(), lambda text: text, handler)
    callback_sessions.start(handler, storage_id, Flags.CHAT_HEAD_CONFIRM, 1, ChatListStorage([chat]))
    application = ApplicationBuilder().token("123:token").build()

    await dispatch_callback(handler, application, callback_update(*storage_id, "chat 0", user_id=2))
    assert handler._conversations[storage_id] == Flags.CHAT_HEAD_CONFIRM
    assert callback_sessions.lookup(storage_id) is not None
    msglogs.add_or_update_message_log.assert_not_called()
    bot.edit_message_text.assert_not_called()
    assert bot.answer_callback_query.call_count == 1

    await dispatch_callback(handler, application, callback_update(*storage_id, "chat 0"))

    msglogs.add_or_update_message_log.assert_called_once()
    assert storage_id not in handler._conversations
    assert callback_sessions.lookup(storage_id) is None


@pytest.mark.asyncio
async def test_conversation_handler_keeps_recipient_session_for_unauthorized_callbacks():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    delivery = Mock()
    storage_id = (TelegramChatID(1), TelegramMessageID(229))
    chat = SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat")
    storage = ChatListStorage([chat])
    original_update = Mock()
    storage.set_chat_suggestion(original_update)

    async def suggested_recipient(update, context):
        return service.suggested_recipient(update, context)

    handler = ConversationHandler(entry_points=[], states={Flags.SUGGEST_RECIPIENTS: [CallbackQueryHandler(suggested_recipient)]}, fallbacks=[], per_message=True, per_chat=True, per_user=False)
    service = RecipientSuggestionService(bot, callback_sessions, Mock(), delivery, lambda: 10, lambda text: text, Mock(), handler)
    callback_sessions.start(handler, storage_id, Flags.SUGGEST_RECIPIENTS, 1, storage)
    application = ApplicationBuilder().token("123:token").build()

    await dispatch_callback(handler, application, callback_update(*storage_id, "chat 0", user_id=2))
    assert handler._conversations[storage_id] == Flags.SUGGEST_RECIPIENTS
    assert callback_sessions.lookup(storage_id) is storage
    delivery.deliver.assert_not_called()
    bot.edit_message_text.assert_not_called()
    assert bot.answer_callback_query.call_count == 1

    await dispatch_callback(handler, application, callback_update(*storage_id, "chat 0"))

    delivery.deliver.assert_called_once_with(original_update, ANY, "tests.mocks.slave chat")
    assert storage_id not in handler._conversations
    assert callback_sessions.lookup(storage_id) is None


def test_callback_handler_expires_when_conversation_state_is_missing():
    manager = create_link_manager()
    chat = callback_chat()
    storage_id = (TelegramChatID(1), TelegramMessageID(209))
    manager.callback_sessions.store(storage_id, 1, ChatListStorage([chat]))

    assert manager.execute(callback_update(*storage_id, "manual_link 0"), None) == ConversationHandler.END

    assert manager.callback_sessions.lookup(storage_id) is None


def test_recipient_selection_expires_when_the_session_is_missing():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    delivery = Mock()
    handler = SimpleNamespace(_conversations={})
    service = RecipientSuggestionService(bot, callback_sessions, Mock(), delivery, lambda: 10, lambda text: text, Mock(), handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(223))
    callback_sessions.set_state(handler, storage_id, Flags.SUGGEST_RECIPIENTS)

    assert service.suggested_recipient(callback_update(*storage_id, "chat 0"), Mock()) == ConversationHandler.END

    delivery.deliver.assert_not_called()
    assert storage_id not in handler._conversations


def test_chat_head_expires_when_the_session_is_missing():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    msglogs = Mock()
    handler = SimpleNamespace(_conversations={})
    service = ChatHeadService(bot, callback_sessions, Mock(), Mock(), SimpleNamespace(channel_id="blueset.telegram"), msglogs, Mock(), lambda text: text, handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(225))
    callback_sessions.set_state(handler, storage_id, Flags.CHAT_HEAD_CONFIRM)

    assert service.make_chat_head(callback_update(*storage_id, "chat 0"), Mock()) == ConversationHandler.END

    msglogs.add_or_update_message_log.assert_not_called()
    assert storage_id not in handler._conversations
