from types import SimpleNamespace
from unittest.mock import Mock

from telegram import Update

from efb_telegram_master.link.callback_sessions import CallbackSessionStore, ChatListStorage
from efb_telegram_master.link.link_actions import LinkActionService
from efb_telegram_master.link.link_service import LinkService


def callback_update(chat_id, message_id, data, user_id=1):
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


def callback_chat():
    return SimpleNamespace(module_id="tests.mocks.slave", full_name="Test chat", linked=False)


def create_link_manager():
    manager = LinkService.__new__(LinkService)
    manager.bot = Mock()
    manager.channel = SimpleNamespace(_=lambda text: text, flag=lambda _name: 10)
    manager._ = lambda text: text
    manager.callback_sessions = CallbackSessionStore(manager.bot, lambda: manager.channel.flag("chats_per_page"))
    manager._conversation_handler = SimpleNamespace(_conversations={})
    manager.action_service = create_link_action_service()
    manager.action_service.bot = manager.bot
    return manager


def create_link_action_service():
    service = LinkActionService.__new__(LinkActionService)
    service.bot = Mock()
    service._ = lambda text: text
    service.runtime = SimpleNamespace(me=SimpleNamespace(username="test_bot"))
    service.logger = Mock()
    return service


def store_callback_session(manager, handler, state, storage_id, chats):
    manager.callback_sessions.start(handler, storage_id, state, 1, ChatListStorage(chats))


async def dispatch_callback(handler, application, update):
    check_result = handler.check_update(update)
    assert check_result is not None
    context = application.context_types.context.from_update(update, application)
    await handler.handle_update(update, application, check_result, context)
