from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest
from ehforwarderbot.types import ChatID, ModuleID

from efb_telegram_master import utils
from efb_telegram_master.constants import Flags
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID
from tests.unit.backfill_support import _build_link_update, _cleanup_link_state, _link_chat_update, _link_completion_service, _sent_link_message


def test_link_completion_reads_multiple_slave_setting_at_completion_time():
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(458))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(module_id=ModuleID("tests.slave"), uid=ChatID("chat"), linked=[], full_name="Test chat", link=Mock())
    multiple_slave_chats = Mock(return_value=False)
    service = _link_completion_service(storage_key, chat, multiple_slave_chats)

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id"):
        service.complete(_build_link_update(-100500), [token])

    multiple_slave_chats.assert_called_once_with()
    assert chat.link.call_args.args[-1] is False


def test_link_completion_rejects_an_inactive_slave_without_consuming_its_session():
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(457))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(module_id=ModuleID("tests.inactive_slave"), uid=ChatID("chat"), linked=[], full_name="Inactive chat", link=Mock())
    service = _link_completion_service(storage_key, chat)

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id", side_effect=NameError):
        service.complete(_build_link_update(-100500), [token])

    service.bot.edit_message_text.assert_called_once_with(
        text="tests.inactive_slave is not activated in current profile. It cannot be linked.",
        chat_id=storage_key[0],
        message_id=storage_key[1],
    )
    service.bot.send_message.assert_not_called()
    chat.link.assert_not_called()
    service.chat_associations.remove_topic_assoc.assert_not_called()
    service.topic_sync.create_topic.assert_not_called()
    service.history_replay.start.assert_not_called()
    assert service.callback_sessions.lookup(storage_key) is not None
    assert service._conversation_handler._conversations[storage_key] == Flags.LINK_EXEC


def test_forged_start_token_does_not_consume_the_owner_session():
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(459))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(module_id=ModuleID("tests.slave"), uid=ChatID("chat"), linked=[], full_name="Test chat", link=Mock())
    service = _link_completion_service(storage_key, chat)

    service.complete(_build_link_update(-100500, user_id=2), [token])

    chat.link.assert_not_called()
    service.bot.send_message.assert_called_once_with(-100500, text="Session expired or unknown parameter. (SE02)", message_thread_id=ANY)
    assert service.callback_sessions.lookup(storage_key) is not None
    assert storage_key in service._conversation_handler._conversations

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id"):
        service.complete(_build_link_update(-100500), [token])
    chat.link.assert_called_once()
    assert service.callback_sessions.lookup(storage_key) is None


def test_start_with_missing_effective_user_does_not_consume_a_session():
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(460))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    chat = SimpleNamespace(module_id=ModuleID("tests.slave"), uid=ChatID("chat"), linked=[], full_name="Test chat", link=Mock())
    service = _link_completion_service(storage_key, chat)
    update = _build_link_update(-100500)
    update.effective_message.text = "/start " + token
    update.effective_message.from_user = None

    service.complete(update, [token])

    chat.link.assert_not_called()
    assert service.callback_sessions.lookup(storage_key) is not None


def test_link_chat_preserves_session_when_link_fails(channel, slave, bot_group):
    chat = channel.chat_manager.update_chat_obj(slave.chat_with_alias)
    storage_key, token, update = _link_chat_update(channel, chat, bot_group, 106)
    channel.callback_sessions.set_state(channel.link_handler, storage_key, Flags.LINK_EXEC)

    with patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 506)), patch.object(chat, "link", side_effect=RuntimeError("link failed")):
        with pytest.raises(RuntimeError, match="link failed"):
            channel.link_completion.complete(update, [token])

    assert channel.callback_sessions.lookup(storage_key) is not None
    assert channel.link_handler._conversations[storage_key] == Flags.LINK_EXEC
    _cleanup_link_state(channel, chat, bot_group)
