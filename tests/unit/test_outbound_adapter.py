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


@pytest.mark.parametrize(
    "operation",
    sorted(QUEUED_OPERATIONS - {
        "delete_message", "send_chat_action", "edit_message_reply_markup",
        "send_location", "send_venue", "create_forum_topic", "edit_forum_topic",
        "reopen_forum_topic", "set_chat_title", "set_chat_photo", "pin_chat_message",
        "set_chat_description",
    }),
)
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
        ("send_chat_action", (100, "typing")),
        ("edit_message_reply_markup", (100,)),
        ("send_location", (100, 1.0, 2.0)),
        ("send_venue", (100, 1.0, 2.0, "title", "address")),
        ("create_forum_topic", (100, "topic")),
        ("edit_forum_topic", (100, 1)),
        ("reopen_forum_topic", (100, 1)),
        ("set_chat_title", (100, "title")),
        ("set_chat_photo", (100, object())),
        ("pin_chat_message", (100, 1)),
        ("set_chat_description", (100, "description")),
    ],
)
def test_chat_mutations_enqueue_with_the_main_sender(operation, arguments):
    manager = _manager_with_adapter_stubs()
    manager._enqueue_main_chat_mutation = Mock(return_value=operation)

    result = getattr(manager, operation)(*arguments)

    assert manager._enqueue_blocking_send_and_wait.call_count == 0
    assert result == operation
    assert manager._enqueue_main_chat_mutation.call_args.args[:2] == (operation, arguments)


def test_send_chat_action_preserves_thread_id_without_mutating_caller_kwargs():
    manager = _manager_with_adapter_stubs()
    manager._enqueue_main_chat_mutation = Mock(return_value=True)
    kwargs = {"message_thread_id": 7, "api_kwargs": {"keep": "value"}}

    assert manager.send_chat_action(100, "typing", **kwargs) is True

    assert kwargs == {"message_thread_id": 7, "api_kwargs": {"keep": "value"}}
    queued_kwargs = manager._enqueue_main_chat_mutation.call_args.args[2]
    assert queued_kwargs == {"api_kwargs": {"keep": "value", "message_thread_id": 7}}


def test_main_chat_mutation_binds_positional_and_keyword_chat_ids_and_strips_metadata():
    manager = _manager_with_adapter_stubs()

    def set_chat_title(chat_id, title):
        return True

    manager._bot.set_chat_title = set_chat_title

    manager._enqueue_main_chat_mutation("set_chat_title", (100, "positional"), {})
    manager._enqueue_main_chat_mutation(
        "set_chat_title", (), {"chat_id": 101, "title": "keyword", "_send_mode": "blocking"}
    )

    calls = manager._enqueue_blocking_api_operation.call_args_list
    assert calls[0].kwargs["target_chat_id"] == 100
    assert calls[0].kwargs["kwargs"] == {}
    assert calls[1].kwargs["target_chat_id"] == 101
    assert calls[1].kwargs["kwargs"] == {"chat_id": 101, "title": "keyword"}
    assert all(call.kwargs["required_sender_bot_id"] == "__main__" for call in calls)


def test_callbacks_and_read_operations_remain_direct():
    manager = _manager_with_adapter_stubs()
    manager._bot.answer_callback_query.return_value = True
    manager._bot.get_me.return_value = "bot"

    assert manager.answer_callback_query("query-id") is True
    assert manager.get_me() == "bot"

    manager._enqueue_blocking_api_operation.assert_not_called()
    manager._bot.answer_callback_query.assert_called_once_with("query-id")
    manager._bot.get_me.assert_called_once_with()
