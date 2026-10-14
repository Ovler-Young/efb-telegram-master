"""Telegram controls for source-chat text aggregation preferences."""

import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from ehforwarderbot.types import ChatID
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler

from . import utils
from .locale_mixin import LocaleMixin
from .ptb_compat import Filters, sync_reply_text
from .utils import EFBChannelChatIDStr

if TYPE_CHECKING:
    from . import TelegramChannel
    from .chat import ETMChatType


@dataclass
class ChatSelection:
    requester_id: int
    chat_id: int
    topic_id: Optional[int]
    action: str
    origins: list[EFBChannelChatIDStr]
    expires_at: float
    message_id: Optional[int] = None


class AggregationSettings(LocaleMixin):
    PAGE_SIZE = 8
    SESSION_SECONDS = 900

    def __init__(self, channel: 'TelegramChannel'):
        self.channel = channel
        self.bot = channel.bot_manager
        self.sessions: dict[str, ChatSelection] = {}
        self.lock = threading.RLock()
        self.bot.dispatcher.add_handler(CommandHandler(
            'aggregation', self.bot.as_async_callback(self.command),
            filters=Filters.update.message | Filters.update.channel_post))
        self.bot.dispatcher.add_handler(CallbackQueryHandler(
            self.bot.as_async_callback(self.callback), pattern=r'^aggregation:'))

    def _origins(self, message: Message) -> list[EFBChannelChatIDStr]:
        if message.chat.type not in ('group', 'supergroup'):
            return []
        if message.is_topic_message or message.message_thread_id is not None:
            origin = self.channel.db.get_topic_slave(message.chat.id, message.message_thread_id or 1)
            return [origin] if origin else []
        if message.chat.is_forum:
            origin = self.channel.db.get_topic_slave(message.chat.id, 1)
            return [origin] if origin else []
        return list(dict.fromkeys(self.channel.db.get_chat_assoc(
            master_uid=utils.chat_id_to_str(self.channel.channel_id, ChatID(str(message.chat.id))))))

    def _chat(self, origin: EFBChannelChatIDStr) -> 'ETMChatType':
        channel_id, chat_id, _ = utils.chat_id_str_to_id(origin)
        return self.channel.chat_manager.get_chat(channel_id, chat_id, build_dummy=True)

    def _apply(self, origin: EFBChannelChatIDStr, action: str) -> str:
        chat = self._chat(origin)
        if action != 'status':
            self.channel.db.set_text_aggregation_override(chat, {'on': True, 'off': False, 'reset': None}[action])
        override = self.channel.db.get_text_aggregation_override(chat.module_id, chat.uid)
        allowed = self.channel.flag.allows_text_aggregation(chat, origin, override)
        enabled = bool(self.channel.flag('text_aggregation') and allowed)
        source = self._('inherited') if override is None else self._('chat override: on') if override else self._('chat override: off')
        state = self._('on') if enabled else self._('off')
        text = self._('Text aggregation for {chat}: {state} ({source}).').format(
            chat=chat.alias or chat.name or origin, state=state, source=source)
        if not self.channel.flag('text_aggregation'):
            text += '\n' + self._('The global text aggregation switch is off.')
        return text

    def _keyboard(self, token: str, selection: ChatSelection, offset: int = 0) -> InlineKeyboardMarkup:
        rows = []
        for index in range(offset, min(offset + self.PAGE_SIZE, len(selection.origins))):
            origin = selection.origins[index]
            chat = self._chat(origin)
            rows.append([InlineKeyboardButton(chat.alias or chat.name or origin,
                                              callback_data=f'aggregation:{token}:select:{index}')])
        navigation = []
        if offset:
            navigation.append(InlineKeyboardButton('‹', callback_data=f'aggregation:{token}:page:{offset - self.PAGE_SIZE}'))
        if offset + self.PAGE_SIZE < len(selection.origins):
            navigation.append(InlineKeyboardButton('›', callback_data=f'aggregation:{token}:page:{offset + self.PAGE_SIZE}'))
        if navigation:
            rows.append(navigation)
        return InlineKeyboardMarkup(rows)

    def command(self, update: Update, context: CallbackContext):
        user, message = update.effective_user, update.effective_message
        if user is None or user.id not in self.channel.config['admins'] or message is None:
            return
        args = context.args or []
        if len(args) > 1 or (args and args[0] not in ('on', 'off', 'reset')):
            return sync_reply_text(self.bot, message, self._('Usage: /aggregation [on|off|reset]'))
        action = args[0] if args else 'status'
        origins = self._origins(message)
        if not origins:
            return sync_reply_text(self.bot, message, self._('This group or topic has no linked source chat.'))
        if len(origins) == 1:
            return sync_reply_text(self.bot, message, self._apply(origins[0], action))
        with self.lock:
            now = time.monotonic()
            self.sessions = {key: value for key, value in self.sessions.items() if value.expires_at > now}
            token = secrets.token_hex(8)
            selection = ChatSelection(user.id, message.chat.id, message.message_thread_id,
                                      action, origins, now + self.SESSION_SECONDS)
            sent = sync_reply_text(self.bot, message, self._('Select a source chat:'),
                                   reply_markup=self._keyboard(token, selection), _force_main_bot=True)
            selection.message_id = sent.message_id
            self.sessions[token] = selection

    def callback(self, update: Update, context: CallbackContext):
        query, user, message = update.callback_query, update.effective_user, update.effective_message
        if query is None or user is None or message is None:
            return
        if user.id not in self.channel.config['admins']:
            return self.bot.answer_callback_query(query.id, text=self._('Only configured bot admins can change this setting.'))
        parts = (query.data or '').split(':')
        with self.lock:
            selection = self.sessions.get(parts[1]) if len(parts) == 4 else None
            if (selection is None or selection.expires_at <= time.monotonic()
                    or selection.requester_id != user.id or selection.chat_id != message.chat.id
                    or selection.message_id != message.message_id
                    or selection.topic_id != message.message_thread_id):
                return self.bot.answer_callback_query(query.id, text=self._('This selection has expired. Send /aggregation again.'))
            token, action, index_text = parts[1:]
            if action not in ('page', 'select') or not index_text.isdecimal():
                return self.bot.answer_callback_query(query.id, text=self._('Invalid selection.'))
            index = int(index_text)
            if index >= len(selection.origins):
                return self.bot.answer_callback_query(query.id, text=self._('Invalid selection.'))
            if action == 'page':
                self.bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=message.message_id,
                                                    reply_markup=self._keyboard(token, selection, index))
            else:
                origin = selection.origins[index]
                if origin not in self._origins(message):
                    return self.bot.answer_callback_query(query.id, text=self._('The chat binding changed. Send /aggregation again.'))
                text = self._apply(origin, selection.action)
                self.sessions.pop(token)
                self.bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id,
                                           text=text, reply_markup=None)
            self.bot.answer_callback_query(query.id)
