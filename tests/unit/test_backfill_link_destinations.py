from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from ehforwarderbot.types import ChatID, ModuleID

from efb_telegram_master import utils
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID
from tests.unit.backfill_support import _add_chat_association, _build_link_update, _cleanup_link_state, _link_chat_update, _link_completion_service, _sent_link_message, _store_link_session


def test_link_chat_auto_mode_backfills_on_first_link(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key, token, update = _link_chat_update(channel, chat, bot_group, 101)
    sent_message = _sent_link_message(bot_group, 500)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as migrate_chat_history,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token])

    migrate_chat_history.assert_called_once_with(utils.chat_id_to_str(chat=chat), bot_group, None, storage_key)
    send_history_link.assert_not_called()
    assert storage_key not in channel.link_handler._conversations
    _cleanup_link_state(channel, chat, bot_group)


def test_initial_link_backfills_to_the_chat_migrated_during_status_edit(channel, slave, bot_group):
    chat = slave.chat_with_alias
    migrated_chat_id = -100700
    storage_key, token, update = _link_chat_update(channel, chat, bot_group, 109)

    def migrate_status_edit(**kwargs):
        if kwargs["chat_id"] == bot_group:
            channel.topic_sync.migrate_chat_associations(bot_group, migrated_chat_id)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 509)),
        patch.object(channel.bot_manager, "get_chat_info", return_value=SimpleNamespace(is_forum=False)),
        patch.object(channel.bot_manager, "edit_message_text", side_effect=migrate_status_edit),
        patch.object(channel.history_replay, "start") as start,
    ):
        channel.link_completion.complete(update, [token])

    start.assert_called_once_with(utils.chat_id_to_str(chat=chat), migrated_chat_id, None, storage_key)
    _cleanup_link_state(channel, chat, migrated_chat_id)


def test_link_chat_edits_status_message_with_sender_bot(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key, token, update = _link_chat_update(channel, chat, bot_group, 105)
    sent_message = _sent_link_message(bot_group, 505, sender_bot_id="8465204282")

    with (
        patch.object(channel.bot_manager, "send_message", return_value=sent_message),
        patch.object(channel.bot_manager, "edit_message_text") as edit_message_text,
        patch.object(channel.history_replay, "start"),
        patch.object(channel.link_completion, "send_history_link"),
    ):
        channel.link_completion.complete(update, [token])

    target_status_edit = edit_message_text.call_args_list[0].kwargs
    assert target_status_edit["chat_id"] == bot_group
    assert target_status_edit["message_id"] == 505
    assert target_status_edit["_sender_bot_id"] == "8465204282"
    _cleanup_link_state(channel, chat, bot_group)


def test_link_chat_auto_mode_sends_history_link_on_relink(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key, token, update = _link_chat_update(channel, chat, bot_group, 102)
    _add_chat_association(channel, chat, bot_group)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 501)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as migrate_chat_history,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token])

    migrate_chat_history.assert_not_called()
    send_history_link.assert_called_once()
    _cleanup_link_state(channel, chat, bot_group)


@pytest.mark.parametrize("backfill_flag", ["true", "yes", "on", "1"])
def test_link_chat_backfill_override_forces_replay(channel, slave, bot_group, backfill_flag):
    chat = slave.chat_with_alias
    storage_key, token, update = _link_chat_update(channel, chat, bot_group, 103)
    _add_chat_association(channel, chat, bot_group)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 502)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as migrate_chat_history,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token, backfill_flag])

    migrate_chat_history.assert_called_once_with(utils.chat_id_to_str(chat=chat), bot_group, None, storage_key)
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


@pytest.mark.parametrize("backfill_flag", ["false", "no", "off", "0"])
def test_link_chat_backfill_override_skips_replay(channel, slave, bot_group, backfill_flag):
    chat = slave.chat_with_alias
    storage_key, token, update = _link_chat_update(channel, chat, bot_group, 130)
    _add_chat_association(channel, chat, bot_group)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 530)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as replay,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token, backfill_flag])

    replay.assert_not_called()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)


@pytest.mark.parametrize("backfill_flag", ["true", "yes", "on", "1"], ids=str)
def test_requested_empty_backfill_sends_one_history_location_to_the_linked_topic(channel, slave, bot_group, backfill_flag):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(456))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    _add_chat_association(channel, chat, bot_group)
    update = _build_link_update(bot_group, is_forum=True)
    topic_id = 789
    channel.topic_sync.create_topic = Mock(return_value=topic_id)
    channel.msglogs.get_recent_messages = Mock(return_value=[])
    channel.history_migrations.replace_entries = Mock(return_value=0)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 531)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as start,
        patch.object(channel.topic_sync, "update_single_topic_info"),
    ):
        channel.link_completion.complete(update, [token, backfill_flag])
        start.assert_called_once_with(utils.chat_id_to_str(chat=chat), bot_group, topic_id, storage_key)
        channel.history_replay._queue_and_process(utils.chat_id_to_str(chat=chat), bot_group, topic_id, storage_key)

    history_calls = [call for call in channel.bot_manager.api.send_message.call_args_list if "previously linked" in call.kwargs.get("text", "")]
    assert len(history_calls) == 1
    assert history_calls[0].kwargs == {
        "chat_id": bot_group,
        "text": "This chat was previously linked. History messages are not migrated. You can view previous messages here: https://t.me/c/1234567890/456",
        "disable_notification": True,
        "message_thread_id": topic_id,
    }
    _cleanup_link_state(channel, chat, bot_group)


def test_automatic_empty_backfill_sends_the_original_history_location_once(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key = (TelegramChatID(-1001234567890), TelegramMessageID(457))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    _store_link_session(channel, chat, storage_key)
    channel.msglogs.get_recent_messages = Mock(return_value=[])
    channel.history_migrations.replace_entries = Mock(return_value=0)

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 532)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as start,
    ):
        channel.link_completion.complete(_build_link_update(bot_group), [token])
        start.assert_called_once_with(utils.chat_id_to_str(chat=chat), bot_group, None, storage_key)
        channel.history_replay._queue_and_process(utils.chat_id_to_str(chat=chat), bot_group, None, storage_key)

    history_calls = [call for call in channel.bot_manager.api.send_message.call_args_list if "previously linked" in call.kwargs.get("text", "")]
    assert len(history_calls) == 1
    assert history_calls[0].kwargs["chat_id"] == bot_group
    assert history_calls[0].kwargs["text"].endswith("https://t.me/c/1234567890/457")
    _cleanup_link_state(channel, chat, bot_group)


def test_private_callback_relink_uses_the_previous_supergroup_history_location():
    storage_key = (TelegramChatID(1044903212), TelegramMessageID(458))
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    previous_group_id = TelegramChatID(-1001234567890)
    chat = SimpleNamespace(
        module_id=ModuleID("tests.slave"),
        uid=ChatID("chat"),
        linked=[utils.chat_id_to_str(ModuleID("blueset.telegram"), ChatID(str(previous_group_id)))],
        full_name="Test chat",
        link=Mock(),
    )
    service = _link_completion_service(storage_key, chat)

    with patch("efb_telegram_master.link_completion.coordinator.get_module_by_id"):
        service.complete(_build_link_update(-100500), [token])

    history_calls = [call for call in service.bot.send_message.call_args_list if "previously linked" in call.kwargs.get("text", "")]
    assert len(history_calls) == 1
    assert history_calls[0].kwargs["text"].endswith("https://t.me/c/1234567890/458")


def test_link_chat_raw_message_override_forces_behavior_when_args_are_truncated(channel, slave, bot_group):
    chat = slave.chat_with_alias
    storage_key, token, update = _link_chat_update(channel, chat, bot_group, 104)
    _add_chat_association(channel, chat, bot_group)
    update.effective_message.text = f"/start {token} true"

    with (
        patch.object(channel.bot_manager, "send_message", return_value=_sent_link_message(bot_group, 503)),
        patch.object(channel.bot_manager, "edit_message_text"),
        patch.object(channel.history_replay, "start") as migrate_chat_history,
        patch.object(channel.link_completion, "send_history_link") as send_history_link,
    ):
        channel.link_completion.complete(update, [token])

    migrate_chat_history.assert_called_once()
    send_history_link.assert_not_called()
    _cleanup_link_state(channel, chat, bot_group)
