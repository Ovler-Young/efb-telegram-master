from types import SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot import Message
from telegram import Update

from efb_telegram_master import utils
from efb_telegram_master.ptb_compat import sync_reply_text
from efb_telegram_master.utils import TelegramChatID, TelegramTopicID


class _ReadOnlyReplyMessage:
    def __init__(self, reply_to_message):
        self.chat = SimpleNamespace(id=0, is_forum=True)
        self.message_id = reply_to_message.message_thread_id + 1
        self.message_thread_id = reply_to_message.message_thread_id
        self._reply_to_message = reply_to_message
        self.to_dict = Mock(return_value={})

    @property
    def reply_to_message(self):
        return self._reply_to_message


def _build_slave_message(slave, chat=None, author=None):
    chat = chat or slave.chat_with_alias
    author = author or chat.self
    msg = Message()
    msg.uid = "topic-group-test"
    msg.chat = chat
    msg.author = author
    msg.text = "topic group text"
    return msg


def test_topic_assoc_crud(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(10001)
    thread_id = TelegramTopicID(20002)

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) == thread_id
    assert channel.db.get_topic_slaves(topic_chat_id) == [(slave_uid, thread_id)]
    assert channel.db.get_topic_slave(topic_chat_id, thread_id) == slave_uid

    channel.db.remove_topic_assoc(topic_chat_id=topic_chat_id, message_thread_id=thread_id)
    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) is None


def test_get_slave_msg_dest_uses_topic_group(channel, slave):
    topic_group = TelegramChatID(30003)
    msg = _build_slave_message(slave)

    with (
        patch.object(channel.bot_manager, "get_chat_info", return_value=SimpleNamespace(is_forum=True)),
        patch.object(channel.chat_binding, "create_topic", return_value=TelegramTopicID(40004)) as create_topic,
        patch.object(channel, "topic_group", topic_group),
    ):
        _, (tg_dest, thread_id) = channel.slave_messages.get_slave_msg_dest(msg)

    assert tg_dest == topic_group
    assert thread_id == TelegramTopicID(40004)
    create_topic.assert_called_once_with(
        slave_uid=utils.chat_id_to_str(chat=slave.chat_with_alias),
        telegram_chat_id=topic_group,
    )


def test_flag_manager_reads_top_level_topic_group(channel):
    channel.config["topic_group"] = 34567
    channel.config["flags"] = {}

    flag = utils.ExperimentalFlagsManager(channel)

    assert flag("topic_group") == 34567


def test_create_topic_creates_once_and_reuses_cached_assoc(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(50005)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)

    forum_topic = SimpleNamespace(message_thread_id=TelegramTopicID(60006))
    with patch.object(channel.bot_manager, "create_forum_topic", return_value=forum_topic) as create_forum_topic:
        first = channel.chat_binding.create_topic(slave_uid, topic_chat_id)
        second = channel.chat_binding.create_topic(slave_uid, topic_chat_id)

    assert first == TelegramTopicID(60006)
    assert second == TelegramTopicID(60006)
    assert create_forum_topic.call_count == 1
    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) == TelegramTopicID(60006)

    channel.db.remove_topic_assoc(slave_uid=slave_uid)


def test_master_message_routes_forum_thread_to_slave(channel, slave):
    topic_chat_id = TelegramChatID(70007)
    thread_id = TelegramTopicID(80008)
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    message = Mock()
    message.chat = SimpleNamespace(id=int(topic_chat_id), is_forum=True)
    message.message_thread_id = int(thread_id)
    message.reply_to_message = Mock(message_id=int(thread_id) + 1, message_thread_id=int(thread_id))
    message.to_dict.return_value = {}

    update = Update(update_id=1, message=message)

    with patch.object(channel.master_messages, "process_telegram_message") as process_telegram_message:
        channel.master_messages.msg(update, None)

    process_telegram_message.assert_called_once()
    args = process_telegram_message.call_args.args
    kwargs = process_telegram_message.call_args.kwargs
    assert args[2] == slave_uid
    assert kwargs["quote"] is True

    channel.db.remove_topic_assoc(slave_uid=slave_uid)


def test_master_message_ignores_forum_topic_auto_reply_without_mutating_message(channel, slave):
    topic_chat_id = TelegramChatID(80009)
    thread_id = TelegramTopicID(80010)
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    reply_to_topic_starter = Mock(message_id=int(thread_id), message_thread_id=int(thread_id))
    message = _ReadOnlyReplyMessage(reply_to_topic_starter)
    message.chat.id = int(topic_chat_id)

    update = Update(update_id=3, message=message)

    with patch.object(channel.master_messages, "process_telegram_message") as process_telegram_message:
        channel.master_messages.msg(update, None)

    process_telegram_message.assert_called_once()
    kwargs = process_telegram_message.call_args.kwargs
    assert kwargs["quote"] is False

    channel.db.remove_topic_assoc(slave_uid=slave_uid)


def test_sync_reply_text_keeps_forum_topic_thread():
    bot = Mock()
    bot.send_message.return_value = Mock(message_id=2)
    message = Mock()
    message.chat.id = -100123
    message.message_id = 1
    message.message_thread_id = 456

    sync_reply_text(bot, message, "Processing...")

    bot.send_message.assert_called_once_with(-100123, text="Processing...", message_thread_id=456)


def test_master_message_ignores_unknown_forum_thread(channel, slave):
    topic_chat_id = TelegramChatID(90009)
    thread_id = TelegramTopicID(90010)
    other_slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    channel.db.remove_topic_assoc(slave_uid=other_slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, TelegramTopicID(42), other_slave_uid)

    message = Mock()
    message.chat = SimpleNamespace(id=int(topic_chat_id), is_forum=True)
    message.message_thread_id = int(thread_id)
    message.reply_to_message = None
    message.to_dict.return_value = {}

    update = Update(update_id=2, message=message)

    with patch.object(channel.master_messages, "process_telegram_message") as process_telegram_message:
        channel.master_messages.msg(update, None)

    process_telegram_message.assert_not_called()
    channel.db.remove_topic_assoc(slave_uid=other_slave_uid)
