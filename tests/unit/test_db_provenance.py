import pickle
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ehforwarderbot import MsgType
from ehforwarderbot.types import MessageID
from peewee import SqliteDatabase
from prometheus_client import generate_latest

from efb_telegram_master import utils
from efb_telegram_master.etm_metrics import Metrics
from efb_telegram_master.message import ETMMsg
from efb_telegram_master.models import MsgLog, TopicAssoc
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.outbound_types import SendReceipt
from efb_telegram_master.persistence.msglog_repository import MsgLogRepository
from efb_telegram_master.slave_message import SlaveMessageService
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID, TelegramTopicID


def test_database_method_metrics_record_bounded_public_operation_labels(channel):
    metrics = Metrics()
    channel.db.set_metrics(metrics)

    assert channel.chat_associations.get_chat_assoc(master_uid="metrics-master") == []
    with pytest.raises(ValueError, match="Only one parameter"):
        channel.msglogs.get_msg_log()
    assert channel.history_migrations.get_entries_page("metrics-slave", 12345, None, None, 1) == []
    assert channel.msglogs.get_recent_message_page("metrics-slave", None, 1) == []

    rendered = generate_latest(metrics.registry).decode()

    assert 'etm_database_method_duration_seconds_count{method="get_chat_assoc"} 1.0' in rendered
    assert 'etm_database_method_failures_total{method="get_msg_log"} 1.0' in rendered
    assert 'etm_database_method_duration_seconds_count{method="get_history_migration_entry_page"} 1.0' in rendered
    assert 'etm_database_method_duration_seconds_count{method="get_recent_msglog_page"} 1.0' in rendered
    assert "metrics-master" not in rendered


def test_topic_assoc_table_exists_and_round_trips(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(11111)
    thread_id = TelegramTopicID(22222)

    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)
    assoc = channel.chat_associations.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    assert isinstance(assoc, TopicAssoc)
    assert channel.chat_associations.get_topic_thread_id(slave_uid, topic_chat_id) == thread_id
    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)


def test_topic_assoc_is_replaced_for_same_slave_and_thread(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(33333)

    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)
    channel.chat_associations.add_topic_assoc(topic_chat_id, TelegramTopicID(44444), slave_uid)
    channel.chat_associations.add_topic_assoc(topic_chat_id, TelegramTopicID(55555), slave_uid)

    assert channel.chat_associations.get_topic_thread_id(slave_uid, topic_chat_id) == TelegramTopicID(55555)
    assert TopicAssoc.select().where(TopicAssoc.slave_uid == slave_uid).count() == 1
    channel.chat_associations.remove_topic_assoc(slave_uid=slave_uid)


def test_remove_chat_assoc_removes_topic_assoc(channel):
    channel.chat_associations.add_chat_assoc("master-topic-cleanup", "slave-topic-cleanup", multiple_slave=True)
    channel.chat_associations.add_topic_assoc(TelegramChatID(66666), TelegramTopicID(77777), "slave-topic-cleanup")

    channel.chat_associations.remove_chat_assoc(master_uid="master-topic-cleanup")

    assert channel.chat_associations.get_topic_thread_id("slave-topic-cleanup", TelegramChatID(66666)) is None


def test_slave_delivery_receipt_persists_sender_bot_id():
    test_db = SqliteDatabase(":memory:")
    repository = MsgLogRepository()
    author = SimpleNamespace(module_id="tests.slave", uid="author")
    chat = SimpleNamespace(module_id="tests.slave", uid="chat", get_member=lambda _uid: author)
    message = SimpleNamespace(
        uid=MessageID("receipt-provenance-message"),
        chat=chat,
        author=author,
        text="receipt provenance",
        type=MsgType.Text,
        target=None,
        commands=None,
        reactions={},
        is_system=False,
        attributes=None,
        substitutions=None,
        deliver_to=SimpleNamespace(channel_id="tests.master"),
    )
    receipt = SendReceipt(
        SimpleNamespace(chat=SimpleNamespace(id=123456), chat_id=123456, message_id=654321),
        sender_bot_id="777",
    )
    processor = object.__new__(SlaveMessageService)
    processor.logger = SimpleNamespace(debug=lambda *_args: None, warning=lambda *_args: None)
    processor.msglogs = repository
    processor.chat_manager = SimpleNamespace(update_chat_obj=lambda value: value)
    processor.router = SimpleNamespace(resolve_reply=lambda *_args: None)
    processor.commands = SimpleNamespace(register_command=lambda *_args: None)
    processor.text_delivery = SimpleNamespace(text=lambda *_args: receipt)

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        processor.dispatch_message(message, "", None, 123456, None)

        stored = repository.get_msg_log(master_msg_id="123456.654321")
        assert stored is not None and stored.sender_bot_id == "777"


def test_message_reconstructor_restores_sender_bot_id(channel, slave):
    chat = slave.chat_with_alias
    etm_msg = ETMMsg(
        uid=MessageID("restored-message"),
        chat=channel.chat_manager.update_chat_obj(chat),
        author=channel.chat_manager.get_or_enrol_member(chat, chat.other),
        text="restored",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=channel,
    )

    channel.msglogs.add_or_update_message_log(etm_msg, SimpleNamespace(chat_id=4444, message_id=5555), sender_bot_id="888")
    row = channel.msglogs.get_msg_log(master_msg_id="4444.5555")
    assert row is not None and channel.message_reconstructor.build(row).sender_bot_id == "888"
    row.delete_instance()


def _reaction_message(reactor, reactions):
    return SimpleNamespace(
        uid=MessageID("reaction-message"),
        chat=SimpleNamespace(module_id="tests.mocks.slave", uid="chat"),
        author=SimpleNamespace(module_id="tests.mocks.slave", uid="author"),
        text="message",
        type=MsgType.Text,
        type_telegram=TGMsgType.Text,
        deliver_to=SimpleNamespace(channel_id="tests.mocks.slave"),
        file_id=None,
        file_unique_id=None,
        mime=None,
        is_system=False,
        attributes=None,
        commands=None,
        substitutions=None,
        target=None,
        sender_bot_id=None,
        reactions=reactions,
    )


def test_reaction_alternate_updates_one_canonical_row_and_clears_retraction():
    test_db = SqliteDatabase(":memory:")
    manager = MsgLogRepository()
    reactor = SimpleNamespace(module_id="tests.mocks.slave", uid="reactor")
    message = _reaction_message(reactor, {})

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=10))
        message.reactions = {"R0": [reactor]}
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=11), old_message_id=(TelegramChatID(100), TelegramMessageID(10)), sender_bot_id="777")
        row = MsgLog.get()
        assert (MsgLog.select().count(), row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == (1, "100.10", "100.11", "777")
        assert pickle.loads(bytes(row.pickle))["reactions"] == {"R0": ("tests.mocks.slave reactor",)}

        message.reactions = {}
        manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=11), old_message_id=(TelegramChatID(100), TelegramMessageID(11)), sender_bot_id="777")
        row = MsgLog.get()
        assert (MsgLog.select().count(), row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id, row.pickle) == (1, "100.10", "100.11", "777", None)


def test_reaction_alternate_db_failures_preserve_then_update_canonical_row():
    test_db = SqliteDatabase(":memory:")
    manager = MsgLogRepository()
    reactor = SimpleNamespace(module_id="tests.mocks.slave", uid="reactor")
    message = _reaction_message(reactor, {"NEW": [reactor]})

    with test_db.bind_ctx([MsgLog]):
        test_db.create_tables([MsgLog])
        for initial_alt, old_id, failed_id, success_id in ((None, 10, 11, 12), ("100.11", 11, 12, 13)):
            MsgLog.delete().execute()
            MsgLog.create(
                master_msg_id="100.10",
                master_msg_id_alt=initial_alt,
                slave_message_id="reaction-message",
                text="old",
                slave_origin_uid="tests.mocks.slave chat",
                slave_member_uid="tests.mocks.slave author",
                msg_type=MsgType.Text.name,
                sent_to="tests.mocks.slave",
                sender_bot_id="700",
                pickle=pickle.dumps({"reactions": {"OLD": ("tests.mocks.slave reactor",)}}),
            )
            with patch.object(MsgLog, "insert", wraps=MsgLog.insert) as insert:
                with patch("peewee.ModelInsert.execute", side_effect=RuntimeError("db failed")) as execute:
                    with pytest.raises(RuntimeError, match="db failed"):
                        manager.add_or_update_message_log(
                            message, SimpleNamespace(chat_id=100, message_id=failed_id), old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="800"
                        )
            assert insert.call_count == 1
            assert execute.call_count == 1
            row = MsgLog.get()
            assert (row.master_msg_id_alt, row.sender_bot_id, pickle.loads(bytes(row.pickle))["reactions"]) == (initial_alt, "700", {"OLD": ("tests.mocks.slave reactor",)})

            manager.add_or_update_message_log(message, SimpleNamespace(chat_id=100, message_id=success_id), old_message_id=(TelegramChatID(100), TelegramMessageID(old_id)), sender_bot_id="900")
            row = MsgLog.get()
            assert (MsgLog.select().count(), row.master_msg_id, row.master_msg_id_alt, row.sender_bot_id) == (1, "100.10", f"100.{success_id}", "900")
