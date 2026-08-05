from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import ChatMigrated

from efb_telegram_master.outbound import QueueEnqueueError
from efb_telegram_master.telegram_api import TelegramAPI


class Queue:
    def __init__(self) -> None:
        self.requests = []

    def enqueue_and_wait(self, request):
        self.requests.append(request)
        return SimpleNamespace(message_id=1)


def build_api() -> tuple[TelegramAPI, Queue, SimpleNamespace]:
    channel = SimpleNamespace(chat_binding=SimpleNamespace(chat_migration_by_id=lambda *_args: None), _=lambda text: text)
    queue = Queue()
    bot = SimpleNamespace()
    return TelegramAPI(channel, bot, queue, None), queue, channel


def test_send_message_affixes_content_and_selects_main_for_callback() -> None:
    api, queue, _channel = build_api()

    markup = InlineKeyboardMarkup([[InlineKeyboardButton("Go", callback_data="go")]])
    receipt = api.send_message(42, "message", prefix="before", suffix="after", reply_markup=markup)

    assert receipt.message_id == 1
    assert queue.requests[0].args == (42, "before\nmessage\nafter")
    assert queue.requests[0].required_sender_bot_id == "__main__"


def test_send_message_rejects_boolean_chat_id() -> None:
    api, _queue, _channel = build_api()

    with pytest.raises(QueueEnqueueError, match="non-Boolean integral"):
        api.send_message(True, "message")


def test_chat_migration_retries_with_rewritten_chat_id() -> None:
    api, queue, channel = build_api()
    migrations = []
    channel.chat_binding.chat_migration_by_id = lambda old, new: migrations.append((old, new))
    original = api._route_affixed_operation
    calls = 0

    def route(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ChatMigrated(new_chat_id=-100)
        return original(*args, **kwargs)

    api._route_affixed_operation = route
    api.send_message(42, "message")

    assert migrations == [(42, -100)]
    assert queue.requests[0].telegram_chat_id == -100
