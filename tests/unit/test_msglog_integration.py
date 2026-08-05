import asyncio
import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call
from threading import Lock
from uuid import UUID

import pytest
from telegram import Update
from efb_telegram_master import TelegramChannel
from efb_telegram_master.chat_binding import ChatBindingManager
import efb_telegram_master.master_message as master_message
from efb_telegram_master.master_message import MasterMessageProcessor
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.slave_message import SlaveMessageProcessor
from tests.integration import conftest as integration_conftest
from tests.integration.test_mtproto_live import (
    _delete_msg_logs_by_master_ids,
    _wait_for_ingestion_worker_exit,
)


def test_live_msglog_sync_keeps_polling_running():
    path = Path(__file__).parents[1] / "integration" / "test_mtproto_live.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    test = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "test_sync_msglog_ingests_unlogged_topic_messages_live"
    )

    fixture_names = {argument.arg for argument in test.args.args}
    lifecycle_calls = [
        node for node in ast.walk(test)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in {"poll_bot", "poll_bot_factory"}
        and node.func.attr in {"start", "stop"}
    ]

    assert "poll_bot" in fixture_names
    assert "poll_bot_factory" not in fixture_names
    assert lifecycle_calls == []


def test_live_msglog_gap_deletion_is_limited_to_exact_master_message_ids():
    channel = SimpleNamespace(db=SimpleNamespace(delete_msg_log=Mock()))
    master_msg_ids = ("-100123.41", "-100123.42")

    _delete_msg_logs_by_master_ids(channel, master_msg_ids)

    assert channel.db.delete_msg_log.call_args_list == [
        call(master_msg_id="-100123.41"),
        call(master_msg_id="-100123.42"),
    ]


@pytest.mark.asyncio
async def test_live_msglog_cleanup_joins_registered_worker_with_bounded_timeout():
    events = []

    class Worker:
        alive = True

        def join(self, timeout):
            events.append(("join", timeout))
            self.alive = False

        def is_alive(self):
            return self.alive

    worker = Worker()
    channel = SimpleNamespace(chat_binding=SimpleNamespace(
        _msglog_ingestion_lock=Lock(),
        _msglog_ingestion_threads={-100123: worker},
    ))

    await _wait_for_ingestion_worker_exit(channel, -100123, timeout=7)

    assert events == [("join", 7)]
    assert not worker.is_alive()


def test_live_msglog_cleanup_waits_in_finally_before_deleting_scan():
    path = Path(__file__).parents[1] / "integration" / "test_mtproto_live.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    test = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "test_sync_msglog_ingests_unlogged_topic_messages_live"
    )
    cleanup = next(node for node in test.body if isinstance(node, ast.Try))
    wait = next(
        node for node in ast.walk(ast.Module(body=cleanup.finalbody, type_ignores=[]))
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_wait_for_ingestion_worker_exit"
    )
    deletion = next(
        node for node in ast.walk(ast.Module(body=cleanup.finalbody, type_ignores=[]))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
    )

    assert wait.lineno < deletion.lineno


def test_integration_postgres_config_reads_required_environment(monkeypatch):
    values = {
        "TEST_POSTGRES_HOST": "postgres.example",
        "TEST_POSTGRES_PORT": "5433",
        "TEST_POSTGRES_DB": "etm_test",
        "TEST_POSTGRES_USER": "etm",
        "TEST_POSTGRES_PASSWORD": "secret",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    config = integration_conftest.integration_postgres_config.__wrapped__()

    assert config == {
        "type": "postgresql",
        "host": "postgres.example",
        "port": 5433,
        "database": "etm_test",
        "user": "etm",
        "password": "secret",
    }


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

    channel.chat_binding.schedule_msglog_ingestion.assert_called_once_with(100)
    channel.bot_manager.send_message.assert_called_once()

    message.from_user.id = 11
    TelegramChannel.sync_msglog(channel, update, SimpleNamespace())
    assert channel.chat_binding.schedule_msglog_ingestion.call_count == 1


def test_resume_msglog_ingestions_schedules_each_bound_retryable_group():
    manager = object.__new__(ChatBindingManager)
    manager.db = SimpleNamespace(
        get_resumable_msglog_ingestion_scans=Mock(return_value=[
            SimpleNamespace(source_chat_id="100"), SimpleNamespace(source_chat_id="200"),
        ]),
        get_topic_slaves=Mock(side_effect=[[('a', 1)], [('b', 2)]]),
    )
    manager.schedule_msglog_ingestion = Mock()
    manager.logger = Mock()

    ChatBindingManager.resume_pending_msglog_ingestions(manager)

    assert manager.schedule_msglog_ingestion.call_args_list[0].args == (100,)
    assert manager.schedule_msglog_ingestion.call_args_list[1].args == (200,)


def test_schedule_msglog_ingestion_starts_one_thread_per_group(monkeypatch):
    manager = object.__new__(ChatBindingManager)
    manager.channel = SimpleNamespace(mtproto=SimpleNamespace(
        enabled=True, connected=True, config=SimpleNamespace(scan_ceiling=100),
    ))
    manager.db = SimpleNamespace(get_or_create_msglog_ingestion_scan=Mock(
        return_value=SimpleNamespace(status="pending", scanned_count=0),
    ))
    manager._msglog_ingestion_lock = Lock()
    manager._msglog_ingestion_threads = {}
    thread = SimpleNamespace(is_alive=Mock(return_value=True), start=Mock())
    monkeypatch.setattr("efb_telegram_master.chat_binding.threading.Thread", Mock(return_value=thread))

    assert ChatBindingManager.schedule_msglog_ingestion(manager, 100) == "started"
    assert ChatBindingManager.schedule_msglog_ingestion(manager, 100) == "already running"
    thread.start.assert_called_once()


def test_msglog_ingestion_uses_the_bound_telegram_runtime_loop():
    class LoopPinningRuntime:
        def __init__(self):
            self.calls = []

        def call(self, coroutine):
            self.calls.append(coroutine)
            coroutine.close()

    runtime = LoopPinningRuntime()
    manager = object.__new__(ChatBindingManager)
    manager.bot = SimpleNamespace(_runtime=runtime)
    manager.db = Mock()
    manager.channel = SimpleNamespace(mtproto=SimpleNamespace())
    manager.logger = Mock()

    ChatBindingManager._run_msglog_ingestion(manager, 100)

    assert len(runtime.calls) == 1


def test_msglog_ingestion_worker_owners_are_unique_when_thread_ids_collide(monkeypatch):
    owners = []

    class RecordingService:
        def __init__(self, _db, _mtproto):
            pass

        async def run(self, _source_chat_id, *, lease_owner):
            owners.append(lease_owner)

    class Runtime:
        def call(self, coroutine):
            asyncio.run(coroutine)

    manager = object.__new__(ChatBindingManager)
    manager.bot = SimpleNamespace(_runtime=Runtime())
    manager.db = Mock()
    manager.channel = SimpleNamespace(mtproto=SimpleNamespace())
    manager.logger = Mock()
    monkeypatch.setattr("efb_telegram_master.chat_binding.MsgLogIngestionService", RecordingService)
    monkeypatch.setattr("efb_telegram_master.chat_binding.threading.get_ident", lambda: 42)

    ChatBindingManager._run_msglog_ingestion(manager, 100)
    ChatBindingManager._run_msglog_ingestion(manager, 100)

    assert len(owners) == 2
    assert owners[0] != owners[1]
    assert all(UUID(owner).version == 4 for owner in owners)


def test_ingested_master_edits_do_not_dispatch_or_remove_messages():
    processor = object.__new__(MasterMessageProcessor)
    processor.channel = SimpleNamespace(_=lambda text: text)
    processor.channel_id = "tests.master"
    processor.db = SimpleNamespace(
        FAIL_FLAG="__fail__",
        get_msg_log=Mock(return_value=SimpleNamespace(
            provenance="mtproto_ingested", slave_message_id="mtproto-ingested:100.10",
        )),
    )
    processor.logger = Mock()
    processor.process_telegram_message = Mock()

    for text in ("ordinary edit", "rm` remove remotely"):
        message = Mock()
        message.chat = SimpleNamespace(id=100, is_forum=False)
        message.message_id = 10
        message.text = text
        message.to_dict.return_value = {}
        update = Update(update_id=1, edited_message=message)

        MasterMessageProcessor.msg(processor, update, None)

    assert processor.db.get_msg_log.call_count == 2
    processor.process_telegram_message.assert_not_called()


def test_ingested_master_reply_dispatches_without_target(monkeypatch):
    class CapturedMessage:
        def __init__(self):
            self.file = None
            self.target = None

        def put_telegram_file(self, _message):
            pass

    target_log = SimpleNamespace(
        provenance="mtproto_ingested",
        slave_origin_uid="tests.slave source",
        build_etm_msg=Mock(return_value=SimpleNamespace(chat=SimpleNamespace(uid="source"), uid="synthetic")),
    )
    sent_messages = []
    slave = SimpleNamespace(
        supported_message_types={master_message.MsgType.Text},
        channel_name="Test slave",
    )
    monkeypatch.setattr(master_message, "ETMMsg", CapturedMessage)
    monkeypatch.setattr(master_message, "get_msg_type", lambda _message: TGMsgType.Text)
    monkeypatch.setattr(master_message, "coordinator", SimpleNamespace(
        slaves={"tests.slave": slave},
        send_message=lambda message: sent_messages.append(message),
    ))

    processor = object.__new__(MasterMessageProcessor)
    processor.channel = SimpleNamespace(flag=Mock(return_value=False))
    processor.bot = Mock()
    processor.db = SimpleNamespace(
        get_msg_log=Mock(return_value=target_log),
        add_or_update_message_log=Mock(),
    )
    processor.chat_manager = SimpleNamespace(
        get_chat=Mock(return_value=SimpleNamespace(self=SimpleNamespace())),
    )
    processor.logger = Mock()

    reply = SimpleNamespace(chat=SimpleNamespace(id=100), message_id=9)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=100),
        message_id=10,
        reply_to_message=reply,
        text="normal body",
        text_markdown_v2="normal body",
        caption=None,
        caption_markdown_v2=None,
    )

    MasterMessageProcessor.process_telegram_message(
        processor, Update(update_id=1, message=message), None, "tests.slave source", quote=True,
    )

    assert len(sent_messages) == 1
    assert sent_messages[0].text == "normal body"
    assert sent_messages[0].target is None
    target_log.build_etm_msg.assert_not_called()


def test_ingested_text_and_media_backfill_use_copy_message():
    manager = object.__new__(ChatBindingManager)
    manager.db = Mock()
    manager.chat_manager = Mock()
    manager.logger = Mock()
    text_log = SimpleNamespace(
        master_msg_id="100.10", text="text", media_type="Text", provenance="mtproto_ingested",
        time=datetime.now(),
    )
    media_log = SimpleNamespace(
        master_msg_id="100.11", text="caption", media_type="Photo", provenance="mtproto_ingested",
        time=datetime.now(),
    )
    manager.db.get_recent_messages.return_value = [text_log, media_log]
    manager.db.replace_history_migration_entries.return_value = 2

    ChatBindingManager._queue_history_migration_entries(manager, "tests.slave", 300)

    entries = manager.db.replace_history_migration_entries.call_args.args[3]
    assert [entry["formatted_text"] for entry in entries] == [None, None]
    entry_namespaces = [SimpleNamespace(**entry) for entry in entries]
    assert [ChatBindingManager._prepare_history_migration_call(entry, 300, None)[0] for entry in
            entry_namespaces] == ["copy_message", "copy_message"]


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
