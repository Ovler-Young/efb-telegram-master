import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master.delivery.slave_delivery_helpers import send_identity
from efb_telegram_master.delivery.slave_routing import SlaveMessageRouter


def test_forum_destination_uses_cached_chat_info_until_ttl() -> None:
    processor = object.__new__(SlaveMessageRouter)
    chat_uid = "tests.slave chat"
    tg_chat = "telegram -100123"

    def get_chat_assoc(*, slave_uid=None, master_uid=None):
        return [tg_chat] if slave_uid == chat_uid else [chat_uid] if master_uid == tg_chat else []

    processor.admins = [1]
    processor.topic_group = -100999
    processor.topic_sync = SimpleNamespace(create_topic=Mock(return_value=55))
    processor.bot = SimpleNamespace(get_chat_info=Mock(return_value=SimpleNamespace(is_forum=True)))
    processor.db = SimpleNamespace()
    processor.chat_associations = SimpleNamespace(get_chat_assoc=Mock(side_effect=get_chat_assoc), get_topic_thread_id=Mock(return_value=55))
    processor.chat_manager = SimpleNamespace(update_chat_obj=lambda chat: chat, get_or_enrol_member=lambda chat, author: author)
    processor.chat_dest_cache = SimpleNamespace(get=Mock(return_value=chat_uid), remove=Mock())
    processor.generate_message_template = Mock(return_value="template")
    processor.logger = Mock()
    processor._known_forum_chat_ids = {}
    processor._known_forum_chat_ids_lock = threading.Lock()

    first = SimpleNamespace(uid="one", chat=SimpleNamespace(module_id="tests.slave", uid="chat"), author=SimpleNamespace())
    second = SimpleNamespace(uid="two", chat=SimpleNamespace(module_id="tests.slave", uid="chat"), author=SimpleNamespace())
    assert processor.route(first).destination == -100123
    assert processor.route(second).thread_id == 55
    processor.bot.get_chat_info.assert_called_once_with(-100123)

    processor._known_forum_chat_ids[-100123] = time.monotonic() - processor.FORUM_CHAT_CACHE_TTL - 1
    assert processor.route(second).thread_id == 55
    assert processor.bot.get_chat_info.call_count == 2


def test_send_kwargs_preserve_slave_routing_identity() -> None:
    message = SimpleNamespace(chat=SimpleNamespace(module_id="tests.slave", uid="chat"))
    assert send_identity(message) == {"_slave_id": "tests.slave chat"}
