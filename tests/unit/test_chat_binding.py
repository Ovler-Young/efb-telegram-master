import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

from telegram import InlineKeyboardButton, Update
from telegram.ext import CallbackQueryHandler, ConversationHandler

from efb_telegram_master import utils
from efb_telegram_master.chat_binding import ChatBindingManager, ChatListStorage
from efb_telegram_master.constants import Flags
from efb_telegram_master.utils import TelegramChatID, TelegramMessageID


def test_full_chat_pagination(channel, slave):
    storage_id = (TelegramChatID(0), TelegramMessageID(1))
    legends, buttons = channel.chat_binding.slave_chats_pagination(storage_id)
    legend = "\n".join(legends)
    assert slave.channel_emoji in legend
    assert slave.channel_name in legend
    assert min(channel.flag("chats_per_page"), len(slave.get_chats())) == len(buttons) - 1


def test_source_chat_pagination(channel, slave):
    storage_id = (TelegramChatID(0), TelegramMessageID(3))
    source_chats = [utils.chat_id_to_str(chat=slave.group)]
    legends, buttons = channel.chat_binding.slave_chats_pagination(storage_id, source_chats=source_chats)
    legend = "\n".join(legends)
    assert slave.channel_emoji in legend
    assert slave.channel_name in legend
    assert len(buttons) == 2


def test_chat_pagination_filters_groups_users_and_invalid_regex(channel, slave):
    _, buttons = channel.chat_binding.slave_chats_pagination(
        (TelegramChatID(0), TelegramMessageID(4)), pattern="wonderland"
    )
    names = [button.text for row in buttons[:-1] for button in row]
    assert names and all("Wonderland" in name for name in names)

    for message_id, (pattern, chat_type) in enumerate(
            (("type: group", "GroupChat"), ("type: private", "PrivateChat")), start=5):
        _, buttons = channel.chat_binding.slave_chats_pagination(
            (TelegramChatID(0), TelegramMessageID(message_id)), pattern=pattern
        )
        names = [button.text for row in buttons[:-1] for button in row]
        expected = slave.get_chats_by_criteria(chat_type=chat_type)
        assert names and all(any(chat.display_name in name for chat in expected) for name in names)

    _, buttons = channel.chat_binding.slave_chats_pagination(
        (TelegramChatID(0), TelegramMessageID(7)), pattern="("
    )
    assert len(buttons) == 1


def test_truncate_ellipsis(channel):
    truncate_ellipsis = channel.chat_binding.truncate_ellipsis
    short_text = "short text"
    long_text = "This is a long text. Cursus pellentesque cras maecenas hac malesuada porttitor nullam, dignissim enim feugiat placerat eget quisque, dui sem dictum fames sapien mauris. Feugiat euismod nisi donec nunc cras aliquam diam, arcu fames pretium pellentesque faucibus phasellus, in montes felis elit lacinia auctor. Commodo curae nibh donec vel ipsum sociosqu maecenas pellentesque scelerisque suspendisse blandit himenaeos rutrum ad, nec dictum porttitor non luctus fringilla feugiat volutpat adipiscing cubilia vitae lacus. Tempor iaculis facilisis maecenas quam nisl pulvinar magnis lacus, sodales porta quisque rutrum habitasse metus purus ante libero, malesuada mollis est donec cubilia accumsan parturient. Parturient libero gravida imperdiet massa praesent habitant scelerisque pellentesque mollis elit, urna quisque tellus in nostra aliquet montes natoque fermentum, condimentum enim magna odio vestibulum mauris viverra sagittis iaculis."
    assert truncate_ellipsis(short_text, len(short_text) + 10) == short_text
    truncated = truncate_ellipsis(long_text, 256)
    assert len(truncated) <= 256
    assert truncated.endswith("…")


def test_recipient_can_be_selected_while_suggestion_edit_is_completing():
    binding = ChatBindingManager.__new__(ChatBindingManager)
    binding.msg_storage = {}
    delivered = []
    binding.channel = SimpleNamespace(
        _=lambda text: text,
        chat_binding=binding,
        master_messages=SimpleNamespace(
            process_telegram_message=lambda update, context, destination: delivered.append(
                (update.effective_message.text, destination)
            ),
        ),
    )
    recipient = SimpleNamespace(module_id='slave', uid='recipient', full_name='Recipient')
    original = Update.de_json({
        'update_id': 1,
        'message': {'message_id': 10, 'date': 1, 'text': 'deliver this text',
                    'chat': {'id': 42, 'type': 'private'},
                    'from': {'id': 42, 'is_bot': False, 'first_name': 'User'}},
    }, None)
    callback = Update.de_json({
        'update_id': 2,
        'callback_query': {
            'id': 'selection', 'chat_instance': 'private', 'data': 'chat 0',
            'from': {'id': 42, 'is_bot': False, 'first_name': 'User'},
            'message': {'message_id': 11, 'date': 1,
                        'chat': {'id': 42, 'type': 'private'}},
        },
    }, None)

    async def select(update, context):
        return binding.suggested_recipient(update, context)

    binding.suggestion_handler = ConversationHandler(
        entry_points=[],
        states={Flags.SUGGEST_RECIPIENTS: [CallbackQueryHandler(select)]},
        fallbacks=[], per_message=True, per_chat=True, per_user=False,
    )

    def paginate(storage_id, *args, **kwargs):
        binding.msg_storage[storage_id] = ChatListStorage([recipient])
        return [], [[InlineKeyboardButton('Recipient', callback_data='chat 0')],
                    [InlineKeyboardButton('Cancel', callback_data='cancel')]]

    binding.slave_chats_pagination = paginate
    edits = []

    def edit(**kwargs):
        edits.append(kwargs['text'])
        if 'reply_markup' in kwargs:
            # Telegram can publish the buttons before returning the edit RPC.
            check = binding.suggestion_handler.check_update(callback)
            assert check is not None, 'Visible recipient buttons must accept callbacks'
            asyncio.run(binding.suggestion_handler.handle_update(
                callback, SimpleNamespace(bot=None), check, SimpleNamespace(),
            ))

    binding.bot = SimpleNamespace(edit_message_text=edit, answer_callback_query=Mock())
    binding.register_suggestions(original, ['slave recipient'], TelegramChatID(42), TelegramMessageID(11))

    assert delivered == [('deliver this text', 'slave recipient')]
    assert edits[-1] == 'Delivering the message to Recipient.'
