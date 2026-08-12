import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from ehforwarderbot.constants import MsgType
from telegram import Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.chat_binding import ChatBindingManager
from efb_telegram_master.slave_message import ETMMsg, SlaveMessageProcessor


def test_sync_msglog_requires_admin_and_a_bound_forum_group():
    channel = object.__new__(TelegramChannel)
    channel.config = {"admins": [10]}
    channel.db = SimpleNamespace(get_topic_slaves=Mock(return_value=[("tests.slave", 7)]))
    channel.chat_binding = SimpleNamespace(schedule_msglog_ingestion=Mock(return_value="started"))
    channel.bot_manager = SimpleNamespace(send_message=Mock())
    channel.translator = SimpleNamespace(gettext=lambda text: text)
    message = Mock()
    message.chat = SimpleNamespace(id=100, is_forum=True)
    message.from_user = SimpleNamespace(id=10)
    update = Update(update_id=1, message=message)

    TelegramChannel.sync_msglog(channel, update, SimpleNamespace())
    message.from_user.id = 11
    TelegramChannel.sync_msglog(channel, update, SimpleNamespace())

    channel.chat_binding.schedule_msglog_ingestion.assert_called_once_with(100)


def test_stop_msglog_ingestions_joins_active_workers():
    manager = object.__new__(ChatBindingManager)
    manager._msglog_ingestion_lock = threading.Lock()
    manager._msglog_ingestion_stop = threading.Event()
    worker = threading.Thread(target=manager._msglog_ingestion_stop.wait)
    manager._msglog_ingestion_threads = {100: worker}
    worker.start()

    assert ChatBindingManager.stop_msglog_ingestions(manager)

    assert manager._msglog_ingestion_stop.is_set()
    assert not worker.is_alive()


def test_channel_shutdown_defers_database_close_until_a_nonresponsive_ingestion_worker_exits():
    manager = object.__new__(ChatBindingManager)
    manager._msglog_ingestion_lock = threading.Lock()
    manager._msglog_ingestion_stop = threading.Event()
    request_released = threading.Event()
    manager.MSGLOG_INGESTION_JOIN_TIMEOUT = 0.01
    manager.logger = Mock()
    def block_runtime(coroutine):
        coroutine.close()
        request_released.wait()

    manager.bot = SimpleNamespace(_runtime=SimpleNamespace(call=block_runtime))
    manager.channel = SimpleNamespace(mtproto=SimpleNamespace())
    manager.db = SimpleNamespace()
    worker = threading.Thread(target=manager._run_msglog_ingestion, args=(100,), daemon=True)
    manager._msglog_ingestion_threads = {100: worker}
    worker.start()

    async def disconnect() -> None:
        return None

    mtproto = SimpleNamespace(disconnect=Mock(side_effect=disconnect))
    channel = object.__new__(TelegramChannel)
    channel.logger = Mock()
    channel.rpc_utilities = SimpleNamespace(shutdown=Mock())
    channel.chat_binding = manager
    channel.bot_manager = SimpleNamespace(stop_channel_resources=Mock())
    channel.telegram_runtime = SimpleNamespace(stop=Mock(side_effect=lambda: asyncio.run(TelegramChannel._telegram_runtime_stopped(channel, SimpleNamespace()))))
    channel.master_messages = SimpleNamespace(stop_worker=Mock())
    channel.db = SimpleNamespace(stop_worker=Mock())
    channel.mtproto = mtproto

    completed = threading.Event()
    shutdown = threading.Thread(target=lambda: (TelegramChannel.stop_polling(channel), completed.set()), daemon=True)
    started = time.monotonic()
    shutdown.start()

    assert completed.wait(timeout=1)
    shutdown.join(timeout=1)
    assert time.monotonic() - started < 1
    assert manager._msglog_ingestion_stop.is_set()
    assert worker.is_alive()
    mtproto.disconnect.assert_called_once()
    channel.db.stop_worker.assert_not_called()

    request_released.set()
    worker.join(timeout=1)
    assert not worker.is_alive()
    channel.db.stop_worker.assert_called_once()


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


def test_pending_slave_message_is_dispatched_once(monkeypatch):
    processor = object.__new__(SlaveMessageProcessor)
    processor.logger = Mock()
    processor._pending_slave_messages = set()
    processor._pending_slave_messages_lock = threading.Lock()
    processor.get_slave_msg_dest = Mock(return_value=("", (123, None)))
    processor.is_silent = Mock(return_value=False)
    processor.dispatch_message = Mock()
    message = SimpleNamespace(edit=False, uid="duplicate", type=MsgType.Text, chat=SimpleNamespace())
    monkeypatch.setattr("efb_telegram_master.slave_message.utils.chat_id_to_str", lambda **_kwargs: "slave")

    processor.send_message(message)
    processor.send_message(message)

    processor.dispatch_message.assert_called_once_with(message, "", None, 123, None, False, dedupe_key=("slave", "duplicate"))
