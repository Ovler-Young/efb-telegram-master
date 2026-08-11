from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot import Message
from ehforwarderbot.constants import MsgType
from ehforwarderbot.types import MessageID
from telegram import Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.chat_binding import ChatBindingManager
from efb_telegram_master.slave_message import ETMMsg, SlaveMessageProcessor


@pytest.fixture
def sync_msglog_channel():
    channel = object.__new__(TelegramChannel)
    channel.config = {"admins": [10]}
    channel.chat_associations = SimpleNamespace(get_topic_slaves=Mock(return_value=[("tests.slave", 7)]))
    channel.chat_binding = SimpleNamespace(schedule_msglog_ingestion=Mock(return_value="started"))
    channel.bot_manager = SimpleNamespace(api=SimpleNamespace(send_message=Mock()))
    channel.translator = SimpleNamespace(gettext=lambda text: text)
    return channel


def sync_msglog_update(*, user_id=10, is_forum=True):
    message = Mock()
    message.chat = SimpleNamespace(id=100, is_forum=is_forum)
    message.from_user = SimpleNamespace(id=user_id)
    message.message_thread_id = None
    return Update(update_id=1, message=message)


def test_sync_msglog_schedules_for_admin_in_bound_forum_group(sync_msglog_channel):
    channel = sync_msglog_channel

    TelegramChannel.sync_msglog(channel, sync_msglog_update(), SimpleNamespace())

    channel.chat_associations.get_topic_slaves.assert_called_once_with(100)
    channel.chat_binding.schedule_msglog_ingestion.assert_called_once_with(100)
    channel.bot_manager.api.send_message.assert_called_once_with(100, text="MsgLog sync started for this group.")


@pytest.mark.parametrize(
    ("user_id", "is_forum", "bound_topics", "expected_reply", "topic_lookup_expected"),
    [
        (11, True, [("tests.slave", 7)], "This command is for ETM admins only.", False),
        (10, False, [("tests.slave", 7)], "This command must be used in a bound forum group.", False),
        (10, True, [], "This forum group has no bound topics.", True),
    ],
    ids=["non-admin", "non-forum", "no-bound-topics"],
)
def test_sync_msglog_rejects_unqualified_requests(sync_msglog_channel, user_id, is_forum, bound_topics, expected_reply, topic_lookup_expected):
    channel = sync_msglog_channel
    channel.chat_associations.get_topic_slaves.return_value = bound_topics

    TelegramChannel.sync_msglog(channel, sync_msglog_update(user_id=user_id, is_forum=is_forum), SimpleNamespace())

    if topic_lookup_expected:
        channel.chat_associations.get_topic_slaves.assert_called_once_with(100)
    else:
        channel.chat_associations.get_topic_slaves.assert_not_called()
    channel.chat_binding.schedule_msglog_ingestion.assert_not_called()
    channel.bot_manager.api.send_message.assert_called_once_with(100, text=expected_reply)


def test_sync_msglog_ignores_updates_without_an_effective_message(sync_msglog_channel):
    channel = sync_msglog_channel

    TelegramChannel.sync_msglog(channel, Update(update_id=1), SimpleNamespace())

    channel.chat_associations.get_topic_slaves.assert_not_called()
    channel.chat_binding.schedule_msglog_ingestion.assert_not_called()
    channel.bot_manager.api.send_message.assert_not_called()


def test_resume_msglog_ingestions_schedules_each_bound_retryable_group():
    manager = object.__new__(ChatBindingManager)
    manager.msglog_ingestion = SimpleNamespace(
        get_resumable_scans=Mock(
            return_value=[
                SimpleNamespace(source_chat_id="100"),
                SimpleNamespace(source_chat_id="200"),
            ]
        )
    )
    manager.chat_associations = SimpleNamespace(get_topic_slaves=Mock(side_effect=[[("a", 1)], [("b", 2)]]))
    manager.schedule_msglog_ingestion = Mock()
    manager.logger = Mock()

    ChatBindingManager.resume_pending_msglog_ingestions(manager)

    assert [call.args for call in manager.schedule_msglog_ingestion.call_args_list] == [(100,), (200,)]


def test_ingested_rows_are_not_remote_get_or_reaction_targets():
    row = SimpleNamespace(provenance="mtproto_ingested")
    chat = SimpleNamespace(module_id="tests.slave", uid="chat")
    channel = object.__new__(TelegramChannel)
    channel.msglogs = SimpleNamespace(get_msg_log=Mock(return_value=row))
    channel.chat_manager = Mock()

    assert TelegramChannel.get_message_by_id(channel, chat, "mtproto-ingested:100.1") is None

    processor = object.__new__(SlaveMessageProcessor)
    processor.msglogs = SimpleNamespace(get_msg_log=Mock(return_value=row))
    processor.logger = Mock()
    processor.update_reactions(SimpleNamespace(chat=chat, msg_id="mtproto-ingested:100.1", reactions={}))

    processor.logger.info.assert_called_once()


@pytest.mark.parametrize(
    ("provenance", "expected_target_msg_id"),
    [("mtproto_ingested", None), ("live", 456)],
)
def test_dispatch_reply_target_respects_provenance(provenance, expected_target_msg_id):
    processor = object.__new__(SlaveMessageProcessor)
    processor.logger = Mock()
    processor.msglogs = SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(master_msg_id="123.456", provenance=provenance)))
    processor.chat_manager = Mock()
    processor.channel = SimpleNamespace(commands=SimpleNamespace(register_command=Mock()))
    processor.slave_message_text = Mock(return_value=None)
    processor._release_pending_slave_message = Mock()
    target = Message(uid=MessageID("recovered"), chat=SimpleNamespace(module_id="tests.slave", uid="chat"))
    message = Message(uid=MessageID("reply"), chat=SimpleNamespace(module_id="tests.slave", uid="chat"), target=target, text="reply", type=MsgType.Text)

    processor.dispatch_message(message, "", None, 123, None)

    assert processor.slave_message_text.call_args.args[6] == expected_target_msg_id


def test_ordinary_send_writes_msglog_once_and_releases_completion(monkeypatch):
    processor = object.__new__(SlaveMessageProcessor)
    processor.logger = Mock()
    processor.msglogs = SimpleNamespace(add_or_update_message_log=Mock())
    processor.chat_manager = Mock()
    processor.channel = SimpleNamespace(commands=SimpleNamespace(register_command=Mock()))
    processor.build_reactions_footer = Mock(return_value="")
    processor._release_pending_slave_message = Mock()
    sent = SimpleNamespace(chat=SimpleNamespace(id=123), message_id=456, sender_bot_id="7")
    processor.slave_message_text = Mock(return_value=sent)
    etm_msg = Mock()
    monkeypatch.setattr(ETMMsg, "from_efbmsg", Mock(return_value=etm_msg))
    monkeypatch.setattr("efb_telegram_master.slave_message.get_msg_type", Mock(return_value="Text"))
    message = SimpleNamespace(
        uid="slave-message",
        target=None,
        commands=[],
        reactions={},
        text="hello",
        type=MsgType.Text,
    )

    processor.dispatch_message(message, "", None, 123, None, dedupe_key=("slave", "slave-message"))

    processor.msglogs.add_or_update_message_log.assert_called_once_with(
        etm_msg,
        sent,
        None,
        sender_bot_id="7",
    )
    processor._release_pending_slave_message.assert_called_once_with(("slave", "slave-message"))
