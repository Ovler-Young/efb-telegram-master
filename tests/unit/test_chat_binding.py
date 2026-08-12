from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from telegram import CallbackQuery, Chat, Message, Update, User

from efb_telegram_master import utils
from efb_telegram_master.chat_binding import ChatBindingManager, ChatListStorage
from efb_telegram_master.constants import Flags
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID


def _callback_update(user_id, chat_id, message_id, data):
    user = User(user_id, "test", False)
    message = Message(message_id, datetime.now(), Chat(chat_id, "group"), from_user=user)
    callback = CallbackQuery("query", user, "instance", message=message, data=data)
    return Update(1, callback_query=callback)


def test_chat_binding_callbacks_reject_other_users_without_clearing_the_session():
    manager = object.__new__(ChatBindingManager)
    manager.bot = Mock()
    manager.channel = SimpleNamespace(_=lambda text: text)
    storage_id = (TelegramChatID(-100), TelegramMessageID(10))
    manager.msg_storage = {storage_id: ChatListStorage([], owner_id=10)}

    for handler, data, expected_state in (
        (manager.link_chat_confirm, "chat 0", Flags.LINK_CONFIRM),
        (manager.make_chat_head, "chat 0", Flags.CHAT_HEAD_CONFIRM),
        (manager.suggested_recipient, "chat 0", Flags.SUGGEST_RECIPIENTS),
    ):
        assert handler(_callback_update(20, -100, 10, data), SimpleNamespace()) == expected_state
        assert storage_id in manager.msg_storage

    assert manager.bot.answer_callback_query.call_count == 3


def test_full_chat_pagination(channel, slave):
    storage_id = (TelegramChatID(0), TelegramMessageID(1))
    legends, buttons = channel.chat_binding.slave_chats_pagination(storage_id)
    legend = "\n".join(legends)
    assert slave.channel_emoji in legend
    assert slave.channel_name in legend
    assert min(channel.flag("chats_per_page"), len(slave.get_chats())) == len(buttons) - 1


def test_source_chat_pagination(channel, slave):
    storage_id = (TelegramChatID(0), TelegramMessageID(3))
    source_chats = [utils.chat_id_to_str(chat=slave.group)]
    legends, buttons = channel.chat_binding.slave_chats_pagination(storage_id, source_chats=source_chats)
    legend = "\n".join(legends)
    assert slave.channel_emoji in legend
    assert slave.channel_name in legend
    assert len(buttons) == 2


def test_chat_pagination_filters_groups_users_and_invalid_regex(channel, slave):
    _, buttons = channel.chat_binding.slave_chats_pagination((TelegramChatID(0), TelegramMessageID(4)), pattern="wonderland")
    names = [button.text for row in buttons[:-1] for button in row]
    assert names and all("Wonderland" in name for name in names)

    for message_id, (pattern, chat_type) in enumerate((("type: group", "GroupChat"), ("type: private", "PrivateChat")), start=5):
        _, buttons = channel.chat_binding.slave_chats_pagination((TelegramChatID(0), TelegramMessageID(message_id)), pattern=pattern)
        names = [button.text for row in buttons[:-1] for button in row]
        expected = slave.get_chats_by_criteria(chat_type=chat_type)
        assert names and all(any(chat.display_name in name for chat in expected) for name in names)

    _, buttons = channel.chat_binding.slave_chats_pagination((TelegramChatID(0), TelegramMessageID(7)), pattern="(")
    assert len(buttons) == 1


def test_truncate_ellipsis(channel):
    truncate_ellipsis = channel.chat_binding.truncate_ellipsis
    short_text = "short text"
    long_text = "This is a long text. Cursus pellentesque cras maecenas hac malesuada porttitor nullam, dignissim enim feugiat placerat eget quisque, dui sem dictum fames sapien mauris. Feugiat euismod nisi donec nunc cras aliquam diam, arcu fames pretium pellentesque faucibus phasellus, in montes felis elit lacinia auctor. Commodo curae nibh donec vel ipsum sociosqu maecenas pellentesque scelerisque suspendisse blandit himenaeos rutrum ad, nec dictum porttitor non luctus fringilla feugiat volutpat adipiscing cubilia vitae lacus. Tempor iaculis facilisis maecenas quam nisl pulvinar magnis lacus, sodales porta quisque rutrum habitasse metus purus ante libero, malesuada mollis est donec cubilia accumsan parturient. Parturient libero gravida imperdiet massa praesent habitant scelerisque pellentesque mollis elit, urna quisque tellus in nostra aliquet montes natoque fermentum, condimentum enim magna odio vestibulum mauris viverra sagittis iaculis."
    assert truncate_ellipsis(short_text, len(short_text) + 10) == short_text
    truncated = truncate_ellipsis(long_text, 256)
    assert len(truncated) <= 256
    assert truncated.endswith("…")


# All other methods are to be tested with integration testing.
