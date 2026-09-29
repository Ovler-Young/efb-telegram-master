from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master.bot_manager import SendReceipt, TelegramBotManager
from efb_telegram_master.outbound import QUEUED_OPERATIONS, QueueEnqueueError


def _manager_with_adapter_stubs():
    manager = TelegramBotManager.__new__(TelegramBotManager)
    manager._bot = Mock()
    manager._cleanup_tls = SimpleNamespace(pending_cleanup=[])
    manager._make_send_receipt = TelegramBotManager._make_send_receipt.__get__(
        manager, TelegramBotManager
    )
    manager._enqueue_blocking_send_and_wait = Mock(
        return_value=SendReceipt(message=SimpleNamespace(chat_id=100))
    )
    manager._enqueue_eventual_send = Mock(
        return_value=SendReceipt(message=SimpleNamespace(chat_id=100), queued=True)
    )
    manager._enqueue_blocking_api_operation = Mock(return_value=True)
    return manager


@pytest.mark.parametrize("operation", sorted(QUEUED_OPERATIONS - {"delete_message"}))
def test_closed_operations_enqueue_through_the_public_adapter(operation):
    manager = _manager_with_adapter_stubs()

    getattr(manager, operation)(chat_id=100)

    assert manager._enqueue_blocking_send_and_wait.call_count == 1
    queued_function = manager._enqueue_blocking_send_and_wait.call_args.args[2]
    assert queued_function.__name__ == operation


def test_delete_message_enqueues_with_a_required_sender():
    manager = _manager_with_adapter_stubs()

    manager.delete_message(100, 42, _sender_bot_id="aux-1")

    assert manager._enqueue_blocking_api_operation.call_args.kwargs["operation"] == "delete_message"
    assert manager._enqueue_blocking_api_operation.call_args.kwargs["required_sender_bot_id"] == "aux-1"


def test_queued_wrapper_rejects_an_unknown_send_mode_before_enqueue():
    manager = _manager_with_adapter_stubs()

    with pytest.raises(QueueEnqueueError):
        manager.send_message(chat_id=100, text="message", _send_mode="later")

    manager._enqueue_blocking_send_and_wait.assert_not_called()


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("answer_callback_query", ("query-id",)),
        ("send_chat_action", (100, "typing")),
        ("send_location", (100, 1.0, 2.0)),
        ("edit_message_reply_markup", ()),
        ("create_forum_topic", (100, "topic")),
        ("edit_forum_topic", (100, 1)),
        ("reopen_forum_topic", (100, 1)),
        ("set_chat_title", (100, "title")),
        ("set_chat_photo", (100, object())),
        ("pin_chat_message", (100, 1)),
        ("set_chat_description", (100, "description")),
    ],
)
def test_operations_outside_the_closed_set_remain_direct(operation, arguments):
    manager = _manager_with_adapter_stubs()
    getattr(manager._bot, operation).return_value = operation

    result = getattr(manager, operation)(*arguments)

    assert manager._enqueue_blocking_send_and_wait.call_count == 0
    assert manager._enqueue_blocking_api_operation.call_count == 0
    assert result == operation or result.message == operation
    getattr(manager._bot, operation).assert_called_once()
