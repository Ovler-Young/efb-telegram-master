from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ehforwarderbot import Message
from ehforwarderbot.types import ChatID
from telegram import Update
from telegram.error import TelegramError

from efb_telegram_master import utils
from efb_telegram_master.ptb_compat import sync_reply_text
from efb_telegram_master.topic_sync import TopicGroupService
from efb_telegram_master.utils import TelegramChatID, TelegramTopicID


@pytest.mark.parametrize(
    ("method_name", "error", "expected_reply", "log_message"),
    [
        (
            "_update_forum_reply",
            RuntimeError("SENTINEL-SECRET"),
            "Error occurred while updating forum group info.",
            "Error occurred while updating forum group info (%s).",
        ),
        (
            "_update_single_group",
            TelegramError("SENTINEL-SECRET"),
            "Error occurred while update chat details.",
            "Error occurred while update chat details (%s).",
        ),
    ],
)
def test_update_info_error_replies_are_bounded_and_log_context(monkeypatch, method_name, error, expected_reply, log_message) -> None:
    logger = Mock()
    bot = Mock()
    channel = Mock(get_chat=Mock(side_effect=error))
    service = TopicGroupService(
        SimpleNamespace(),
        bot,
        Mock(),
        Mock(),
        Mock(),
        "tests.master",
        lambda text: text,
        lambda single, _plural, _count: single,
        logger,
    )
    update = SimpleNamespace(effective_message=SimpleNamespace(message_thread_id=None))
    if method_name == "_update_forum_reply":
        service._update_forum_group_info = Mock(side_effect=error)
        service._update_forum_reply(update, TelegramChatID(1))
    else:
        monkeypatch.setattr("efb_telegram_master.topic_sync.coordinator.slaves", {"slave": channel})
        service._update_single_group(update, SimpleNamespace(id=1), "slave chat")

    reply = bot.reply_error.call_args.args[1]
    assert reply == expected_reply
    assert "SENTINEL-SECRET" not in reply
    assert logger.exception.call_args.args == (log_message, type(error).__name__)


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

    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)
    channel.chat_associations.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    assert channel.chat_associations.get_topic_thread_id(slave_uid, topic_chat_id) == thread_id
    assert channel.chat_associations.get_topic_slaves(topic_chat_id) == [(slave_uid, thread_id)]
    assert channel.chat_associations.get_topic_slave(topic_chat_id, thread_id) == slave_uid

    channel.chat_associations.remove_topic_assoc(topic_chat_id=topic_chat_id, message_thread_id=thread_id)
    assert channel.chat_associations.get_topic_thread_id(slave_uid, topic_chat_id) is None


def test_get_slave_msg_dest_uses_topic_group(channel, slave):
    topic_group = TelegramChatID(30003)
    msg = _build_slave_message(slave)

    with (
        patch.object(channel.bot_manager, "get_chat_info", return_value=SimpleNamespace(is_forum=True)),
        patch.object(channel.topic_sync, "create_topic", return_value=TelegramTopicID(40004)) as create_topic,
        patch.object(channel, "topic_group", topic_group),
    ):
        plan = channel.message_service.router.route(msg)
        tg_dest, thread_id = plan.destination, plan.thread_id

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
    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)

    forum_topic = SimpleNamespace(message_thread_id=TelegramTopicID(60006))
    with patch.object(channel.bot_manager, "create_forum_topic", return_value=forum_topic) as create_forum_topic:
        first = channel.topic_sync.create_topic(slave_uid, topic_chat_id)
        second = channel.topic_sync.create_topic(slave_uid, topic_chat_id)

    assert first == TelegramTopicID(60006)
    assert second == TelegramTopicID(60006)
    assert create_forum_topic.call_count == 1
    assert channel.chat_associations.get_topic_thread_id(slave_uid, topic_chat_id) == TelegramTopicID(60006)

    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)


def test_create_topic_schedules_association_rescan_after_persistence():
    calls = []

    class Associations:
        @contextmanager
        def topic_provisioning_transaction(self):
            calls.append("transaction-enter")
            yield
            calls.append("transaction-exit")

        def get_topic_thread_id(self, **_kwargs):
            return None

        def add_topic_assoc(self, _chat_id, _thread_id, _slave_uid):
            calls.append("association-persisted")

    service = TopicGroupService(
        SimpleNamespace(),
        SimpleNamespace(create_forum_topic=Mock(return_value=SimpleNamespace(message_thread_id=7))),
        Associations(),
        SimpleNamespace(get_chat=Mock(return_value=SimpleNamespace(chat_title="Test chat"))),
        SimpleNamespace(schedule_for_association=lambda _source_chat_id: calls.append("rescan-scheduled")),
        "tests.master",
        lambda text: text,
        lambda single, _plural, _count: single,
        Mock(),
    )

    assert service.create_topic("tests.slave target", TelegramChatID(100)) == TelegramTopicID(7)
    assert calls == ["transaction-enter", "association-persisted", "transaction-exit", "rescan-scheduled"]


def test_chat_migration_preserves_all_associations_and_recreates_forum_topics(channel, slave, bot_group):
    first_slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    second_slave_uid = "tests.slave.second"
    old_chat_id = TelegramChatID(bot_group)
    new_chat_id = TelegramChatID(-100710)
    old_master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(old_chat_id)))
    new_master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(new_chat_id)))
    channel.chat_associations.add_chat_assoc(old_master_uid, first_slave_uid)
    channel.chat_associations.add_chat_assoc(old_master_uid, second_slave_uid, multiple_slave=True)

    with (
        patch.object(channel.bot_manager, "get_chat_info", return_value=SimpleNamespace(is_forum=True)),
        patch.object(channel.topic_sync, "create_topic") as create_topic,
    ):
        channel.topic_sync.migrate_chat_associations(old_chat_id, new_chat_id)

    assert set(channel.chat_associations.get_chat_assoc(master_uid=new_master_uid)) == {first_slave_uid, second_slave_uid}
    assert channel.chat_associations.get_chat_assoc(master_uid=old_master_uid) == []
    assert create_topic.call_args_list == [
        ((first_slave_uid, new_chat_id), {}),
        ((second_slave_uid, new_chat_id), {}),
    ]
    channel.chat_associations.remove_chat_assoc(master_uid=new_master_uid)


def test_master_message_routes_forum_thread_to_slave(channel, slave):
    topic_chat_id = TelegramChatID(70007)
    thread_id = TelegramTopicID(80008)
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)
    channel.chat_associations.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    message = Mock()
    message.chat = SimpleNamespace(id=int(topic_chat_id), is_forum=True)
    message.message_thread_id = int(thread_id)
    message.reply_to_message = Mock(message_id=int(thread_id) + 1, message_thread_id=int(thread_id))
    message.to_dict.return_value = {}

    update = Update(update_id=1, message=message)

    with patch.object(channel.master_message_delivery, "deliver") as deliver:
        channel.master_message_worker.inbound.msg(update, None)

    deliver.assert_called_once()
    args = deliver.call_args.args
    kwargs = deliver.call_args.kwargs
    assert args[2] == slave_uid
    assert kwargs["quote"] is True

    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)


def test_master_message_ignores_forum_topic_auto_reply_without_mutating_message(channel, slave):
    topic_chat_id = TelegramChatID(80009)
    thread_id = TelegramTopicID(80010)
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)
    channel.chat_associations.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    reply_to_topic_starter = Mock(message_id=int(thread_id), message_thread_id=int(thread_id))
    message = _ReadOnlyReplyMessage(reply_to_topic_starter)
    message.chat.id = int(topic_chat_id)

    update = Update(update_id=3, message=message)

    with patch.object(channel.master_message_delivery, "deliver") as deliver:
        channel.master_message_worker.inbound.msg(update, None)

    deliver.assert_called_once()
    kwargs = deliver.call_args.kwargs
    assert kwargs["quote"] is False

    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)


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
    channel.chat_associations.remove_topic_assoc(slave_uid=other_slave_uid)
    channel.chat_associations.add_topic_assoc(topic_chat_id, TelegramTopicID(42), other_slave_uid)

    message = Mock()
    message.chat = SimpleNamespace(id=int(topic_chat_id), is_forum=True)
    message.message_thread_id = int(thread_id)
    message.reply_to_message = None
    message.to_dict.return_value = {}

    update = Update(update_id=2, message=message)

    with patch.object(channel.master_message_delivery, "deliver") as deliver:
        channel.master_message_worker.inbound.msg(update, None)

    deliver.assert_not_called()
    channel.chat_associations.remove_topic_assoc(slave_uid=other_slave_uid)
