"""Select a real source member from a confirmed Telegram container."""

import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler

from .aggregate import member_identity, member_message, utf16_length
from .ptb_compat import sync_reply_text
from . import utils
from .utils import EFBChannelChatIDStr

if TYPE_CHECKING:
    from . import TelegramChannel
    from .db import MsgLog
    from .master_message import MasterMessageProcessor
    from .message import ETMMsg

SourceIdentity = Tuple[str, str]


def current_source_route(channel: 'TelegramChannel', origin: str) -> Tuple[int, Optional[str]]:
    """Read the current source association without creating a topic."""
    associations = channel.db.get_chat_assoc(slave_uid=origin)
    target = int(utils.chat_id_str_to_id(associations[0])[1]) if associations else int(
        channel.topic_group or channel.config['admins'][0])
    topic = channel.db.get_topic_thread_id(slave_uid=origin, topic_chat_id=target) if channel.topic_group else None
    return target, str(topic) if topic is not None else None


@dataclass
class MemberSelection:
    update: Update
    context: CallbackContext
    destination: str
    method: str
    target_id: str
    requester_id: int
    chat_id: int
    identities: list[SourceIdentity]
    expires_at: float
    message_id: Optional[int] = None


class SourceMemberSelector:
    PAGE_SIZE = 8
    SESSION_SECONDS = 900

    def __init__(self, processor: 'MasterMessageProcessor'):
        self.processor = processor
        self.sessions: dict[str, MemberSelection] = {}
        self.lock = threading.RLock()
        processor.bot.dispatcher.add_handler(CallbackQueryHandler(
            processor.bot.as_async_callback(self.callback), pattern=r'^member:'))

    def resolve(self, target: 'MsgLog', identity: SourceIdentity) -> Optional['ETMMsg']:
        """Recheck the clicked source against current routing and queued state."""
        aggregate = target.aggregate
        children = aggregate['children'] if aggregate else [target.source_member] if target.source_member else []
        if not any(member_identity(child) == identity for child in children):
            return None
        target_chat = int(target.master_msg_id.rsplit('.', 1)[0])
        topic = target.master_message_thread_id
        if current_source_route(self.processor.channel, identity[0]) != (target_chat, topic):
            return None
        try:
            row, member, _ = self.processor.bot.live_aggregation.source_state(
                (identity[0], target_chat, topic), identity)
        except ValueError:
            return None
        if row is None or member is None or member['status'] != 'active':
            return None
        message = row.build_source_member(member, self.processor.chat_manager)
        message.target = None
        return message

    @staticmethod
    def quoted_identity(update: Update, target: 'MsgLog') -> Optional[SourceIdentity]:
        message = update.effective_message
        aggregate = target.aggregate
        if not message or not aggregate or not message.quote or not message.reply_to_message:
            return None
        quote = message.quote
        text = aggregate['confirmed_text']
        # Telegram's nested reply is the user's displayed snapshot, which may
        # predate the last confirmed edit even when its quote remains present.
        if message.reply_to_message.text != text or not quote.text:
            return None
        first = text.find(quote.text)
        if first < 0 or first != text.rfind(quote.text):
            return None
        start = quote.position
        end = start + utf16_length(quote.text)
        if start < 0:
            return None
        try:
            fragment = text.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')
        except UnicodeDecodeError:
            return None
        if fragment != quote.text:
            return None
        matches = [item for item in aggregate['confirmed_ranges']
                   if item['start'] <= start and end <= item['end']]
        return (matches[0]['origin_uid'], matches[0]['source_id']) if len(matches) == 1 else None

    def select(self, update: Update, context: CallbackContext, target: 'MsgLog', destination: str,
               method: str, selected_identity: Optional[SourceIdentity] = None) -> Optional['ETMMsg']:
        if selected_identity is None and target.source_member:
            selected_identity = member_identity(target.source_member)
        if selected_identity is not None:
            result = self.resolve(target, selected_identity)
            if result is None or selected_identity[0] != destination:
                self.processor.bot.reply_error(update, self.processor._(
                    'This source message is no longer available here. Please retry the reply or /rm.'))
                return None
            return result
        aggregate = target.aggregate
        assert aggregate is not None
        identity = self.quoted_identity(update, target)
        if identity is None and len(aggregate['children']) == 1:
            identity = member_identity(aggregate['children'][0])
        if identity and identity[0] == destination:
            result = self.resolve(target, identity)
            if result:
                return result
        self.show(update, context, target, destination, method)
        return None

    def show(self, update: Update, context: CallbackContext, target: 'MsgLog', destination: str, method: str):
        user = update.effective_user
        if user is None or user.id not in self.processor.channel.config['admins']:
            return
        aggregate = target.aggregate
        assert aggregate is not None
        assert update.effective_chat is not None and update.effective_message is not None
        identities = [member_identity(child) for child in aggregate['children']
                      if child['origin_uid'] == destination and self.resolve(target, member_identity(child))]
        if not identities:
            return self.processor.bot.reply_error(update, self.processor._(
                'This source message is no longer available here. Please retry the reply or /rm.'))
        session = MemberSelection(update, context, destination, method, target.master_msg_id,
                                  user.id, update.effective_chat.id, identities,
                                  time.monotonic() + self.SESSION_SECONDS)
        with self.lock:
            self.sessions = {token: saved for token, saved in self.sessions.items()
                             if saved.expires_at > time.monotonic()}
            token = secrets.token_urlsafe(9)
            self.sessions[token] = session
            try:
                reply = sync_reply_text(self.processor.bot, update.effective_message,
                                        self.processor._('Choose a source message.'),
                                        reply_markup=self.keyboard(token, session, target, 0),
                                        _force_main_bot=True)
                session.message_id = reply.message_id
            except Exception:
                self.sessions.pop(token, None)
                raise

    def keyboard(self, token: str, session: MemberSelection, target: 'MsgLog', page: int):
        aggregate = target.aggregate
        assert aggregate is not None
        children = {member_identity(child): child for child in aggregate['children']}
        rows = []
        for index in range(page * self.PAGE_SIZE, min((page + 1) * self.PAGE_SIZE, len(session.identities))):
            member = children[session.identities[index]]
            label = '{}: {}'.format(member['author_name'], ' '.join((member_message(member).text or '').split()))
            rows.append([InlineKeyboardButton(label[:80], callback_data=f'member:{token}:s:{index}')])
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton('‹', callback_data=f'member:{token}:p:{page - 1}'))
        navigation.append(InlineKeyboardButton(self.processor._('Cancel'), callback_data=f'member:{token}:c:0'))
        if (page + 1) * self.PAGE_SIZE < len(session.identities):
            navigation.append(InlineKeyboardButton('›', callback_data=f'member:{token}:p:{page + 1}'))
        rows.append(navigation)
        return InlineKeyboardMarkup(rows)

    def callback(self, update: Update, context: CallbackContext):
        query = update.callback_query
        user = update.effective_user
        if query is None:
            return
        with self.lock:
            parts = (query.data or '').split(':')
            session = self.sessions.get(parts[1]) if len(parts) == 4 else None
            if (session is None or session.expires_at <= time.monotonic() or user is None
                    or user.id != session.requester_id or user.id not in self.processor.channel.config['admins']
                    or update.effective_chat is None or update.effective_chat.id != session.chat_id
                    or update.effective_message is None or update.effective_message.message_id != session.message_id):
                return self.processor.bot.answer_callback_query(query.id, text=self.processor._(
                    'This selection has expired or belongs to another user. Please retry the reply or /rm.'))
            token, action, argument = parts[1:]
            target = self.processor.db.get_msg_log(master_msg_id=session.target_id)
            if target is None or not target.aggregate or not argument.isdecimal():
                return self.processor.bot.answer_callback_query(query.id, text=self.processor._(
                    'This selection has expired. Please retry the reply or /rm.'))
            index = int(argument)
            if action == 'p' and 0 <= index <= (len(session.identities) - 1) // self.PAGE_SIZE:
                if not all(self.resolve(target, identity) for identity in session.identities):
                    self.sessions.pop(token, None)
                    return self.processor.bot.answer_callback_query(query.id, text=self.processor._(
                        'Source messages have changed. Please retry the reply or /rm.'))
                self.processor.bot.edit_message_reply_markup(chat_id=session.chat_id, message_id=session.message_id,
                                                             reply_markup=self.keyboard(token, session, target, index))
                return self.processor.bot.answer_callback_query(query.id)
            if action != 'c' and (action != 's' or not 0 <= index < len(session.identities)):
                return self.processor.bot.answer_callback_query(query.id)
            self.sessions.pop(token, None)
            self.processor.bot.edit_message_reply_markup(chat_id=session.chat_id, message_id=session.message_id,
                                                         reply_markup=None)
            self.processor.bot.answer_callback_query(query.id)
            if action == 'c':
                return
            identity = session.identities[index]
            if session.method == 'rm':
                return self.processor.delete_message(session.update, session.context, selected_identity=identity)
            return self.processor.process_telegram_message(session.update, session.context, EFBChannelChatIDStr(session.destination),
                                                           quote=True, selected_identity=identity)
