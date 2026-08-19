from types import SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot.types import ChatID, ModuleID
from telegram import Update

from efb_telegram_master import utils
from efb_telegram_master.channel_commands import TelegramCommandService
from efb_telegram_master.history_replay import HistoryReplayWorker
from efb_telegram_master.models import HistoryMigrationEntry
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID
from tests.unit.backfill_support import _build_link_update, _cleanup_link_state, _link_completion_service


def test_replacing_a_chat_association_discards_its_pending_history(channel, slave, bot_group):
    chat = slave.chat_with_alias
    slave_uid = utils.chat_id_to_str(chat=chat)
    old_master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(bot_group)))
    replacement_master_uid = utils.chat_id_to_str(channel.channel_id, ChatID("-100701"))
    channel.chat_associations.add_chat_assoc(old_master_uid, slave_uid)
    HistoryMigrationEntry.create(slave_chat_id=slave_uid, target_chat_id=str(bot_group), source_master_msg_id="10.20", formatted_text="pending", position=0)

    channel.chat_associations.add_chat_assoc(replacement_master_uid, slave_uid)

    assert not HistoryMigrationEntry.select().where(HistoryMigrationEntry.slave_chat_id == slave_uid).exists()
    _cleanup_link_state(channel, chat, -100701)


def test_private_callback_automatic_empty_backfill_omits_an_invalid_history_url():
    storage_key = (TelegramChatID(1044903212), TelegramMessageID(457))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(module_id=ModuleID("tests.slave"), uid=ChatID("chat"), linked=[], full_name="Test chat", link=Mock())
    service = _link_completion_service(storage_key, chat)

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id"):
        service.complete(_build_link_update(-100500), [token])

    service.history_replay.start.assert_called_once_with("tests.slave chat", -100500, None, storage_key)
    worker_bot = SimpleNamespace(send_message=Mock())
    worker = HistoryReplayWorker(worker_bot, Mock(), Mock(), Mock(), Mock())
    worker.queue_entries = Mock(return_value=0)
    worker._queue_and_process("tests.slave chat", -100500, None, storage_key)
    assert "https://t.me/" not in worker_bot.send_message.call_args.kwargs["text"]


def test_resolve_command_args_falls_back_to_raw_message_text():
    args = TelegramCommandService.resolve_command_args("/start token true", ["token"])

    assert args == ["token", "true"]


def test_start_uses_raw_message_args_for_link_chat(channel):
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1,
                "text": "/start token true",
                "chat": {"id": -1001, "type": "supergroup", "title": "Test Group"},
                "from": {"id": 42, "is_bot": False, "first_name": "Tester"},
            },
        },
        channel.bot_manager._async_bot,
    )
    context = SimpleNamespace(args=["token"])

    with patch.object(channel.link_completion, "complete") as link_chat:
        channel.command_service.start(update, context)

    link_chat.assert_called_once_with(update, ["token", "true"])
