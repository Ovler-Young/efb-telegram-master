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


def test_sync_msglog_requires_admin_and_a_bound_forum_group():
    channel = object.__new__(TelegramChannel)
    channel.config = {"admins": [10]}
    channel.db = SimpleNamespace(get_topic_slaves=Mock(return_value=[("tests.slave", 7)]))
    channel.chat_binding = SimpleNamespace(schedule_msglog_ingestion=Mock(return_value="started"))
    channel.bot_manager = SimpleNamespace(api=SimpleNamespace(send_message=Mock()))
    channel.translator = SimpleNamespace(gettext=lambda text: text)
    message = Mock()
    message.chat = SimpleNamespace(id=100, is_forum=True)
    message.from_user = SimpleNamespace(id=10)
    update = Update(update_id=1, message=message)

    TelegramChannel.sync_msglog(channel, update, SimpleNamespace())
    message.from_user.id = 11
    TelegramChannel.sync_msglog(channel, update, SimpleNamespace())

    channel.chat_binding.schedule_msglog_ingestion.assert_called_once_with(100)


def test_resume_msglog_ingestions_schedules_each_bound_retryable_group():
    manager = object.__new__(ChatBindingManager)
    manager.db = SimpleNamespace(
        get_resumable_msglog_ingestion_scans=Mock(
            return_value=[
                SimpleNamespace(source_chat_id="100"),
                SimpleNamespace(source_chat_id="200"),
            ]
        ),
        get_topic_slaves=Mock(side_effect=[[("a", 1)], [("b", 2)]]),
    )
    manager.schedule_msglog_ingestion = Mock()
    manager.logger = Mock()

    ChatBindingManager.resume_pending_msglog_ingestions(manager)

    assert [call.args for call in manager.schedule_msglog_ingestion.call_args_list] == [(100,), (200,)]


def test_ingested_rows_are_not_remote_get_or_reaction_targets():
    row = SimpleNamespace(provenance="mtproto_ingested")
    chat = SimpleNamespace(module_id="tests.slave", uid="chat")
    channel = object.__new__(TelegramChannel)
    channel.db = SimpleNamespace(get_msg_log=Mock(return_value=row))
    channel.chat_manager = Mock()

    assert TelegramChannel.get_message_by_id(channel, chat, "mtproto-ingested:100.1") is None

    processor = object.__new__(SlaveMessageProcessor)
    processor.db = SimpleNamespace(get_msg_log=Mock(return_value=row))
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
    processor.db = SimpleNamespace(get_msg_log=Mock(return_value=SimpleNamespace(master_msg_id="123.456", provenance=provenance)))
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
    processor.db = SimpleNamespace(add_or_update_message_log=Mock())
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

    processor.db.add_or_update_message_log.assert_called_once_with(
        etm_msg,
        sent,
        None,
        sender_bot_id="7",
    )
    processor._release_pending_slave_message.assert_called_once_with(("slave", "slave-message"))
