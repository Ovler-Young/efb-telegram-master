from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ConversationHandler

from efb_telegram_master.callback_sessions import ChatListStorage
from efb_telegram_master.constants import Flags
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID
from tests.unit.chat_binding_support import callback_chat, callback_update, create_link_action_service, create_link_manager, dispatch_callback, store_callback_session


@pytest.fixture
def callback_manager():
    return create_link_manager()


@pytest.fixture
def link_chat():
    return callback_chat()


def test_link_execute_expires_missing_callback_session(callback_manager):
    storage_id = (TelegramChatID(1), TelegramMessageID(201))
    handler = callback_manager._conversation_handler
    callback_manager.callback_sessions.set_state(handler, storage_id, Flags.LINK_EXEC)

    with patch.object(callback_manager.bot, "edit_message_text"), patch.object(callback_manager.bot, "answer_callback_query") as answer_callback_query:
        assert callback_manager.execute(callback_update(*storage_id, "manual_link 0"), None) == ConversationHandler.END

    assert storage_id not in handler._conversations
    answer_callback_query.assert_called_once_with("callback-id")


@pytest.mark.parametrize("callback", ["manual_link", "manual_link 0 extra", "manual_link nope"])
def test_link_exec_rejects_malformed_callback_tokens(callback_manager, link_chat, callback):
    storage_id = (TelegramChatID(1), TelegramMessageID(202))
    store_callback_session(callback_manager, callback_manager._conversation_handler, Flags.LINK_EXEC, storage_id, [link_chat])

    with patch.object(callback_manager.bot, "edit_message_text"), patch.object(callback_manager.bot, "answer_callback_query"):
        assert callback_manager.execute(callback_update(*storage_id, callback), None) == ConversationHandler.END

    assert callback_manager.callback_sessions.lookup(storage_id) is None
    assert storage_id not in callback_manager._conversation_handler._conversations


@pytest.mark.parametrize("callback", ["offset", "offset nope", "offset 999", "chat nope", "chat 4"])
def test_link_confirmation_rejects_invalid_callback_indexes(callback_manager, link_chat, callback):
    storage_id = (TelegramChatID(1), TelegramMessageID(206))
    store_callback_session(callback_manager, callback_manager._conversation_handler, Flags.LINK_CONFIRM, storage_id, [link_chat])

    with patch.object(callback_manager.bot, "edit_message_text"), patch.object(callback_manager.bot, "answer_callback_query"), patch.object(callback_manager, "render_list") as generate:
        assert callback_manager.confirm(callback_update(*storage_id, callback), None) == ConversationHandler.END

    generate.assert_not_called()
    assert callback_manager.callback_sessions.lookup(storage_id) is None
    assert storage_id not in callback_manager._conversation_handler._conversations


def test_link_confirmation_answers_before_building_the_action_menu(callback_manager, link_chat):
    storage_id = (TelegramChatID(1), TelegramMessageID(208))
    store_callback_session(callback_manager, callback_manager._conversation_handler, Flags.LINK_CONFIRM, storage_id, [link_chat])
    calls = []
    callback_manager.bot.answer_callback_query.side_effect = lambda *_args: calls.append("answer")

    with patch.object(callback_manager.action_service, "render", side_effect=lambda *_args: calls.append("build")):
        assert callback_manager.confirm(callback_update(*storage_id, "chat 0"), None) == Flags.LINK_EXEC

    assert calls == ["answer", "build"]


def test_link_exec_keeps_valid_manual_link_callback_active(callback_manager, link_chat):
    storage_id = (TelegramChatID(1), TelegramMessageID(205))
    store_callback_session(callback_manager, callback_manager._conversation_handler, Flags.LINK_EXEC, storage_id, [link_chat])

    with patch.object(callback_manager.action_service, "execute", return_value=Flags.LINK_EXEC) as execute_action, patch.object(callback_manager.bot, "answer_callback_query") as answer_callback_query:
        assert callback_manager.execute(callback_update(*storage_id, "manual_link 0"), None) == Flags.LINK_EXEC

    execute_action.assert_called_once_with("manual_link", link_chat, *storage_id)
    answer_callback_query.assert_not_called()
    assert callback_manager.callback_sessions.lookup(storage_id) is not None


@pytest.mark.asyncio
async def test_conversation_handler_keeps_link_session_for_an_unauthorized_callback(callback_manager, link_chat):
    storage_id = (TelegramChatID(1), TelegramMessageID(212))

    async def confirm(update, context):
        return callback_manager.confirm(update, context)

    handler = ConversationHandler(entry_points=[], states={Flags.LINK_CONFIRM: [CallbackQueryHandler(confirm)], Flags.LINK_EXEC: []}, fallbacks=[], per_message=True, per_chat=True, per_user=False)
    callback_manager._conversation_handler = handler
    callback_manager.callback_sessions.start(handler, storage_id, Flags.LINK_CONFIRM, 1, ChatListStorage([link_chat]))
    application = ApplicationBuilder().token("123:token").build()

    with patch.object(callback_manager, "render_list") as render_list:
        await dispatch_callback(handler, application, callback_update(*storage_id, "offset 0", user_id=2))

    render_list.assert_not_called()
    await dispatch_callback(handler, application, callback_update(*storage_id, "chat 0", user_id=2))

    assert handler._conversations[storage_id] == Flags.LINK_CONFIRM
    assert callback_manager.callback_sessions.lookup(storage_id) is not None
    assert callback_manager.bot.answer_callback_query.call_args_list[0].args == ("callback-id",)
    assert callback_manager.bot.answer_callback_query.call_args_list[0].kwargs == {"text": "Session expired or unknown parameter. (SE02)"}
    callback_manager.bot.edit_message_text.assert_not_called()

    with patch.object(callback_manager.action_service, "render") as render_action:
        await dispatch_callback(handler, application, callback_update(*storage_id, "chat 0"))

    assert handler._conversations[storage_id] == Flags.LINK_EXEC
    render_action.assert_called_once_with(link_chat, *storage_id)


def test_link_action_service_renders_relink_and_manual_action_menu():
    service = create_link_action_service()
    chat = SimpleNamespace(full_name="Selected chat", linked=True)
    storage_id = (TelegramChatID(1), TelegramMessageID(214))

    service.render(chat, *storage_id)

    kwargs = service.bot.edit_message_text.call_args.kwargs
    assert "This chat has already linked to Telegram." in kwargs["text"]
    assert [button.text for button in kwargs["reply_markup"].inline_keyboard[0]] == ["Relink", "Restore", "Manual Relink"]


def test_link_action_service_keeps_manual_link_session_active():
    service = create_link_action_service()
    chat = SimpleNamespace(full_name="Selected chat", linked=False)
    storage_id = (TelegramChatID(1), TelegramMessageID(215))

    assert service.execute("manual_link", chat, *storage_id) == Flags.LINK_EXEC

    kwargs = service.bot.edit_message_text.call_args.kwargs
    assert "<code>/start" in kwargs["text"]
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == "Cancel"


@pytest.mark.asyncio
async def test_conversation_handler_keeps_link_execute_session_for_unauthorized_callbacks(callback_manager):
    storage_id = (TelegramChatID(1), TelegramMessageID(213))
    chat = SimpleNamespace(module_id="tests.mocks.slave", full_name="Selected chat", linked=True, unlink=Mock())

    async def execute(update, context):
        return callback_manager.execute(update, context)

    handler = ConversationHandler(entry_points=[], states={Flags.LINK_EXEC: [CallbackQueryHandler(execute)]}, fallbacks=[], per_message=True, per_chat=True, per_user=False)
    callback_manager._conversation_handler = handler
    store_callback_session(callback_manager, handler, Flags.LINK_EXEC, storage_id, [chat])
    application = ApplicationBuilder().token("123:token").build()

    await dispatch_callback(handler, application, callback_update(*storage_id, "unlink 0", user_id=2))
    assert handler._conversations[storage_id] == Flags.LINK_EXEC
    assert callback_manager.callback_sessions.lookup(storage_id) is not None
    chat.unlink.assert_not_called()
    callback_manager.bot.edit_message_text.assert_not_called()
    assert callback_manager.bot.answer_callback_query.call_count == 1

    await dispatch_callback(handler, application, callback_update(*storage_id, "unlink 0"))

    chat.unlink.assert_called_once_with()
    assert storage_id not in handler._conversations
    assert callback_manager.callback_sessions.lookup(storage_id) is None
