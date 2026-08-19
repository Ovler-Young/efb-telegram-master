from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram import Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ConversationHandler

from efb_telegram_master.core.constants import Flags
from efb_telegram_master.delivery.commands import CommandsManager, ETMCommandMsgStorage


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


async def _dispatch_callback(handler, application, update):
    check_result = handler.check_update(update)
    assert check_result is not None
    context = application.context_types.context.from_update(update, application)
    await handler.handle_update(update, application, check_result, context)


@pytest.mark.asyncio
async def test_command_callback_is_limited_to_the_message_owner():
    manager = CommandsManager.__new__(CommandsManager)
    manager._ = lambda text: text
    manager.bot = Mock()
    manager.logger = Mock()
    manager.msg_storage = {}

    async def command_exec(update, context):
        return manager.command_exec(update, context)

    manager.command_conv = ConversationHandler(
        entry_points=[],
        states={Flags.COMMAND_PENDING: [CallbackQueryHandler(command_exec)]},
        fallbacks=[],
        per_message=True,
        per_chat=True,
        per_user=False,
    )
    command = SimpleNamespace(callable_name="run", args=(), kwargs={})
    module = SimpleNamespace(run=Mock(return_value="done"))
    message = SimpleNamespace(chat=SimpleNamespace(id=1), message_id=401)
    manager.register_command(message, ETMCommandMsgStorage([command], module, "", "", authorized_user_ids=(1,)))
    application = ApplicationBuilder().token("123:token").build()

    await _dispatch_callback(manager.command_conv, application, _callback_update(1, 401, "0", user_id=2))

    assert manager.command_conv._conversations[(1, 401)] == Flags.COMMAND_PENDING
    assert (1, 401) in manager.msg_storage
    module.run.assert_not_called()
    manager.bot.edit_message_reply_markup.assert_not_called()
    manager.bot.answer_callback_query.assert_called_once_with(callback_query_id="callback-id", text="Session expired or unknown parameter. (SE02)")

    await _dispatch_callback(manager.command_conv, application, _callback_update(1, 401, "0"))

    module.run.assert_called_once_with()
    manager.bot.edit_message_reply_markup.assert_called_once_with(chat_id=1, message_id=401, reply_markup=None)
    assert (1, 401) not in manager.command_conv._conversations
    assert (1, 401) not in manager.msg_storage

    group_message = SimpleNamespace(chat=SimpleNamespace(id=-100500), message_id=402)
    manager.register_command(group_message, ETMCommandMsgStorage([command], module, "", "", authorized_user_ids=(100,)))

    await _dispatch_callback(manager.command_conv, application, _callback_update(-100500, 402, "0", user_id=2))

    assert manager.command_conv._conversations[(-100500, 402)] == Flags.COMMAND_PENDING
    assert (-100500, 402) in manager.msg_storage
    assert module.run.call_count == 1
    assert manager.bot.edit_message_reply_markup.call_count == 1

    await _dispatch_callback(manager.command_conv, application, _callback_update(-100500, 402, "0", user_id=100))

    assert module.run.call_count == 2
    assert manager.bot.edit_message_reply_markup.call_count == 2
    assert (-100500, 402) not in manager.command_conv._conversations
    assert (-100500, 402) not in manager.msg_storage
