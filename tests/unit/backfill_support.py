"""Shared setup for split backfill behavior tests.

These helpers consolidate repeated callback-session and sent-message setup;
behavioral assertions remain in the module that owns each contract.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from ehforwarderbot.types import ChatID, ModuleID
from telegram import Update

from efb_telegram_master import utils
from efb_telegram_master.callback_sessions import CallbackSessionStore, ChatListStorage
from efb_telegram_master.constants import Flags
from efb_telegram_master.link_completion import LinkCompletionService
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID


def _build_link_update(chat_id, *, is_forum=False, user_id=1):
    effective_chat = SimpleNamespace(id=chat_id, is_forum=is_forum, type="group")
    message = Mock()
    message.chat = effective_chat
    message.forward_from_chat = None
    message.reply_text = Mock()
    message.from_user = SimpleNamespace(id=user_id)
    return Update(update_id=1, message=message)


def _store_link_session(channel, chat, storage_key):
    storage = ChatListStorage([channel.chat_manager.update_chat_obj(chat)])
    channel.callback_sessions.store(storage_key, 1, storage)


def _link_chat_update(channel, chat, bot_group, message_id):
    storage_key = (TelegramChatID(bot_group), TelegramMessageID(message_id))
    _store_link_session(channel, chat, storage_key)
    token = utils.b64en(utils.message_id_to_str(*storage_key))
    return storage_key, token, _build_link_update(bot_group)


def _add_chat_association(channel, chat, master_chat_id):
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(master_chat_id)))
    channel.chat_associations.add_chat_assoc(master_uid, utils.chat_id_to_str(chat=chat))


def _cleanup_link_state(channel, chat, master_chat_id):
    master_uid = utils.chat_id_to_str(channel.channel_id, ChatID(str(master_chat_id)))
    channel.chat_associations.remove_chat_assoc(master_uid=master_uid)
    channel.chat_associations.remove_topic_assoc(slave_uid=utils.chat_id_to_str(chat=chat))


def _sent_link_message(chat_id, message_id, sender_bot_id=None):
    sent_message = Mock()
    sent_message.chat.id = chat_id
    sent_message.message_id = message_id
    sent_message.reply_text = Mock()
    sent_message.sender_bot_id = sender_bot_id
    return sent_message


def _link_completion_service(storage_key, chat, multiple_slave_chats=lambda: False):
    bot = SimpleNamespace(
        send_message=Mock(return_value=_sent_link_message(-100500, 600)),
        edit_message_text=Mock(),
    )
    callback_sessions = CallbackSessionStore(bot, lambda: 10)
    handler = SimpleNamespace(_conversations={})
    callback_sessions.start(handler, storage_key, Flags.LINK_EXEC, 1, ChatListStorage([chat]))
    service = LinkCompletionService(
        bot,
        ModuleID("blueset.telegram"),
        multiple_slave_chats,
        SimpleNamespace(remove_topic_assoc=Mock(), get_chat_assoc=Mock(return_value=[])),
        callback_sessions,
        Mock(),
        Mock(),
        lambda message: message,
        lambda single, plural, count: single if count == 1 else plural,
        Mock(),
        handler,
    )
    return service
