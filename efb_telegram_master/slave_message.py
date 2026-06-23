# coding=utf-8

import html
import itertools
import logging
import os
import tempfile
import time
import traceback
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Tuple, Optional, TYPE_CHECKING, List, IO, Union

import humanize
import telegram  # lgtm [py/import-and-import-from]
import telegram.constants
import telegram.error
from PIL import Image
from telegram import InputFile, InputMediaPhoto, InputMediaDocument, InputMediaVideo, InputMediaAnimation, \
    InlineKeyboardMarkup, InlineKeyboardButton, InputMedia
from telegram._utils.types import ReplyMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError

from ehforwarderbot import Message, Status, coordinator
from ehforwarderbot.chat import ChatNotificationState, SelfChatMember, GroupChat, PrivateChat, SystemChat, Chat
from ehforwarderbot.constants import MsgType
from ehforwarderbot.message import LinkAttribute, LocationAttribute, MessageCommand, Reactions, \
    StatusAttribute
from ehforwarderbot.status import ChatUpdates, MemberUpdates, MessageRemoval, MessageReactionsUpdate
from . import utils
from .chat_destination_cache import ChatDestinationCache
from .chat_object_cache import ChatObjectCacheManager
from .commands import ETMCommandMsgStorage
from .constants import Emoji
from .locale_mixin import LocaleMixin
from .message import ETMMsg
from .msg_type import get_msg_type
from .utils import TelegramChatID, TelegramTopicID, TelegramMessageID, OldMsgID

if TYPE_CHECKING:
    from . import TelegramChannel
    from .bot_manager import TelegramBotManager
    from .db import DatabaseManager


class SlaveMessageProcessor(LocaleMixin):
    """Process messages as Message objects from slave channels."""

    REACTION_DB_WAIT_TIMEOUT = 2.0
    REACTION_DB_WAIT_INTERVAL = 0.05
    REMOTE_IMAGE_URL_VENDOR_KEY = "blueset.telegram.image_url"

    def __init__(self, channel: 'TelegramChannel'):
        self.channel: 'TelegramChannel' = channel
        self.bot: 'TelegramBotManager' = self.channel.bot_manager
        self.logger: logging.Logger = logging.getLogger(__name__)
        self.flag: utils.ExperimentalFlagsManager = self.channel.flag
        self.db: 'DatabaseManager' = channel.db
        self.chat_dest_cache: ChatDestinationCache = channel.chat_dest_cache
        self.chat_manager: ChatObjectCacheManager = channel.chat_manager

    @staticmethod
    def _get_edit_kwargs(msg: Message):
        """Forward sender bot metadata so edit wrappers reserve the matching quota."""
        _sender_bot_id = (msg.vendor_specific or {}).get('_sender_bot_id')
        if _sender_bot_id:
            return {'_sender_bot_id': _sender_bot_id}
        return {}

    def is_silent(self, msg: Message) -> Optional[bool]:
        """Determine if a message shall be sent silently.
        Returns None if the message shall not be sent at all.
        """
        xid = msg.uid
        if isinstance(msg.author, SelfChatMember):
            # Message is send by admin not through EFB
            your_slave_msg = self.flag('your_message_on_slave')
            if your_slave_msg == 'silent':
                return True
            elif your_slave_msg == 'mute':
                self.logger.debug("[%s] Message is muted as it is from the admin.", xid)
                return None
        elif msg.chat.notification == ChatNotificationState.NONE or \
                (msg.chat.notification == ChatNotificationState.MENTIONS and
                 (not msg.substitutions or not msg.substitutions.is_mentioned)):
            # Shall not be notified in slave channel
            muted_on_slave = self.flag('message_muted_on_slave')
            if muted_on_slave == 'silent':
                return True
            elif muted_on_slave == 'mute':
                self.logger.debug("[%s] Message is muted due to slave channel settings.", xid)
                return None
        return False

    def send_message(self, msg: Message) -> Message:
        """
        Process a message from slave channel and deliver it to the user.

        Args:
            msg (Message): The message.
        """
        try:
            xid = msg.uid
            self.logger.debug("[%s] Slave message delivered to ETM.\n%s", xid, msg)

            msg_template, (tg_dest, thread_id) = self.get_slave_msg_dest(msg)

            silent = self.is_silent(msg)
            if silent is None:
                self.logger.debug("[%s] Message is not delivered per silent settings.", xid)
                return msg

            if tg_dest is None:
                self.logger.debug("[%s] Sender of the message is muted.", xid)
                return msg

            # When editing message
            old_msg_id: Optional[OldMsgID] = None
            _edit_sender_bot_id: Optional[str] = None
            if msg.edit:
                old_msg = self.db.get_msg_log(slave_msg_id=msg.uid,
                                              slave_origin_uid=utils.chat_id_to_str(chat=msg.chat))
                if old_msg:
                    _edit_sender_bot_id = old_msg.sender_bot_id

                    if old_msg.master_msg_id_alt:
                        old_msg_id = utils.message_id_str_to_id(old_msg.master_msg_id_alt)
                    else:
                        old_msg_id = utils.message_id_str_to_id(old_msg.master_msg_id)
                else:
                    self.logger.info('[%s] Was supposed to edit this message, '
                                     'but it does not exist in database. Sending new message instead.',
                                     msg.uid)

            if _edit_sender_bot_id:
                msg.vendor_specific = msg.vendor_specific or {}
                msg.vendor_specific['_sender_bot_id'] = _edit_sender_bot_id

            self.dispatch_message(msg, msg_template, old_msg_id, tg_dest, thread_id, silent)
        except Exception as e:
            if isinstance(e, telegram.error.BadRequest) and e.message:
                if "Topic" in e.message:
                    try:
                        self.bot.reopen_forum_topic(
                            chat_id=tg_dest,
                            message_thread_id=thread_id
                        )
                    except telegram.error.BadRequest as reopen_err:
                        self.logger.error('Failed to reopen topic, Reason: %s', reopen_err)
                        self.db.remove_topic_assoc(
                            topic_chat_id=tg_dest,
                            message_thread_id=thread_id,
                        )
            else:
                self.logger.error("Error occurred while processing message from slave channel.\nMessage: %s\n%s\n%s",
                              repr(msg), repr(e), traceback.format_exc())
        return msg

    def dispatch_message(self, msg: Message, msg_template: str,
                         old_msg_id: Optional[OldMsgID],
                         tg_dest: TelegramChatID,
                         thread_id: Optional[TelegramTopicID],
                         silent: bool = False):
        """Dispatch with header, destination and Telegram message ID and destinations."""

        xid = msg.uid

        # When targeting a message (reply to)
        target_msg_id: Optional[TelegramMessageID] = None
        if isinstance(msg.target, Message):
            self.logger.debug("[%s] Message is replying to %s.", msg.uid, msg.target)
            log = self.db.get_msg_log(
                slave_msg_id=msg.target.uid,
                slave_origin_uid=utils.chat_id_to_str(chat=msg.target.chat)
            )
            if not log:
                self.logger.debug("[%s] Target message %s is not found in database.", msg.uid, msg.target)
            else:
                self.logger.debug("[%s] Target message has database entry: %s.", msg.uid, log)
                target_msg = utils.message_id_str_to_id(log.master_msg_id)
                if not target_msg or target_msg[0] != int(tg_dest):
                    self.logger.error('[%s] Trying to reply to a message not from this chat. '
                                      'Message destination: %s. Target message: %s.',
                                      msg.uid, tg_dest, target_msg)
                    target_msg_id = None
                else:
                    target_msg_id = target_msg[1]

        # Generate basic reply markup
        commands: Optional[List[MessageCommand]] = None
        reply_markup: Optional[InlineKeyboardMarkup] = None

        if msg.commands:
            commands = msg.commands
            buttons = []
            for idx, i in enumerate(commands):
                buttons.append([InlineKeyboardButton(i.name, callback_data=str(idx))])
            reply_markup = InlineKeyboardMarkup(buttons)

        reactions = self.build_reactions_footer(msg.reactions)

        msg.text = msg.text or ""

        # Type dispatching
        if msg.type == MsgType.Text:
            tg_msg = self.slave_message_text(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                             reply_markup, silent)
        elif msg.type == MsgType.Link:
            tg_msg = self.slave_message_link(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                             reply_markup, silent)
        elif msg.type == MsgType.Sticker:
            tg_msg = self.slave_message_sticker(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                                reply_markup, silent)
        elif msg.type == MsgType.Image:
            if self.flag("send_image_as_file"):
                tg_msg = self.slave_message_file(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                                 reply_markup, silent)
            else:
                tg_msg = self.slave_message_image(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                                  reply_markup, silent)
        elif msg.type == MsgType.Animation:
            tg_msg = self.slave_message_animation(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                                  reply_markup, silent)
        elif msg.type == MsgType.File:
            tg_msg = self.slave_message_file(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                             reply_markup, silent)
        elif msg.type == MsgType.Voice:
            tg_msg = self.slave_message_voice(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                              reply_markup, silent)
        elif msg.type == MsgType.Location:
            tg_msg = self.slave_message_location(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                                 reply_markup, silent)
        elif msg.type == MsgType.Video:
            tg_msg = self.slave_message_video(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id,
                                              reply_markup, silent)
        elif msg.type == MsgType.Status:
            # Status messages are not to be recorded in databases
            return self.slave_message_status(msg, tg_dest, thread_id)
        elif msg.type == MsgType.Unsupported:
            tg_msg = self.slave_message_unsupported(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id,
                                                    target_msg_id, reply_markup, silent)
        else:
            self.bot.send_chat_action(tg_dest, ChatAction.TYPING, message_thread_id=thread_id)
            tg_msg = self.bot.send_message(tg_dest, prefix=msg_template, suffix=reactions,
                                           disable_notification=silent,
                                           message_thread_id=thread_id,
                                           text=self._('Unknown type of message "{0}". (UT01)')
                                           .format(msg.type.name))

        if tg_msg and commands:
            self.channel.commands.register_command(tg_msg, ETMCommandMsgStorage(
                commands, coordinator.get_module_by_id(msg.author.module_id), msg_template, msg.text
            ))

        if tg_msg is None:
            self.logger.warning("[%s] Message sending returned None, skipping database logging. "
                               "This may happen during shutdown or when Telegram API is unavailable.", xid)
            return

        if hasattr(tg_msg, '_delayed_execution_pending') and tg_msg._delayed_execution_pending:
            self.logger.debug("[%s] Message execution is delayed (task_id: %s), deferring database logging.",
                             xid, getattr(tg_msg, 'task_id', 'unknown'))

            etm_msg = ETMMsg.from_efbmsg(msg, self.chat_manager)

            if hasattr(tg_msg, 'task_id'):
                self.bot.register_delayed_database_update(tg_msg.task_id, etm_msg, old_msg_id)
            else:
                self.logger.warning("[%s] Delayed message missing task_id, cannot register database update", xid)
        else:
            # Normal (blocking) execution — send already succeeded,
            # hand off DB write asynchronously so the calling thread is
            # not blocked by local I/O, and failures are retried instead
            # of silently dropped.
            self.logger.debug("[%s] Message is sent to the user with telegram message id %s.%s.",
                              xid, tg_msg.chat.id, tg_msg.message_id)

            etm_msg = ETMMsg.from_efbmsg(msg, self.chat_manager)

            sender_bot_id = getattr(tg_msg, 'sender_bot_id', None)

            self.bot.submit_async_db_write(
                etm_msg, tg_msg, old_msg_id, sender_bot_id=sender_bot_id,
            )

    def get_slave_msg_dest(self, msg: Message) -> Tuple[str, Tuple[Optional[TelegramChatID], Optional[TelegramTopicID]]]:
        """Get the Telegram destination of a message with its header.

        Returns:
            msg_template (str): header of the message.
            (Optional[TelegramChatID], Optional[TelegramTopicID]): Telegram destination chat ID and thread ID, None if muted.
        """
        xid = msg.uid
        chat = self.chat_manager.update_chat_obj(msg.chat)
        msg.chat = chat
        msg.author = self.chat_manager.get_or_enrol_member(msg.chat, msg.author)

        chat_uid = utils.chat_id_to_str(chat=msg.chat)
        tg_chats = self.db.get_chat_assoc(slave_uid=chat_uid)
        tg_chat = None
        tg_dest: Optional[TelegramChatID] = None
        thread_id: Optional[TelegramTopicID] = None

        if tg_chats:
            tg_chat = tg_chats[0]
        self.logger.debug("[%s] The message should deliver to %s", xid, tg_chat)

        singly_linked = True
        if tg_chat:
            slaves = self.db.get_chat_assoc(master_uid=tg_chat)
            if slaves and len(slaves) > 1:
                singly_linked = False
                self.logger.debug("[%s] Sender is linked with other chats in a Telegram group.", xid)
        self.logger.debug("[%s] Message is in chat %s", xid, msg.chat)

        # Generate chat text template & Decide type target
        tg_dest = TelegramChatID(self.channel.config['admins'][0])

        if tg_chat:
            tg_dest = TelegramChatID(int(utils.chat_id_str_to_id(tg_chat)[1]))
        if self.channel.topic_group:
            if not isinstance(chat, SystemChat):
                tg_dest = TelegramChatID(int(utils.chat_id_str_to_id(tg_chat)[1]) if tg_chat else self.channel.topic_group)
                master_chat_info = self.bot.get_chat_info(tg_dest)
                if master_chat_info.is_forum:
                    existing_thread_id = self.db.get_topic_thread_id(slave_uid=chat_uid, topic_chat_id=tg_dest)
                    thread_id = self.channel.chat_binding.create_topic(slave_uid=chat_uid, telegram_chat_id=tg_dest)
                    if thread_id is not None and existing_thread_id is None:
                        msg.vendor_specific = msg.vendor_specific or {}
                        msg.vendor_specific['_force_send_mode'] = 'blocking'

        if not tg_chat:
            singly_linked = False
        if thread_id:
            singly_linked = True

        msg_template = self.generate_message_template(msg, singly_linked)
        self.logger.debug("[%s] Message is sent to Telegram chat %s, with header \"%s\".",
                          xid, tg_dest, msg_template)

        if self.chat_dest_cache.get(str(tg_dest)) != chat_uid:
            self.chat_dest_cache.remove(str(tg_dest))

        return msg_template, (tg_dest, thread_id)


    def html_substitutions(self, msg: Message) -> str:
        """Build a Telegram-flavored HTML string for message text substitutions."""
        text = msg.text
        if msg.substitutions:
            ranges = sorted(msg.substitutions.keys())
            t = ""
            prev = 0
            for i in ranges:
                t += html.escape(text[prev:i[0]])
                sub_chat = msg.substitutions[i]
                if isinstance(sub_chat, SelfChatMember) or (isinstance(sub_chat, Chat) and sub_chat.has_self):
                    t += f'<a href="tg://user?id={self.channel.config["admins"][0]}">'
                    t += html.escape(text[i[0]:i[1]])
                    t += "</a>"
                else:
                    t += '<code>'
                    t += html.escape(text[i[0]:i[1]])
                    t += '</code>'
                prev = i[1]
            t += html.escape(text[prev:])
            return t
        elif text:
            return html.escape(text)
        return text

    @staticmethod
    def _send_mode(msg: Message,
                   old_msg_id: Optional[OldMsgID],
                   thread_id: Optional[TelegramTopicID] = None) -> str:
        force_send_mode = (msg.vendor_specific or {}).get('_force_send_mode')
        if force_send_mode in {'blocking', 'eventual'}:
            return force_send_mode
        if old_msg_id is not None or msg.commands:
            return 'blocking'
        return 'eventual'

    @classmethod
    def _remote_image_url(cls, msg: Message) -> Optional[str]:
        url = (msg.vendor_specific or {}).get(cls.REMOTE_IMAGE_URL_VENDOR_KEY)
        if not isinstance(url, str):
            return None
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return url
        return None

    @staticmethod
    def _remote_image_filename(msg: Message, remote_image_url: str) -> str:
        if msg.filename:
            return msg.filename
        parsed = urllib.parse.urlparse(remote_image_url)
        parsed_path = parsed.path
        if parsed.params:
            parsed_path = f"{parsed_path};{parsed.params}"
        return urllib.parse.unquote(os.path.basename(parsed_path)) or "image"

    def _remote_image_placeholder(self) -> IO[bytes]:
        placeholder = tempfile.NamedTemporaryFile(
            suffix=".png",
            dir=utils.ExperimentalFlagsManager.get_temp_dir(self.channel),
        )
        Image.new("RGB", (64, 64), (245, 245, 245)).save(placeholder, "PNG")
        placeholder.seek(0)
        return placeholder

    def _send_remote_image_placeholder(self, tg_dest: TelegramChatID,
                                       thread_id: Optional[TelegramTopicID],
                                       msg_template: str,
                                       reactions: str,
                                       text: str,
                                       target_msg_id: Optional[TelegramMessageID],
                                       reply_markup: Optional[ReplyMarkup],
                                       silent: bool,
                                       *,
                                       as_document: bool = False) -> telegram.Message:
        placeholder = self._remote_image_placeholder()
        filename = "remote-image-placeholder.png"
        try:
            file = self.process_file_obj(placeholder, placeholder.name, filename)
            if as_document:
                return self.bot.send_document(tg_dest, file, prefix=msg_template, suffix=reactions,
                                              caption=text, parse_mode="HTML", filename=filename,
                                              reply_to_message_id=target_msg_id,
                                              message_thread_id=thread_id,
                                              reply_markup=reply_markup,
                                              disable_notification=silent,
                                              _send_mode="blocking")
            return self.bot.send_photo(tg_dest, file, prefix=msg_template, suffix=reactions,
                                       caption=text, parse_mode="HTML",
                                       reply_to_message_id=target_msg_id,
                                       message_thread_id=thread_id,
                                       reply_markup=reply_markup,
                                       disable_notification=silent,
                                       _send_mode="blocking")
        finally:
            placeholder.close()
            self._cleanup_pending_local_api_files()

    def slave_message_text(self, msg: Message, tg_dest: TelegramChatID,
                           thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                           old_msg_id: Optional[OldMsgID] = None,
                           target_msg_id: Optional[TelegramMessageID] = None,
                           reply_markup: Optional[ReplyMarkup] = None,
                           silent: bool = False) -> telegram.Message:
        """
        Send message as text to Telegram.

        Args:
            msg (Message): Message
            tg_dest (TelegramChatID): Telegram Chat ID
            thread_id (Optional[TelegramTopicID]): Telegram Thread ID
            msg_template: Header of the message
            reactions: Footer of the message
            old_msg_id: Telegram message ID to edit
            target_msg_id: Telegram message ID to reply to
            reply_markup: Reply markup to be added to the message
            silent: Silent notification of the message when sending
        Returns:
            The telegram bot message object sent
        """
        self.logger.debug("[%s] Sending as a text message.", msg.uid)
        self.bot.send_chat_action(tg_dest, ChatAction.TYPING, message_thread_id=thread_id)

        text = self.html_substitutions(msg)

        _sender_bot_id = (msg.vendor_specific or {}).get('_sender_bot_id')

        if not old_msg_id:
            tg_msg = self.bot.send_message(tg_dest,
                                           text=text, prefix=msg_template, suffix=reactions,
                                           parse_mode='HTML',
                                           reply_to_message_id=target_msg_id,
                                           message_thread_id=thread_id,
                                           reply_markup=reply_markup,
                                           disable_notification=silent,
                                           _send_mode=self._send_mode(msg, old_msg_id, thread_id))
        else:
            # Cannot change reply_to_message_id when editing a message
            edit_kwargs = dict(chat_id=old_msg_id[0],
                               message_id=old_msg_id[1],
                               text=text, prefix=msg_template, suffix=reactions,
                               parse_mode='HTML',
                               reply_markup=reply_markup)
            if _sender_bot_id:
                edit_kwargs['_sender_bot_id'] = _sender_bot_id
            tg_msg = self.bot.edit_message_text(**edit_kwargs)

        self.logger.debug("[%s] Processed and sent as text message", msg.uid)
        return tg_msg

    def slave_message_link(self, msg: Message, tg_dest: TelegramChatID,
                           thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                           old_msg_id: Optional[OldMsgID] = None,
                           target_msg_id: Optional[TelegramMessageID] = None,
                           reply_markup: Optional[ReplyMarkup] = None,
                           silent: bool = False) -> telegram.Message:
        self.bot.send_chat_action(tg_dest, ChatAction.TYPING, message_thread_id=thread_id)

        assert isinstance(msg.attributes, LinkAttribute)
        attributes: LinkAttribute = msg.attributes

        thumbnail = urllib.parse.quote(attributes.image or "", safe="?=&#:/")
        thumbnail = "<a href=\"%s\">🔗</a>" % thumbnail if thumbnail else "🔗"
        text = "%s <a href=\"%s\">%s</a>\n%s" % \
               (thumbnail,
                urllib.parse.quote(attributes.url, safe="?=&#:/"),
                html.escape(attributes.title or attributes.url),
                html.escape(attributes.description or ""))

        if msg.text:
            text += "\n\n" + self.html_substitutions(msg)
        if old_msg_id:
            _sender_bot_id = (msg.vendor_specific or {}).get('_sender_bot_id')
            edit_kwargs = dict(text=text, chat_id=old_msg_id[0], message_id=old_msg_id[1],
                               prefix=msg_template, suffix=reactions, parse_mode='HTML',
                               reply_markup=reply_markup)
            if _sender_bot_id:
                edit_kwargs['_sender_bot_id'] = _sender_bot_id
            return self.bot.edit_message_text(**edit_kwargs)
        else:
            return self.bot.send_message(chat_id=tg_dest,
                                         text=text,
                                         prefix=msg_template, suffix=reactions,
                                         parse_mode="HTML",
                                         reply_to_message_id=target_msg_id,
                                         message_thread_id=thread_id,
                                         reply_markup=reply_markup,
                                         disable_notification=silent,
                                         _send_mode=self._send_mode(msg, old_msg_id, thread_id))

    # Parameters to decide when to pictures as files
    IMG_MIN_SIZE = 1600
    """Threshold of dimension of the shorter side to send as file."""
    IMG_MAX_SIZE = 1200
    """Threshold of dimension of the longer side to send as file, used along with IMG_SIZE_RATIO."""
    IMG_SIZE_RATIO = 3.5
    """Threshold of aspect ratio (longer side to shorter side) to send as file, used along with IMG_SIZE_RATIO."""
    IMG_SIZE_MAX_RATIO = 10
    """Threshold of aspect ratio (longer side to shorter side) to send as file, used alone."""

    def slave_message_image(self, msg: Message, tg_dest: TelegramChatID,
                            thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                            old_msg_id: Optional[OldMsgID] = None,
                            target_msg_id: Optional[TelegramMessageID] = None,
                            reply_markup: Optional[ReplyMarkup] = None,
                            silent: bool = False) -> telegram.Message:
        remote_image_url = self._remote_image_url(msg)
        if not remote_image_url:
            assert msg.file
        self.bot.send_chat_action(tg_dest, ChatAction.UPLOAD_PHOTO, message_thread_id=thread_id)
        self.logger.debug("[%s] Message is of %s type; Path: %s; MIME: %s", msg.uid, msg.type, msg.path, msg.mime)
        if msg.path:
            self.logger.debug("[%s] Size of %s is %s.", msg.uid, msg.path, os.stat(msg.path).st_size)

        if msg.text:
            text = self.html_substitutions(msg)
        elif msg_template:
            placeholder_flag = self.flag("default_media_prompt")
            if placeholder_flag == "emoji":
                text = "🖼️"
            elif placeholder_flag == "text":
                text = self._("Sent a picture.")
            else:
                text = ""
        else:
            text = ""

        if remote_image_url:
            if old_msg_id:
                try:
                    edit_kwargs = self._get_edit_kwargs(msg)
                    if msg.edit_media:
                        res = self.bot.edit_message_media(
                            chat_id=old_msg_id[0],
                            message_id=old_msg_id[1],
                            media=InputMediaPhoto(remote_image_url),
                            reply_markup=reply_markup,
                            **edit_kwargs
                        )
                        if not text:
                            return res
                    return self.bot.edit_message_caption(chat_id=old_msg_id[0], message_id=old_msg_id[1],
                                                         reply_markup=reply_markup,
                                                         prefix=msg_template, suffix=reactions, caption=text,
                                                         parse_mode="HTML", **edit_kwargs)
                except telegram.error.BadRequest as e:
                    self.logger.warning("[%s] Failed to edit remote image/caption (BadRequest: %s). "
                                        "Sending new message instead.", msg.uid, e)
                    if old_msg_id[0] == tg_dest:
                        target_msg_id = target_msg_id or old_msg_id[1]

            try:
                return self.bot.send_photo(tg_dest, remote_image_url, prefix=msg_template, suffix=reactions,
                                           caption=text, parse_mode="HTML",
                                           reply_to_message_id=target_msg_id,
                                           message_thread_id=thread_id,
                                           reply_markup=reply_markup,
                                           disable_notification=silent,
                                           _send_mode="blocking")
            except telegram.error.BadRequest as e:
                self.logger.warning('[%s] Failed to send remote image URL, sending editable placeholder. Reason: %s',
                                    msg.uid, e)
                return self._send_remote_image_placeholder(tg_dest, thread_id, msg_template, reactions, text,
                                                           target_msg_id, reply_markup, silent)

        try:
            # Avoid Telegram compression of pictures by sending high definition image messages as files
            # Code adopted from wolfsilver's fork:
            # https://github.com/wolfsilver/efb-telegram-master/blob/99668b60f7ff7b6363dfc87751a18281d9a74a09/efb_telegram_master/slave_message.py#L142-L163
            #
            # Rules:
            # 1. If the picture is too large -- shorter side is greater than IMG_MIN_SIZE, send as file.
            # 2. If the picture is large and thin --
            #        longer side is greater than IMG_MAX_SIZE, and
            #        aspect ratio (longer to shorter side ratio) is greater than IMG_SIZE_RATIO,
            #    send as file.
            # 3. If the picture is too thin -- aspect ratio grater than IMG_SIZE_MAX_RATIO, send as file.

            try:
                if msg.path is None:
                    # When we don't have a local file path (e.g. file-like only),
                    # skip the heuristic and default to sending as photo.
                    send_as_file = False
                else:
                    pic_img = Image.open(msg.path)
                    max_size = max(pic_img.size)
                    min_size = min(pic_img.size)
                    img_ratio = max_size / min_size

                    if min_size > self.IMG_MIN_SIZE:
                        send_as_file = True
                    elif max_size > self.IMG_MAX_SIZE and img_ratio > self.IMG_SIZE_RATIO:
                        send_as_file = True
                    elif img_ratio >= self.IMG_SIZE_MAX_RATIO:
                        send_as_file = True
                    else:
                        send_as_file = False
            except IOError:  # Ignore when the image cannot be properly identified.
                send_as_file = False

            file_too_large = self.check_file_size(msg.file)
            edit_media = msg.edit_media
            if file_too_large:
                if old_msg_id:
                    if msg.edit_media:
                        edit_media = False
                    self.bot.send_message(chat_id=old_msg_id[0], reply_to_message_id=old_msg_id[1], text=file_too_large)
                else:
                    message = self.bot.send_message(chat_id=tg_dest, reply_to_message_id=target_msg_id,
                                                    message_thread_id=thread_id, text=text,
                                                    parse_mode="HTML", reply_markup=reply_markup, disable_notification=silent,
                                                    prefix=msg_template, suffix=reactions)
                    message.reply_text(file_too_large)
                    return message

            if old_msg_id:
                try:
                    edit_kwargs = self._get_edit_kwargs(msg)
                    if edit_media:
                        assert msg.path
                        media: InputMedia
                        file = self.process_file_obj(msg.file, msg.path, msg.filename)
                        if send_as_file:
                            media = InputMediaDocument(file)
                        else:
                            media = InputMediaPhoto(file)
                        res = self.bot.edit_message_media(chat_id=old_msg_id[0], message_id=old_msg_id[1], media=media,
                                                    reply_markup=reply_markup, **edit_kwargs)
                        if not text:
                            return res
                    return self.bot.edit_message_caption(chat_id=old_msg_id[0], message_id=old_msg_id[1],
                                                         reply_markup=reply_markup,
                                                         prefix=msg_template, suffix=reactions, caption=text, parse_mode="HTML",
                                                         **edit_kwargs)
                except telegram.error.BadRequest as e:
                    self.logger.warning("[%s] Failed to edit media/caption (BadRequest: %s). Sending new message instead.", msg.uid, e)
                    if old_msg_id[0] == tg_dest:
                        target_msg_id = target_msg_id or old_msg_id[1]
                    msg.file.seek(0)

            if send_as_file:
                assert msg.path
                file = self.process_file_obj(msg.file, msg.path, msg.filename)
                return self.bot.send_document(tg_dest, file, prefix=msg_template, suffix=reactions,
                                              caption=text, parse_mode="HTML", filename=msg.filename,
                                              reply_to_message_id=target_msg_id,
                                              message_thread_id=thread_id,
                                              reply_markup=reply_markup,
                                              disable_notification=silent,
                                              _send_mode=self._send_mode(msg, old_msg_id, thread_id))
            else:
                try:
                    assert msg.path
                    file = self.process_file_obj(msg.file, msg.path, msg.filename)
                    return self.bot.send_photo(tg_dest, file, prefix=msg_template, suffix=reactions,
                                               caption=text, parse_mode="HTML",
                                               reply_to_message_id=target_msg_id,
                                               message_thread_id=thread_id,
                                               reply_markup=reply_markup,
                                               disable_notification=silent,
                                               _send_mode=self._send_mode(msg, old_msg_id, thread_id))
                except telegram.error.BadRequest as e:
                    self.logger.error('[%s] Failed to send it as image, sending as document. Reason: %s',
                                      msg.uid, e)
                    assert msg.path
                    msg.file.seek(0)
                    file = self.process_file_obj(msg.file, msg.path, msg.filename)
                    return self.bot.send_document(tg_dest, file, prefix=msg_template, suffix=reactions,
                                                  caption=text, parse_mode="HTML", filename=msg.filename,
                                                  reply_to_message_id=target_msg_id,
                                                  message_thread_id=thread_id,
                                                  reply_markup=reply_markup,
                                                  disable_notification=silent,
                                                  _send_mode=self._send_mode(msg, old_msg_id, thread_id))
        finally:
            if msg.file:
                msg.file.close()
            self._cleanup_pending_local_api_files()

    def slave_message_animation(self, msg: Message, tg_dest: TelegramChatID,
                                thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                                old_msg_id: Optional[OldMsgID] = None,
                                target_msg_id: Optional[TelegramMessageID] = None,
                                reply_markup: Optional[ReplyMarkup] = None,
                                silent: Optional[bool] = None) -> telegram.Message:
        self.bot.send_chat_action(tg_dest, ChatAction.UPLOAD_PHOTO, message_thread_id=thread_id)

        self.logger.debug("[%s] Message is an Animation; Path: %s; MIME: %s", msg.uid, msg.path, msg.mime)
        if msg.path:
            self.logger.debug("[%s] Size of %s is %s.", msg.uid, msg.path, os.stat(msg.path).st_size)

        if msg.text:
            text = self.html_substitutions(msg)
        else:
            text = ""

        try:
            file_too_large = self.check_file_size(msg.file)
            edit_media = msg.edit_media
            if file_too_large:
                if old_msg_id:
                    if msg.edit_media:
                        edit_media = False
                    self.bot.send_message(chat_id=old_msg_id[0], reply_to_message_id=old_msg_id[1], text=file_too_large)
                else:
                    message = self.bot.send_message(chat_id=tg_dest, reply_to_message_id=target_msg_id,
                                                    message_thread_id=thread_id, text=text,
                                                    parse_mode="HTML", reply_markup=reply_markup,
                                                    disable_notification=silent,
                                                    prefix=msg_template, suffix=reactions)
                    message.reply_text(file_too_large)
                    return message

            if old_msg_id:
                edit_kwargs = self._get_edit_kwargs(msg)
                if edit_media:
                    assert msg.file and msg.path
                    file = self.process_file_obj(msg.file, msg.path, msg.filename)
                    res = self.bot.edit_message_media(chat_id=old_msg_id[0], message_id=old_msg_id[1], media=InputMediaAnimation(file),
                                                reply_markup=reply_markup, **edit_kwargs)
                    if not text:
                        return res
                return self.bot.edit_message_caption(chat_id=old_msg_id[0], message_id=old_msg_id[1],
                                                     prefix=msg_template, suffix=reactions,
                                                     reply_markup=reply_markup,
                                                     caption=text, parse_mode="HTML", **edit_kwargs)
            else:
                assert msg.file and msg.path
                file = self.process_file_obj(msg.file, msg.path, msg.filename)
                anim_file = file if isinstance(file, str) else InputFile(file, filename=msg.filename or "")
                return self.bot.send_animation(tg_dest, anim_file,
                                               prefix=msg_template, suffix=reactions,
                                               caption=text, parse_mode="HTML",
                                               reply_to_message_id=target_msg_id,
                                               message_thread_id=thread_id,
                                               reply_markup=reply_markup,
                                               disable_notification=silent,
                                               _send_mode=self._send_mode(msg, old_msg_id, thread_id))
        finally:
            if msg.file is not None:
                msg.file.close()
            self._cleanup_pending_local_api_files()

    def slave_message_sticker(self, msg: Message, tg_dest: TelegramChatID,
                              thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                              old_msg_id: Optional[OldMsgID] = None,
                              target_msg_id: Optional[TelegramMessageID] = None,
                              reply_markup: Optional[InlineKeyboardMarkup] = None,
                              silent: bool = False) -> telegram.Message:

        self.bot.send_chat_action(tg_dest, ChatAction.UPLOAD_PHOTO, message_thread_id=thread_id)

        sticker_reply_markup = self.build_chat_info_inline_keyboard(msg, msg_template, reactions, reply_markup)

        self.logger.debug("[%s] Message is of %s type; Path: %s; MIME: %s", msg.uid, msg.type, msg.path, msg.mime)
        if msg.path:
            self.logger.debug("[%s] Size of %s is %s.", msg.uid, msg.path, os.stat(msg.path).st_size)

        try:
            if msg.edit_media and old_msg_id is not None:
                if old_msg_id[0] == str(tg_dest):
                    target_msg_id = old_msg_id[1]
                old_msg_id = None

            if old_msg_id and not msg.edit_media:
                try:
                    _sender_bot_id = (msg.vendor_specific or {}).get('_sender_bot_id')
                    edit_kwargs = dict(chat_id=old_msg_id[0], message_id=old_msg_id[1],
                                       reply_markup=sticker_reply_markup)
                    if _sender_bot_id:
                        edit_kwargs['_sender_bot_id'] = _sender_bot_id
                    return self.bot.edit_message_reply_markup(**edit_kwargs)
                except TelegramError:
                    return self.bot.send_message(chat_id=old_msg_id[0], reply_to_message_id=old_msg_id[1],
                                                 prefix=msg_template, text=msg.text, suffix=reactions,
                                                 reply_markup=reply_markup,
                                                 disable_notification=silent)

            else:
                webp_img = None

                file_too_large = self.check_file_size(msg.file)
                if file_too_large:
                    if old_msg_id:
                        self.bot.send_message(chat_id=old_msg_id[0], reply_to_message_id=old_msg_id[1],
                                              text=file_too_large)
                    else:
                        # Send placeholder text first
                        message = self.bot.send_message(chat_id=tg_dest, reply_to_message_id=target_msg_id,
                                                        message_thread_id=thread_id,
                                                        text=self.html_substitutions(msg),
                                                        parse_mode="HTML", reply_markup=reply_markup,
                                                        disable_notification=silent,
                                                        prefix=msg_template, suffix=reactions)
                        message.reply_text(file_too_large)
                        return message

                try:
                    assert msg.file is not None
                    pic_img: Image.Image = Image.open(msg.file)
                    webp_img = tempfile.NamedTemporaryFile(suffix='.webp', dir=utils.ExperimentalFlagsManager.get_temp_dir(self.channel))
                    pic_img.convert("RGBA").save(webp_img, 'webp')
                    webp_img.seek(0)
                    file = self.process_file_obj(webp_img, webp_img.name, msg.filename)
                    return self.bot.send_sticker(tg_dest, file, reply_markup=sticker_reply_markup,
                                                 message_thread_id=thread_id,
                                                 reply_to_message_id=target_msg_id,
                                                 disable_notification=silent,
                                                 _send_mode=self._send_mode(msg, old_msg_id, thread_id))
                except IOError:
                    self.logger.warning("[%s] Failed to convert image to webp sticker, sending as document.", msg.uid)
                    assert msg.file and msg.path
                    file = self.process_file_obj(msg.file, msg.path, msg.filename)
                    return self.bot.send_document(tg_dest, file, prefix=msg_template, suffix=reactions,
                                                  message_thread_id=thread_id,
                                                  caption=msg.text, filename=msg.filename,
                                                  reply_to_message_id=target_msg_id,
                                                  reply_markup=reply_markup,
                                                  disable_notification=silent,
                                                  _send_mode=self._send_mode(msg, old_msg_id, thread_id))
                finally:
                    if webp_img and not webp_img.closed:
                        webp_img.close()
        finally:
            if msg.file and not msg.file.closed:
                msg.file.close()
            self._cleanup_pending_local_api_files()

    @staticmethod
    def build_chat_info_inline_keyboard(msg: Message, msg_template: str, reactions: str,
                                        reply_markup: Optional[InlineKeyboardMarkup]
                                        ) -> InlineKeyboardMarkup:
        """
        Build inline keyboard markup with message header and footer (reactions). Buttons are attached
        before any other commands attached.
        """
        description = []
        if msg_template:
            description.append([InlineKeyboardButton(msg_template, callback_data="void")])
        if msg.text:
            description.append([InlineKeyboardButton(msg.text, callback_data="void")])
        if reactions:
            description.append([InlineKeyboardButton(reactions, callback_data="void")])
        effective_reply_markup = reply_markup if isinstance(reply_markup, InlineKeyboardMarkup) else InlineKeyboardMarkup([])
        existing_rows = [list(row) for row in effective_reply_markup.inline_keyboard]
        return InlineKeyboardMarkup(description + existing_rows)


    def slave_message_file(self, msg: Message, tg_dest: TelegramChatID,
                           thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                           old_msg_id: Optional[OldMsgID] = None,
                           target_msg_id: Optional[TelegramMessageID] = None,
                           reply_markup: Optional[ReplyMarkup] = None,
                           silent: bool = False) -> telegram.Message:
        self.bot.send_chat_action(tg_dest, ChatAction.UPLOAD_DOCUMENT, message_thread_id=thread_id)

        remote_image_url = self._remote_image_url(msg) if msg.type == MsgType.Image else None
        if remote_image_url:
            file_name = self._remote_image_filename(msg, remote_image_url)
        elif msg.filename is None and msg.path is not None:
            file_name = os.path.basename(msg.path)
        else:
            assert msg.filename is not None  # mypy compliance
            file_name = msg.filename

        # Telegram Bot API drops everything after `;` in filenames
        # Replace it with a space
        # Note: it also seems to strip off a lot of unicode punctuations
        file_name = file_name.replace(';', ' ')

        if msg.text:
            text = self.html_substitutions(msg)
        elif msg_template:
            placeholder_flag = self.flag("default_media_prompt")
            if placeholder_flag == "emoji":
                text = "📄"
            elif placeholder_flag == "text":
                text = self._("Sent a file.")
            else:
                text = ""
        else:
            text = ""

        try:
            if remote_image_url:
                if old_msg_id:
                    edit_kwargs = self._get_edit_kwargs(msg)
                    if msg.edit_media:
                        res = self.bot.edit_message_media(
                            chat_id=old_msg_id[0],
                            message_id=old_msg_id[1],
                            media=InputMediaDocument(remote_image_url),
                            **edit_kwargs
                        )
                        if not text:
                            return res
                    return self.bot.edit_message_caption(chat_id=old_msg_id[0], message_id=old_msg_id[1],
                                                         reply_markup=reply_markup,
                                                         prefix=msg_template, suffix=reactions, caption=text,
                                                         parse_mode="HTML", **edit_kwargs)
                try:
                    return self.bot.send_document(tg_dest, remote_image_url,
                                                  prefix=msg_template, suffix=reactions,
                                                  caption=text, parse_mode="HTML", filename=file_name,
                                                  reply_to_message_id=target_msg_id,
                                                  message_thread_id=thread_id,
                                                  reply_markup=reply_markup,
                                                  disable_notification=silent,
                                                  _send_mode="blocking")
                except telegram.error.BadRequest as e:
                    self.logger.warning('[%s] Failed to send remote image URL as document, sending editable placeholder. '
                                        'Reason: %s', msg.uid, e)
                    return self._send_remote_image_placeholder(tg_dest, thread_id, msg_template, reactions, text,
                                                               target_msg_id, reply_markup, silent, as_document=True)

            file_too_large = self.check_file_size(msg.file)
            edit_media = msg.edit_media
            if file_too_large:
                if old_msg_id:
                    if msg.edit_media:
                        edit_media = False
                    self.bot.send_message(chat_id=old_msg_id[0], reply_to_message_id=old_msg_id[1], text=file_too_large)
                else:
                    message = self.bot.send_message(chat_id=tg_dest, reply_to_message_id=target_msg_id,
                                                    message_thread_id=thread_id, text=text,
                                                    parse_mode="HTML", reply_markup=reply_markup,
                                                    disable_notification=silent,
                                                    prefix=msg_template, suffix=reactions)
                    message.reply_text(file_too_large)
                    return message

            if old_msg_id:
                edit_kwargs = self._get_edit_kwargs(msg)
                if edit_media:
                    assert msg.file is not None and msg.path is not None
                    file = self.process_file_obj(msg.file, msg.path, msg.filename)
                    res = self.bot.edit_message_media(chat_id=old_msg_id[0], message_id=old_msg_id[1],
                                                      media=InputMediaDocument(file), **edit_kwargs)
                    if not text:
                        return res
                return self.bot.edit_message_caption(chat_id=old_msg_id[0], message_id=old_msg_id[1], reply_markup=reply_markup,
                                                     prefix=msg_template, suffix=reactions, caption=text, parse_mode="HTML",
                                                     **edit_kwargs)
            assert msg.file is not None and msg.path is not None
            self.logger.debug("[%s] Uploading file %s (%s) as %s", msg.uid,
                              msg.file.name, msg.mime, file_name)
            file = self.process_file_obj(msg.file, msg.path, file_name)
            return self.bot.send_document(tg_dest, file,
                                          prefix=msg_template, suffix=reactions,
                                          caption=text, parse_mode="HTML", filename=file_name,
                                          reply_to_message_id=target_msg_id,
                                          message_thread_id=thread_id,
                                          reply_markup=reply_markup,
                                          disable_notification=silent,
                                          _send_mode=self._send_mode(msg, old_msg_id, thread_id))
        finally:
            if msg.file is not None:
                msg.file.close()
            self._cleanup_pending_local_api_files()

    def slave_message_voice(self, msg: Message, tg_dest: TelegramChatID,
                            thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                            old_msg_id: Optional[OldMsgID] = None,
                            target_msg_id: Optional[TelegramMessageID] = None,
                            reply_markup: Optional[ReplyMarkup] = None,
                            silent: bool = False) -> telegram.Message:
        self.bot.send_chat_action(tg_dest, ChatAction.RECORD_VOICE, message_thread_id=thread_id)
        if msg.text:
            text = self.html_substitutions(msg)
        else:
            text = ""
        self.logger.debug("[%s] Message is a voice file.", msg.uid)
        try:
            file_too_large = self.check_file_size(msg.file)
            edit_media = msg.edit_media
            if file_too_large:
                if old_msg_id:
                    if msg.edit_media:
                        edit_media = False
                    self.bot.send_message(chat_id=old_msg_id[0], reply_to_message_id=old_msg_id[1], text=file_too_large)
                else:
                    message = self.bot.send_message(chat_id=tg_dest, reply_to_message_id=target_msg_id,
                                                    message_thread_id=thread_id, text=text,
                                                    parse_mode="HTML", reply_markup=reply_markup,
                                                    disable_notification=silent,
                                                    prefix=msg_template, suffix=reactions)
                    message.reply_text(file_too_large)
                    return message

            if old_msg_id:
                if edit_media:
                    self.logger.warning("[%s] Cannot edit voice message media. Sending new message instead.", msg.uid)
                    msg_template += " " + self._("[Edited]")
                    if str(tg_dest) == old_msg_id[0]:
                        target_msg_id = target_msg_id or old_msg_id[1]
                    old_msg_id = None
                else:
                    edit_kwargs = self._get_edit_kwargs(msg)
                    return self.bot.edit_message_caption(chat_id=old_msg_id[0], message_id=old_msg_id[1],
                                                         reply_markup=reply_markup, prefix=msg_template,
                                                         suffix=reactions, caption=text, parse_mode="HTML", **edit_kwargs)
            if not old_msg_id:
                assert msg.file is not None
                import pydub

                with tempfile.NamedTemporaryFile(suffix=".ogg", dir=utils.ExperimentalFlagsManager.get_temp_dir(self.channel)) as f:
                    try:
                        pydub.AudioSegment.from_file(msg.file).export(f.name, format="ogg", codec="libopus",
                                                                      parameters=['-vbr', 'on'])
                        processed_path = self.process_file_obj(f, f.name, msg.filename)
                        tg_msg = self.bot.send_voice(tg_dest, processed_path, prefix=msg_template, suffix=reactions,
                                                     caption=text, parse_mode="HTML",
                                                     reply_to_message_id=target_msg_id,
                                                     message_thread_id=thread_id, reply_markup=reply_markup,
                                                     disable_notification=silent,
                                                     _send_mode=self._send_mode(msg, old_msg_id, thread_id))
                        return tg_msg
                    except pydub.exceptions.CouldntDecodeError as e:
                        self.logger.error("[%s] Failed to decode audio file for conversion: %s. Sending as file.", msg.uid, e)
                        msg.file.seek(0)
                        return self.slave_message_file(msg, tg_dest, thread_id, msg_template, reactions,
                                                       old_msg_id=None,
                                                       target_msg_id=target_msg_id, reply_markup=reply_markup, silent=silent)
            raise RuntimeError("Unreachable: voice message send path not entered")
        finally:
            if msg.file is not None:
                msg.file.close()
            self._cleanup_pending_local_api_files()

    def slave_message_location(self, msg: Message, tg_dest: TelegramChatID,
                               thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                               old_msg_id: Optional[OldMsgID] = None,
                               target_msg_id: Optional[TelegramMessageID] = None,
                               reply_markup: Optional[InlineKeyboardMarkup] = None,
                               silent: bool = False) -> telegram.Message:
        # Location messages cannot be edited in content by bots.
        # If an edit request comes, send a new message replying to the old one.
        self.bot.send_chat_action(tg_dest, ChatAction.FIND_LOCATION, message_thread_id=thread_id)
        assert (isinstance(msg.attributes, LocationAttribute))
        attributes: LocationAttribute = msg.attributes
        self.logger.info("[%s] Sending as a Telegram venue.\nlat: %s, long: %s\ntitle: %s\naddress: %s",
                         msg.uid,
                         attributes.latitude, attributes.longitude,
                         msg.text, msg_template)

        self.logger.debug("[%s] Location message received with old_msg_id %s, compare it with tg_dest %s", msg.uid, old_msg_id, tg_dest)
        if old_msg_id and old_msg_id[0] == str(tg_dest):
            # TRANSLATORS: Flag for messages edited on slave channels, but cannot be edited on Telegram.
            msg_template += " " + self._('[edited]')
            target_msg_id = target_msg_id or old_msg_id[1]
            self.logger.debug("[%s] updated target_msg_id %s", msg.uid, target_msg_id)

        location_reply_markup = self.build_chat_info_inline_keyboard(msg, msg_template, reactions, reply_markup)

        # TODO: Use live location if possible? Lift live location messages to EFB Framework?
        return self.bot.send_location(tg_dest, latitude=attributes.latitude,
                                      longitude=attributes.longitude, reply_to_message_id=target_msg_id,
                                      message_thread_id=thread_id,
                                      reply_markup=location_reply_markup,
                                      disable_notification=silent,
                                      _send_mode=self._send_mode(msg, old_msg_id, thread_id))

    def slave_message_video(self, msg: Message, tg_dest: TelegramChatID,
                            thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                            old_msg_id: Optional[OldMsgID] = None,
                            target_msg_id: Optional[TelegramMessageID] = None,
                            reply_markup: Optional[ReplyMarkup] = None,
                            silent: bool = False) -> telegram.Message:
        self.bot.send_chat_action(tg_dest, ChatAction.UPLOAD_VIDEO, message_thread_id=thread_id)
        if msg.text:
            text = self.html_substitutions(msg)
        elif msg_template:
            placeholder_flag = self.flag("default_media_prompt")
            if placeholder_flag == "emoji":
                text = "🎥"
            elif placeholder_flag == "text":
                text = self._("Sent a video.")
            else:
                text = ""
        else:
            text = ""
        try:
            file_too_large = self.check_file_size(msg.file)
            edit_media = msg.edit_media
            if file_too_large:
                if old_msg_id:
                    if msg.edit_media:
                        edit_media = False
                    self.bot.send_message(chat_id=old_msg_id[0], reply_to_message_id=old_msg_id[1], text=file_too_large)
                else:
                    message = self.bot.send_message(chat_id=tg_dest, reply_to_message_id=target_msg_id,
                                                    message_thread_id=thread_id, text=text,
                                                    parse_mode="HTML", reply_markup=reply_markup,
                                                    disable_notification=silent,
                                                    prefix=msg_template, suffix=reactions)
                    message.reply_text(file_too_large)
                    return message

            if old_msg_id:
                edit_kwargs = self._get_edit_kwargs(msg)
                if edit_media:
                    assert msg.file is not None and msg.path is not None
                    file = self.process_file_obj(msg.file, msg.path, msg.filename)
                    res = self.bot.edit_message_media(chat_id=old_msg_id[0], message_id=old_msg_id[1], media=InputMediaVideo(file),
                                                reply_markup=reply_markup, **edit_kwargs)
                    if not text:
                        return res
                return self.bot.edit_message_caption(chat_id=old_msg_id[0], message_id=old_msg_id[1], reply_markup=reply_markup,
                                                     prefix=msg_template, suffix=reactions, caption=text, parse_mode="HTML",
                                                     **edit_kwargs)
            assert msg.file is not None and msg.path is not None
            file = self.process_file_obj(msg.file, msg.path, msg.filename)
            return self.bot.send_video(tg_dest, file, prefix=msg_template, suffix=reactions,
                                       caption=text, parse_mode="HTML",
                                       reply_to_message_id=target_msg_id,
                                       message_thread_id=thread_id,
                                       reply_markup=reply_markup,
                                       disable_notification=silent,
                                       _send_mode=self._send_mode(msg, old_msg_id, thread_id))
        finally:
            if msg.file is not None:
                msg.file.close()
            self._cleanup_pending_local_api_files()

    def slave_message_unsupported(self, msg: Message, tg_dest: TelegramChatID,
                                  thread_id: Optional[TelegramTopicID], msg_template: str, reactions: str,
                                  old_msg_id: Optional[OldMsgID] = None,
                                  target_msg_id: Optional[TelegramMessageID] = None,
                                  reply_markup: Optional[ReplyMarkup] = None,
                                  silent: bool = False) -> telegram.Message:
        self.logger.debug("[%s] Sending as an unsupported message.", msg.uid)
        self.bot.send_chat_action(tg_dest, ChatAction.TYPING, message_thread_id=thread_id)
        if msg.text:
            text = self.html_substitutions(msg)
        else:
            text = ""

        _sender_bot_id = (msg.vendor_specific or {}).get('_sender_bot_id')

        if not old_msg_id:
            tg_msg = self.bot.send_message(tg_dest,
                                           text=text, parse_mode="HTML",
                                           prefix=msg_template + " " + self._("(unsupported)"),
                                           suffix=reactions,
                                           reply_to_message_id=target_msg_id,
                                           message_thread_id=thread_id,
                                           reply_markup=reply_markup,
                                           disable_notification=silent,
                                           _send_mode=self._send_mode(msg, old_msg_id, thread_id))
        else:
            # Cannot change reply_to_message_id or thread_id when editing a message
            edit_kwargs = dict(chat_id=old_msg_id[0],
                               message_id=old_msg_id[1],
                               text=text, parse_mode="HTML",
                               prefix=msg_template + " " + self._("(unsupported) [Edited]"),
                               suffix=reactions,
                               reply_markup=reply_markup)
            if _sender_bot_id:
                edit_kwargs['_sender_bot_id'] = _sender_bot_id
            tg_msg = self.bot.edit_message_text(**edit_kwargs)

        self.logger.debug("[%s] Processed and sent as text message", msg.uid)
        return tg_msg

    def slave_message_status(self, msg: Message, tg_dest: TelegramChatID,
                             thread_id: Optional[TelegramTopicID]):
        attributes = msg.attributes
        assert isinstance(attributes, StatusAttribute)
        if attributes.status_type is StatusAttribute.Types.TYPING:
            self.bot.send_chat_action(tg_dest, ChatAction.TYPING, message_thread_id=thread_id)
        elif attributes.status_type is StatusAttribute.Types.UPLOADING_VOICE:
            self.bot.send_chat_action(tg_dest, ChatAction.RECORD_VOICE, message_thread_id=thread_id)
        elif attributes.status_type is StatusAttribute.Types.UPLOADING_IMAGE:
            self.bot.send_chat_action(tg_dest, ChatAction.UPLOAD_PHOTO, message_thread_id=thread_id)
        elif attributes.status_type is StatusAttribute.Types.UPLOADING_VIDEO:
            self.bot.send_chat_action(tg_dest, ChatAction.UPLOAD_VIDEO, message_thread_id=thread_id)
        elif attributes.status_type is StatusAttribute.Types.UPLOADING_FILE:
            self.bot.send_chat_action(tg_dest, ChatAction.UPLOAD_DOCUMENT, message_thread_id=thread_id)

    def send_status(self, status: Status):
        if isinstance(status, ChatUpdates):
            self.logger.debug("Received chat updates from channel %s", status.channel)
            for i in status.removed_chats:
                self.db.delete_slave_chat_info(status.channel.channel_id, i)
                self.chat_manager.delete_chat_object(status.channel.channel_id, i)
            for i in itertools.chain(status.new_chats, status.modified_chats):
                chat = status.channel.get_chat(i)
                self.chat_manager.update_chat_obj(chat, full_update=True)
        elif isinstance(status, MemberUpdates):
            self.logger.debug("Received member updates from channel %s about group %s",
                              status.channel, status.chat_id)
            for i in status.removed_members:
                self.db.delete_slave_chat_info(status.channel.channel_id, i, status.chat_id)
            self.chat_manager.delete_chat_members(status.channel.channel_id, status.chat_id, status.removed_members)
            chat = status.channel.get_chat(status.chat_id)
            self.chat_manager.update_chat_obj(chat, full_update=True)
        elif isinstance(status, MessageRemoval):
            self.logger.debug("Received message removal request from channel %s on message %s",
                              status.source_channel, status.message)
            old_msg = self.db.get_msg_log(
                slave_msg_id=status.message.uid,
                slave_origin_uid=utils.chat_id_to_str(chat=status.message.chat))
            if old_msg:
                old_msg_id: OldMsgID = utils.message_id_str_to_id(old_msg.master_msg_id_alt or old_msg.master_msg_id)
                self.logger.debug("Found message to delete in Telegram: %s.%s",
                                  *old_msg_id)
                try:
                    if not self.channel.flag('prevent_message_removal'):
                        self.bot.delete_message(*old_msg_id,
                                                _sender_bot_id=old_msg.sender_bot_id)
                        return
                except TelegramError as e:
                    self.logger.warning("Failed to delete message %s.%s: %s. Sending notification instead.", *old_msg_id, e)
                    pass
                self.bot.send_message(chat_id=old_msg_id[0],
                                      text=self._("Message is removed in remote chat."),
                                      reply_to_message_id=old_msg_id[1],
                                      disable_notification=True)
            else:
                self.logger.info('Was supposed to delete a message, '
                                 'but it does not exist in database: %s', status)
        elif isinstance(status, MessageReactionsUpdate):
            self.update_reactions(status)
        else:
            self.logger.error('Received an unsupported type of status: %s', status)

    @staticmethod
    def build_reactions_footer(reactions: Reactions) -> str:
        """Generate a footer string for reactions in the format similar to [🙂×3, ❤️×1].
        Returns '' if no reaction is found.
        """
        result = "[" + ", ".join(f"{k}×{len(v)}" for k, v in reactions.items() if len(v)) + "]"
        if result == "[]":
            return ""
        return result

    @staticmethod
    def _reaction_target_message_id(old_msg: ETMMsg, old_msg_db) -> utils.TgChatMsgIDStr:
        """Choose which Telegram message should surface a reaction update.

        Most slave-originated text/link messages should edit the primary
        Telegram message. Messages mirrored from Telegram user input keep the
        original user message as the primary record and use ``master_msg_id_alt``
        for the editable bot reply, so reactions must target the alternate
        message when it exists.
        """
        if old_msg_db.master_msg_id_alt:
            if old_msg.deliver_to and old_msg.deliver_to.channel_id == old_msg.chat.module_id:
                return old_msg_db.master_msg_id_alt
        if old_msg.type in (MsgType.Text, MsgType.Link):
            return old_msg_db.master_msg_id or old_msg_db.master_msg_id_alt
        return old_msg_db.master_msg_id_alt or old_msg_db.master_msg_id

    def update_reactions(self, status: MessageReactionsUpdate):
        """Update reactions to a Telegram message."""
        slave_origin_uid = utils.chat_id_to_str(chat=status.chat)
        old_msg_db = self.db.get_msg_log(slave_msg_id=status.msg_id,
                                         slave_origin_uid=slave_origin_uid)
        deadline = time.monotonic() + self.REACTION_DB_WAIT_TIMEOUT
        while old_msg_db is None and time.monotonic() < deadline:
            time.sleep(self.REACTION_DB_WAIT_INTERVAL)
            old_msg_db = self.db.get_msg_log(slave_msg_id=status.msg_id,
                                             slave_origin_uid=slave_origin_uid)
        if old_msg_db is None:
            self.logger.error('Trying to update reactions of message, but message is not found in database. '
                              'Message ID %s from %s, status: %s.', status.msg_id, status.chat, status.reactions)
            return

        old_msg: ETMMsg = old_msg_db.build_etm_msg(chat_manager=self.chat_manager)
        old_msg.reactions = status.reactions
        old_msg.edit = True
        old_msg.edit_media = False

        if old_msg_db.sender_bot_id:
            old_msg.vendor_specific = old_msg.vendor_specific or {}
            old_msg.vendor_specific['_sender_bot_id'] = old_msg_db.sender_bot_id

        msg_template, (tg_dest, thread_id) = self.get_slave_msg_dest(old_msg)
        if tg_dest is None:
            self.logger.error('Cannot update reactions for message %s from %s: destination not found.',
                              status.msg_id, status.chat)
            return

        effective_msg = self._reaction_target_message_id(old_msg, old_msg_db)
        chat_id, msg_id = utils.message_id_str_to_id(effective_msg)

        self.dispatch_message(old_msg, msg_template, (chat_id, msg_id), tg_dest, thread_id)

    def generate_message_template(self, msg: Message, singly_linked: bool) -> str:
        msg_prefix = ""  # For group member name
        if isinstance(msg.chat, GroupChat):
            self.logger.debug("[%s] Message is from a group. Sender: %s", msg.uid, msg.author)
            msg_prefix = msg.author.long_name

        if singly_linked:
            if msg_prefix:  # if group message
                msg_template = f"{msg_prefix}:"
            else:
                if msg.chat != msg.author:
                    msg_template = f"{msg.author.long_name}:"
                else:
                    msg_template = ""
        elif isinstance(msg.chat, PrivateChat):
            emoji_prefix = msg.chat.channel_emoji + Emoji.USER
            name_prefix = msg.chat.long_name
            if msg.chat.other != msg.author:
                name_prefix += f", {msg.author.long_name}"
            msg_template = f"{emoji_prefix} {name_prefix}:"
        elif isinstance(msg.chat, GroupChat):
            emoji_prefix = msg.chat.channel_emoji + Emoji.GROUP
            name_prefix = msg.chat.long_name
            msg_template = f"{emoji_prefix} {msg_prefix} [{name_prefix}]:"
        elif isinstance(msg.chat, SystemChat):
            emoji_prefix = msg.chat.channel_emoji + Emoji.SYSTEM
            name_prefix = msg.chat.long_name
            if msg.chat.other != msg.author:
                name_prefix += f", {msg.author.long_name}"
            msg_template = f"{emoji_prefix} {name_prefix}:"
        else:
            msg_template = f"{Emoji.UNKNOWN} {msg.author.long_name} ({msg.chat.display_name}):"
        return msg_template

    def check_file_size(self, file: Optional[IO[bytes]]) -> Optional[str]:
        """
        Return an error message if the file is too large to upload,
        None otherwise.
        """
        if not file or getattr(file, "closed", True):
            return None
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        if not self.channel.flag("local_tdlib_api") and file_size > telegram.constants.FileSizeLimit.FILESIZE_UPLOAD:
            size_str = humanize.naturalsize(file_size)
            max_size_str = humanize.naturalsize(telegram.constants.FileSizeLimit.FILESIZE_UPLOAD)
            return self._(
                "Attachment is too large ({size}). Maximum allowed by Telegram Bot API is {max_size}. (AT02)").format(
                size=size_str, max_size=max_size_str)
        return None

    def process_file_obj(self, file: IO[bytes], path: Union[str, Path], filename: Optional[str] = None) -> Union[IO[bytes], str]:
        """Process file object for sending to Telegram.

        When using local TDLIB API, files need to be accessible by the Docker container.
        If local_tdlib_temp_dir is configured and the file is outside that directory,
        the file will be copied to the shared directory first.

        Args:
            file: The file object
            path: Path to the file
            filename: Optional original filename to preserve when copying

        Returns:
            file:// URI if using local TDLIB API, otherwise the file object
        """
        if self.channel.flag("local_tdlib_api"):
            abs_path = Path(path).absolute()
            temp_dir = utils.ExperimentalFlagsManager.get_temp_dir(self.channel)

            if temp_dir:
                temp_dir_path = Path(temp_dir)
                try:
                    abs_path.relative_to(temp_dir_path)
                except ValueError:
                    import shutil
                    import tempfile as tmp

                    suffix = ''
                    if filename:
                        suffix = Path(filename).suffix

                    if not suffix:
                        suffix = abs_path.suffix

                    if not suffix:
                        file.seek(0)
                        head = file.read(16)
                        file.seek(0)
                        if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
                            suffix = '.webp'
                        elif head[:8] == b'\x89PNG\r\n\x1a\n':
                            suffix = '.png'
                        elif head[:2] == b'\xff\xd8':
                            suffix = '.jpg'
                        elif head[:6] in (b'GIF87a', b'GIF89a'):
                            suffix = '.gif'
                        elif head[4:8] == b'ftyp':
                            suffix = '.mp4'
                        elif head[:4] == b'OggS':
                            suffix = '.ogg'
                        elif head[:4] == b'%PDF':
                            suffix = '.pdf'

                    if filename:
                        safe_filename = os.path.basename(filename)
                        dest_path = os.path.join(temp_dir, safe_filename)
                        if os.path.exists(dest_path):
                            import uuid
                            name_parts = os.path.splitext(safe_filename)
                            safe_filename = f"{name_parts[0]}_{uuid.uuid4().hex[:8]}{name_parts[1]}"
                            dest_path = os.path.join(temp_dir, safe_filename)
                    else:
                        with tmp.NamedTemporaryFile(suffix=suffix, dir=temp_dir, delete=False) as dest:
                            dest_path = dest.name

                    file.seek(0)
                    with open(dest_path, 'wb') as dest:
                        shutil.copyfileobj(file, dest)

                    os.chmod(dest_path, 0o644)

                    abs_path = Path(dest_path)
                    self.logger.debug("Copied file from %s to shared temp dir: %s (original filename: %s)",
                                      path, dest_path, filename or "N/A")

                    tls = self.bot._cleanup_tls
                    if not hasattr(tls, 'pending_cleanup'):
                        tls.pending_cleanup = []
                    tls.pending_cleanup.append(dest_path)

            return abs_path.as_uri()
        return file

    def _cleanup_pending_local_api_files(self):
        """Delete temp files copied to shared dir for local Bot API sends in this thread.
        Only cleans up files that were NOT already claimed by a delayed task."""
        tls = self.bot._cleanup_tls
        pending = getattr(tls, 'pending_cleanup', [])
        for path in pending:
            try:
                os.unlink(path)
                self.logger.debug("Cleaned up local API temp file: %s", path)
            except OSError as e:
                self.logger.warning("Failed to clean up local API temp file %s: %s", path, e)
        tls.pending_cleanup = []
