from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest
from telegram import Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ConversationHandler

from efb_telegram_master import utils
from efb_telegram_master.callback_sessions import CallbackSessionStore, ChatListStorage
from efb_telegram_master.chat_head import ChatHeadService
from efb_telegram_master.constants import Flags
from efb_telegram_master.link_service import LinkService
from efb_telegram_master.recipient_suggestions import RecipientSuggestionService
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID


def _callback_update(chat_id, message_id, data, user_id=1):
    return Update.de_json(
        {
            "update_id": 1,
            "callback_query": {
                "id": "callback-id",
                "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
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
    manager = LinkService.__new__(LinkService)
    manager.bot = Mock()
    manager.channel = SimpleNamespace(_=lambda text: text, flag=lambda _name: 10)
    manager._ = lambda text: text
    manager.callback_sessions = CallbackSessionStore(manager.bot, lambda: manager.channel.flag("chats_per_page"))
    manager.link_handler = SimpleNamespace(_conversations={})
    return manager


@pytest.fixture
def callback_chat():
    return SimpleNamespace(module_id="tests.mocks.slave", full_name="Test chat", linked=False)


def _store_callback_session(manager, handler, state, storage_id, chats):
    manager.callback_sessions.start(handler, storage_id, state, 1, ChatListStorage(chats))


async def _dispatch_callback(handler, application, update):
    check_result = handler.check_update(update)
    assert check_result is not None
    context = application.context_types.context.from_update(update, application)
    await handler.handle_update(update, application, check_result, context)


@pytest.mark.parametrize(
    ("handler_name", "state", "callback"),
    [
        ("link_handler", Flags.LINK_EXEC, "manual_link 0"),
    ],
)
def test_callback_handlers_expire_missing_sessions(callback_manager, handler_name, state, callback):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(201))
    handler = getattr(manager, handler_name)
    manager.callback_sessions.set_state(handler, storage_id, state)
    update = _callback_update(*storage_id, callback)
    method = {
        "link_handler": manager.execute,
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
        assert manager.execute(_callback_update(*storage_id, callback), None) == ConversationHandler.END

    assert manager.callback_sessions.lookup(storage_id) is None
    assert storage_id not in manager.link_handler._conversations


@pytest.mark.parametrize("callback", ["offset", "offset nope", "offset 999", "chat nope", "chat 4"])
def test_link_confirmation_rejects_invalid_callback_indexes(callback_manager, callback_chat, callback):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(206))
    _store_callback_session(manager, manager.link_handler, Flags.LINK_CONFIRM, storage_id, [callback_chat])

    with patch.object(manager.bot, "edit_message_text"), patch.object(manager.bot, "answer_callback_query"), patch.object(manager, "render_list") as generate:
        assert manager.confirm(_callback_update(*storage_id, callback), None) == ConversationHandler.END

    generate.assert_not_called()
    assert manager.callback_sessions.lookup(storage_id) is None
    assert storage_id not in manager.link_handler._conversations


def test_link_confirmation_answers_before_building_the_action_menu(callback_manager, callback_chat):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(208))
    _store_callback_session(manager, manager.link_handler, Flags.LINK_CONFIRM, storage_id, [callback_chat])
    calls = []
    manager.bot.answer_callback_query.side_effect = lambda *_args: calls.append("answer")

    with patch.object(manager, "build_action", side_effect=lambda *_args: calls.append("build")):
        assert manager.confirm(_callback_update(*storage_id, "chat 0"), None) == Flags.LINK_EXEC

    assert calls == ["answer", "build"]


def test_link_exec_keeps_valid_manual_link_callback_active(callback_manager, callback_chat):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(205))
    _store_callback_session(manager, manager.link_handler, Flags.LINK_EXEC, storage_id, [callback_chat])

    with patch.object(manager, "build_action") as build_link_action_message, patch.object(manager.bot, "answer_callback_query") as answer_callback_query:
        assert manager.execute(_callback_update(*storage_id, "manual_link 0"), None) == Flags.LINK_EXEC

    build_link_action_message.assert_not_called()
    answer_callback_query.assert_not_called()
    assert manager.callback_sessions.lookup(storage_id) is not None


def test_other_user_cannot_use_link_callback_and_owner_can_continue(callback_manager, callback_chat):
    storage_id = (TelegramChatID(1), TelegramMessageID(210))
    _store_callback_session(callback_manager, callback_manager.link_handler, Flags.LINK_EXEC, storage_id, [callback_chat])

    assert callback_manager.execute(_callback_update(*storage_id, "manual_link 0", user_id=2), None) == Flags.LINK_EXEC

    assert callback_manager.callback_sessions.lookup(storage_id) is not None
    callback_manager.bot.edit_message_text.assert_not_called()
    callback_manager.bot.answer_callback_query.assert_called_once_with("callback-id", text="Session expired or unknown parameter. (SE02)")
    assert callback_manager.execute(_callback_update(*storage_id, "manual_link 0"), None) == Flags.LINK_EXEC


def test_other_user_cannot_paginate_link_session(callback_manager, callback_chat):
    storage_id = (TelegramChatID(1), TelegramMessageID(211))
    _store_callback_session(callback_manager, callback_manager.link_handler, Flags.LINK_CONFIRM, storage_id, [callback_chat])

    with patch.object(callback_manager, "render_list") as render_list:
        assert callback_manager.confirm(_callback_update(*storage_id, "offset 0", user_id=2), None) == Flags.LINK_CONFIRM

    render_list.assert_not_called()
    assert callback_manager.callback_sessions.lookup(storage_id) is not None
    assert storage_id in callback_manager.link_handler._conversations


@pytest.mark.asyncio
async def test_conversation_handler_keeps_link_session_for_an_unauthorized_callback(callback_manager, callback_chat):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(212))

    async def confirm(update, context):
        return manager.confirm(update, context)

    handler = ConversationHandler(
        entry_points=[],
        states={Flags.LINK_CONFIRM: [CallbackQueryHandler(confirm)], Flags.LINK_EXEC: []},
        fallbacks=[],
        per_message=True,
        per_chat=True,
        per_user=False,
    )
    manager.set_handler(handler)
    manager.callback_sessions.start(handler, storage_id, Flags.LINK_CONFIRM, 1, ChatListStorage([callback_chat]))
    application = ApplicationBuilder().token("123:token").build()

    attacker = _callback_update(*storage_id, "chat 0", user_id=2)
    await _dispatch_callback(handler, application, attacker)

    assert handler._conversations[storage_id] == Flags.LINK_CONFIRM
    assert manager.callback_sessions.lookup(storage_id) is not None
    manager.bot.answer_callback_query.assert_called_once_with("callback-id", text="Session expired or unknown parameter. (SE02)")
    manager.bot.edit_message_text.assert_not_called()

    await _dispatch_callback(handler, application, attacker)
    assert handler._conversations[storage_id] == Flags.LINK_CONFIRM
    assert manager.callback_sessions.lookup(storage_id) is not None
    assert manager.bot.answer_callback_query.call_count == 2

    owner = _callback_update(*storage_id, "chat 0")
    with patch.object(manager, "build_action") as build_action:
        await _dispatch_callback(handler, application, owner)

    assert handler._conversations[storage_id] == Flags.LINK_EXEC
    build_action.assert_called_once_with(callback_chat, *storage_id)


@pytest.mark.asyncio
async def test_conversation_handler_keeps_link_execute_session_for_unauthorized_callbacks(callback_manager):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(213))
    chat = SimpleNamespace(module_id="tests.mocks.slave", full_name="Selected chat", linked=True, unlink=Mock())

    async def execute(update, context):
        return manager.execute(update, context)

    handler = ConversationHandler(
        entry_points=[],
        states={Flags.LINK_EXEC: [CallbackQueryHandler(execute)]},
        fallbacks=[],
        per_message=True,
        per_chat=True,
        per_user=False,
    )
    manager.set_handler(handler)
    manager.callback_sessions.start(handler, storage_id, Flags.LINK_EXEC, 1, ChatListStorage([chat]))
    application = ApplicationBuilder().token("123:token").build()
    attacker = _callback_update(*storage_id, "unlink 0", user_id=2)

    await _dispatch_callback(handler, application, attacker)
    await _dispatch_callback(handler, application, attacker)

    assert handler._conversations[storage_id] == Flags.LINK_EXEC
    assert manager.callback_sessions.lookup(storage_id) is not None
    chat.unlink.assert_not_called()
    manager.bot.edit_message_text.assert_not_called()
    assert manager.bot.answer_callback_query.call_count == 2

    await _dispatch_callback(handler, application, _callback_update(*storage_id, "unlink 0"))

    chat.unlink.assert_called_once_with()
    assert storage_id not in handler._conversations
    assert manager.callback_sessions.lookup(storage_id) is None


@pytest.mark.asyncio
async def test_conversation_handler_keeps_chat_head_session_for_unauthorized_callbacks():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    chat = SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat", self=Mock(), add_self=Mock())
    msglogs = Mock()
    service = ChatHeadService(bot, callback_sessions, Mock(), Mock(), SimpleNamespace(channel_id="blueset.telegram"), msglogs, Mock(), lambda text: text)
    storage_id = (TelegramChatID(1), TelegramMessageID(228))

    async def make_chat_head(update, context):
        return service.make_chat_head(update, context)

    handler = ConversationHandler(
        entry_points=[],
        states={Flags.CHAT_HEAD_CONFIRM: [CallbackQueryHandler(make_chat_head)]},
        fallbacks=[],
        per_message=True,
        per_chat=True,
        per_user=False,
    )
    service.set_handler(handler)
    callback_sessions.start(handler, storage_id, Flags.CHAT_HEAD_CONFIRM, 1, ChatListStorage([chat]))
    application = ApplicationBuilder().token("123:token").build()
    attacker = _callback_update(*storage_id, "chat 0", user_id=2)

    await _dispatch_callback(handler, application, attacker)
    await _dispatch_callback(handler, application, attacker)

    assert handler._conversations[storage_id] == Flags.CHAT_HEAD_CONFIRM
    assert callback_sessions.lookup(storage_id) is not None
    msglogs.add_or_update_message_log.assert_not_called()
    bot.edit_message_text.assert_not_called()
    assert bot.answer_callback_query.call_count == 2

    await _dispatch_callback(handler, application, _callback_update(*storage_id, "chat 0"))

    msglogs.add_or_update_message_log.assert_called_once()
    assert storage_id not in handler._conversations
    assert callback_sessions.lookup(storage_id) is None


@pytest.mark.asyncio
async def test_conversation_handler_keeps_recipient_session_for_unauthorized_callbacks():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    delivery = Mock()
    service = RecipientSuggestionService(bot, callback_sessions, Mock(), delivery, lambda: 10, lambda text: text, Mock())
    storage_id = (TelegramChatID(1), TelegramMessageID(229))
    chat = SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat")
    storage = ChatListStorage([chat])
    original_update = Mock()
    storage.set_chat_suggestion(original_update)

    async def suggested_recipient(update, context):
        return service.suggested_recipient(update, context)

    handler = ConversationHandler(
        entry_points=[],
        states={Flags.SUGGEST_RECIPIENTS: [CallbackQueryHandler(suggested_recipient)]},
        fallbacks=[],
        per_message=True,
        per_chat=True,
        per_user=False,
    )
    service.set_handler(handler)
    callback_sessions.start(handler, storage_id, Flags.SUGGEST_RECIPIENTS, 1, storage)
    application = ApplicationBuilder().token("123:token").build()
    attacker = _callback_update(*storage_id, "chat 0", user_id=2)

    await _dispatch_callback(handler, application, attacker)
    await _dispatch_callback(handler, application, attacker)

    assert handler._conversations[storage_id] == Flags.SUGGEST_RECIPIENTS
    assert callback_sessions.lookup(storage_id) is storage
    delivery.deliver.assert_not_called()
    bot.edit_message_text.assert_not_called()
    assert bot.answer_callback_query.call_count == 2

    await _dispatch_callback(handler, application, _callback_update(*storage_id, "chat 0"))

    delivery.deliver.assert_called_once_with(original_update, ANY, "tests.mocks.slave chat")
    assert storage_id not in handler._conversations
    assert callback_sessions.lookup(storage_id) is None


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
    manager.callback_sessions.lookup(storage_id).offset = manager.channel.flag("chats_per_page")
    manager.logger = Mock()

    with patch.object(manager, "_get_bot_user", return_value=SimpleNamespace(username="test_bot")):
        assert manager.confirm(_callback_update(*storage_id, "chat 10"), None) == Flags.LINK_EXEC

    assert manager.callback_sessions.lookup(storage_id).chats == [selected_chat]
    with patch.object(manager.bot, "answer_callback_query"):
        assert manager.execute(_callback_update(*storage_id, callback), None) == expected_state

    if linked:
        selected_chat.unlink.assert_called_once_with()
    else:
        assert manager.callback_sessions.lookup(storage_id) is not None


@pytest.mark.parametrize("callback", ["manual_link 10", "unlink 10"])
def test_link_actions_reject_stale_second_page_index_after_selection(callback_manager, callback):
    manager = callback_manager
    storage_id = (TelegramChatID(1), TelegramMessageID(208))
    selected_chat = SimpleNamespace(module_id="tests.mocks.slave", full_name="Selected chat", linked=False, unlink=Mock())
    _store_callback_session(manager, manager.link_handler, Flags.LINK_EXEC, storage_id, [selected_chat])
    manager.callback_sessions.lookup(storage_id).offset = manager.channel.flag("chats_per_page")

    with patch.object(manager.bot, "edit_message_text"), patch.object(manager.bot, "answer_callback_query"):
        assert manager.execute(_callback_update(*storage_id, callback), None) == ConversationHandler.END

    selected_chat.unlink.assert_not_called()
    assert manager.callback_sessions.lookup(storage_id) is None


def test_callback_handler_expires_when_conversation_state_is_missing(callback_manager, callback_chat):
    storage_id = (TelegramChatID(1), TelegramMessageID(209))
    callback_manager.callback_sessions.store(storage_id, 1, ChatListStorage([callback_chat]))

    with patch.object(callback_manager.bot, "edit_message_text"), patch.object(callback_manager.bot, "answer_callback_query"):
        assert callback_manager.execute(_callback_update(*storage_id, "manual_link 0"), None) == ConversationHandler.END

    assert callback_manager.callback_sessions.lookup(storage_id) is None


def test_recipient_selection_delivers_the_stored_update_to_the_selected_chat():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    message_delivery = Mock()
    service = RecipientSuggestionService(bot, callback_sessions, Mock(), message_delivery, lambda: 10, lambda text: text, Mock())
    handler = SimpleNamespace(_conversations={})
    service.set_handler(handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(220))
    selected_chat = SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat")
    storage = ChatListStorage([selected_chat])
    original_update = Mock()
    storage.set_chat_suggestion(original_update)
    callback_sessions.start(handler, storage_id, Flags.SUGGEST_RECIPIENTS, 1, storage)

    assert service.suggested_recipient(_callback_update(*storage_id, "chat 0"), Mock()) == ConversationHandler.END

    message_delivery.deliver.assert_called_once_with(original_update, ANY, "tests.mocks.slave chat")
    assert callback_sessions.lookup(storage_id) is None
    assert storage_id not in handler._conversations


def test_other_user_cannot_select_recipient_and_owner_can_continue():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    delivery = Mock()
    service = RecipientSuggestionService(bot, callback_sessions, Mock(), delivery, lambda: 10, lambda text: text, Mock())
    handler = SimpleNamespace(_conversations={})
    service.set_handler(handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(226))
    storage = ChatListStorage([SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat")])
    original_update = Mock()
    storage.set_chat_suggestion(original_update)
    callback_sessions.start(handler, storage_id, Flags.SUGGEST_RECIPIENTS, 1, storage)

    assert service.suggested_recipient(_callback_update(*storage_id, "chat 0", user_id=2), Mock()) == Flags.SUGGEST_RECIPIENTS

    delivery.deliver.assert_not_called()
    assert callback_sessions.lookup(storage_id) is storage
    assert service.suggested_recipient(_callback_update(*storage_id, "chat 0"), Mock()) == ConversationHandler.END
    delivery.deliver.assert_called_once_with(original_update, ANY, "tests.mocks.slave chat")


def test_chat_head_selection_records_a_reply_target_and_cleans_its_session():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    chat = SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat", self=Mock(), add_self=Mock())
    msglogs = Mock()
    service = ChatHeadService(bot, callback_sessions, Mock(), Mock(), SimpleNamespace(channel_id="blueset.telegram"), msglogs, Mock(), lambda text: text)
    handler = SimpleNamespace(_conversations={})
    service.set_handler(handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(221))
    callback_sessions.start(handler, storage_id, Flags.CHAT_HEAD_CONFIRM, 1, ChatListStorage([chat]))

    assert service.make_chat_head(_callback_update(*storage_id, "chat 0"), Mock()) == ConversationHandler.END

    msglogs.add_or_update_message_log.assert_called_once()
    assert callback_sessions.lookup(storage_id) is None
    assert storage_id not in handler._conversations


def test_other_user_cannot_create_chat_head():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    chat = SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat", self=Mock(), add_self=Mock())
    msglogs = Mock()
    service = ChatHeadService(bot, callback_sessions, Mock(), Mock(), SimpleNamespace(channel_id="blueset.telegram"), msglogs, Mock(), lambda text: text)
    handler = SimpleNamespace(_conversations={})
    service.set_handler(handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(227))
    callback_sessions.start(handler, storage_id, Flags.CHAT_HEAD_CONFIRM, 1, ChatListStorage([chat]))

    assert service.make_chat_head(_callback_update(*storage_id, "chat 0", user_id=2), Mock()) == Flags.CHAT_HEAD_CONFIRM

    msglogs.add_or_update_message_log.assert_not_called()
    assert callback_sessions.lookup(storage_id) is not None


@pytest.mark.parametrize("callback", ["chat", "chat 0 extra", "chat nope", "chat 4"])
def test_recipient_selection_rejects_malformed_or_stale_callbacks(callback):
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    delivery = Mock()
    service = RecipientSuggestionService(bot, callback_sessions, Mock(), delivery, lambda: 10, lambda text: text, Mock())
    handler = SimpleNamespace(_conversations={})
    service.set_handler(handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(222))
    storage = ChatListStorage([SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat")])
    storage.set_chat_suggestion(Mock())
    callback_sessions.start(handler, storage_id, Flags.SUGGEST_RECIPIENTS, 1, storage)

    assert service.suggested_recipient(_callback_update(*storage_id, callback), Mock()) == ConversationHandler.END

    delivery.deliver.assert_not_called()
    assert callback_sessions.lookup(storage_id) is None
    assert storage_id not in handler._conversations


def test_recipient_selection_expires_when_the_session_is_missing():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    delivery = Mock()
    service = RecipientSuggestionService(bot, callback_sessions, Mock(), delivery, lambda: 10, lambda text: text, Mock())
    handler = SimpleNamespace(_conversations={})
    service.set_handler(handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(223))
    callback_sessions.set_state(handler, storage_id, Flags.SUGGEST_RECIPIENTS)

    assert service.suggested_recipient(_callback_update(*storage_id, "chat 0"), Mock()) == ConversationHandler.END

    delivery.deliver.assert_not_called()
    assert storage_id not in handler._conversations


@pytest.mark.parametrize("callback", ["offset", "offset 0 extra", "offset nope", "offset -1", "offset 999", "chat 4"])
def test_chat_head_rejects_malformed_or_out_of_range_callbacks(callback):
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    msglogs = Mock()
    service = ChatHeadService(bot, callback_sessions, Mock(), Mock(), SimpleNamespace(channel_id="blueset.telegram"), msglogs, Mock(), lambda text: text)
    handler = SimpleNamespace(_conversations={})
    service.set_handler(handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(224))
    callback_sessions.start(handler, storage_id, Flags.CHAT_HEAD_CONFIRM, 1, ChatListStorage([SimpleNamespace(module_id="tests.mocks.slave", uid="chat", full_name="Selected chat")]))

    with patch.object(service, "render_chat_head") as render_chat_head:
        assert service.make_chat_head(_callback_update(*storage_id, callback), Mock()) == ConversationHandler.END

    render_chat_head.assert_not_called()
    msglogs.add_or_update_message_log.assert_not_called()
    assert callback_sessions.lookup(storage_id) is None
    assert storage_id not in handler._conversations


def test_chat_head_expires_when_the_session_is_missing():
    bot = Mock()
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    msglogs = Mock()
    service = ChatHeadService(bot, callback_sessions, Mock(), Mock(), SimpleNamespace(channel_id="blueset.telegram"), msglogs, Mock(), lambda text: text)
    handler = SimpleNamespace(_conversations={})
    service.set_handler(handler)
    storage_id = (TelegramChatID(1), TelegramMessageID(225))
    callback_sessions.set_state(handler, storage_id, Flags.CHAT_HEAD_CONFIRM)

    assert service.make_chat_head(_callback_update(*storage_id, "chat 0"), Mock()) == ConversationHandler.END

    msglogs.add_or_update_message_log.assert_not_called()
    assert storage_id not in handler._conversations


def test_chat_binding_handlers_keep_the_original_registration_order(channel):
    handlers = channel.telegram_runtime.application.handlers[0]

    def command_index(command):
        return next(index for index, handler in enumerate(handlers) if isinstance(handler, CommandHandler) and command in handler.commands)

    def conversation_index(state):
        return next(index for index, handler in enumerate(handlers) if isinstance(handler, ConversationHandler) and state in handler.states)

    assert command_index("link") < conversation_index(Flags.LINK_CONFIRM)
    assert conversation_index(Flags.LINK_CONFIRM) < command_index("chat") < conversation_index(Flags.CHAT_HEAD_CONFIRM)
    assert conversation_index(Flags.CHAT_HEAD_CONFIRM) < command_index("unlink_all") < conversation_index(Flags.SUGGEST_RECIPIENTS)
    assert conversation_index(Flags.SUGGEST_RECIPIENTS) < command_index("update_info") < command_index("init_topics")


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


def test_truncate_ellipsis(channel):
    truncate_ellipsis = channel.topic_sync.truncate_ellipsis
    short_text = "short text"
    long_text = "This is a long text. Cursus pellentesque cras maecenas hac malesuada porttitor nullam, dignissim enim feugiat placerat eget quisque, dui sem dictum fames sapien mauris. Feugiat euismod nisi donec nunc cras aliquam diam, arcu fames pretium pellentesque faucibus phasellus, in montes felis elit lacinia auctor. Commodo curae nibh donec vel ipsum sociosqu maecenas pellentesque scelerisque suspendisse blandit himenaeos rutrum ad, nec dictum porttitor non luctus fringilla feugiat volutpat adipiscing cubilia vitae lacus. Tempor iaculis facilisis maecenas quam nisl pulvinar magnis lacus, sodales porta quisque rutrum habitasse metus purus ante libero, malesuada mollis est donec cubilia accumsan parturient. Parturient libero gravida imperdiet massa praesent habitant scelerisque pellentesque mollis elit, urna quisque tellus in nostra aliquet montes natoque fermentum, condimentum enim magna odio vestibulum mauris viverra sagittis iaculis."
    assert truncate_ellipsis(short_text, len(short_text) + 10) == short_text
    truncated = truncate_ellipsis(long_text, 256)
    assert len(truncated) <= 256
    assert truncated.endswith("…")


# All other methods are to be tested with integration testing.
