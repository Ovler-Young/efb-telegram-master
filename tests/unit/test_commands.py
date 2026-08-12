from types import SimpleNamespace
from unittest.mock import Mock

from telegram import Update
from telegram.ext import ConversationHandler

from efb_telegram_master.commands import CommandsManager, ETMCommandMsgStorage
from efb_telegram_master.constants import Flags


def test_commands_module_imports_with_typed_modules_list() -> None:
    assert CommandsManager.__name__ == "CommandsManager"


def test_command_registration_authorizes_private_chat_owner_and_group_admins() -> None:
    manager = CommandsManager.__new__(CommandsManager)
    manager.channel = SimpleNamespace(config={"admins": [1, 2]})
    manager.command_conv = SimpleNamespace(_conversations={})
    manager.msg_storage = {}
    private_storage = ETMCommandMsgStorage([], Mock(), "", "")
    group_storage = ETMCommandMsgStorage([], Mock(), "", "")

    manager.register_command(SimpleNamespace(chat=SimpleNamespace(id=9, type="private"), message_id=401), private_storage)
    manager.register_command(SimpleNamespace(chat=SimpleNamespace(id=-1009, type="supergroup"), message_id=402), group_storage)

    assert private_storage.authorized_user_ids == frozenset({9})
    assert group_storage.authorized_user_ids == frozenset({1, 2})


def _callback_update(chat_id: int, message_id: int, user_id: int) -> Update:
    return Update.de_json(
        {
            "update_id": 1,
            "callback_query": {
                "id": "callback-id",
                "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
                "chat_instance": "instance",
                "data": "0",
                "message": {
                    "message_id": message_id,
                    "date": 1,
                    "chat": {"id": chat_id, "type": "private", "first_name": "Tester"},
                },
            },
        },
        None,
    )


def test_command_callback_rejects_users_outside_the_session_authorization() -> None:
    manager = CommandsManager.__new__(CommandsManager)
    manager.channel = SimpleNamespace(_=lambda text: text)
    manager.bot = Mock()
    manager.logger = Mock()
    command = SimpleNamespace(callable_name="run", args=(), kwargs={})
    module = SimpleNamespace(run=Mock(return_value="done"))
    storage = ETMCommandMsgStorage([command], module, "", "")
    storage.authorized_user_ids = frozenset({1})
    manager.msg_storage = {(1, 401): storage}

    state = manager.command_exec(_callback_update(1, 401, user_id=2), SimpleNamespace())

    assert state == Flags.COMMAND_PENDING
    assert (1, 401) in manager.msg_storage
    module.run.assert_not_called()
    manager.bot.answer_callback_query.assert_called_once_with(callback_query_id="callback-id", text="Session expired or unknown parameter. (SE02)")


def test_command_callback_allows_a_configured_group_admin() -> None:
    manager = CommandsManager.__new__(CommandsManager)
    manager.channel = SimpleNamespace(_=lambda text: text)
    manager.bot = Mock()
    manager.logger = Mock()
    command = SimpleNamespace(callable_name="run", args=(), kwargs={})
    module = SimpleNamespace(run=Mock(return_value="done"))
    storage = ETMCommandMsgStorage([command], module, "", "")
    storage.authorized_user_ids = frozenset({1, 2})
    manager.msg_storage = {(1, 401): storage}

    state = manager.command_exec(_callback_update(1, 401, user_id=2), SimpleNamespace())

    assert state == ConversationHandler.END
    module.run.assert_called_once_with()
