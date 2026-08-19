# coding=utf-8

import logging
import threading
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

import telegram  # lgtm [py/import-and-import-from]
import telegram.constants
import telegram.error
from ehforwarderbot import Message, coordinator
from ehforwarderbot.chat import ChatNotificationState, SelfChatMember
from ehforwarderbot.constants import MsgType
from ehforwarderbot.message import MessageCommand
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ChatType

from . import utils
from .chat_object_cache import ChatObjectCacheManager
from .commands import CommandsManager, ETMCommandMsgStorage
from .message import ETMMsg
from .msg_type import get_msg_type
from .slave_delivery_helpers import reactions_footer, send_identity
from .slave_message_claims import SlaveMessageClaimLifecycle
from .slave_routing import SlaveMessageRouter
from .slave_status import deliver_message_status
from .utils import OldMsgID, TelegramChatID, TelegramMessageID, TelegramTopicID

if TYPE_CHECKING:
    from .telegram_api import TelegramAPI


class SlaveMessageService:
    """Process messages as Message objects from slave channels."""

    REACTION_DB_WAIT_TIMEOUT = 2.0
    REACTION_DB_WAIT_INTERVAL = 0.05

    def __init__(
        self,
        bot: "TelegramAPI",
        flag: utils.ExperimentalFlagsManager,
        msglogs,
        delivery_claims,
        chat_manager: ChatObjectCacheManager,
        commands: CommandsManager,
        translate: Callable[[str], str],
        ngettext: Callable[[str, str, int], str],
        router: SlaveMessageRouter,
        text_delivery,
        image_delivery,
        media_delivery,
        file_delivery,
    ):
        self.bot = bot
        self.logger: logging.Logger = logging.getLogger(__name__)
        self.flag = flag
        self.msglogs = msglogs
        self.claim_lifecycle = SlaveMessageClaimLifecycle(delivery_claims, self.logger)
        self.chat_manager = chat_manager
        self.commands = commands
        self.translate = translate
        self.translate_plural = ngettext
        self.router = router
        self.text_delivery = text_delivery
        self.image_delivery = image_delivery
        self.media_delivery = media_delivery
        self.file_delivery = file_delivery

    def _(self, text: str) -> str:
        return getattr(self, "translate", lambda value: value)(text)

    def ngettext(self, singular: str, plural: str, count: int) -> str:
        return getattr(self, "translate_plural", lambda one, many, amount: one if amount == 1 else many)(singular, plural, count)

    def is_silent(self, msg: Message) -> Optional[bool]:
        """Determine if a message shall be sent silently.
        Returns None if the message shall not be sent at all.
        """
        xid = msg.uid
        if isinstance(msg.author, SelfChatMember):
            # Message is send by admin not through EFB
            your_slave_msg = self.flag("your_message_on_slave")
            if your_slave_msg == "silent":
                return True
            elif your_slave_msg == "mute":
                self.logger.debug("[%s] Message is muted as it is from the admin.", xid)
                return None
        elif msg.chat.notification == ChatNotificationState.NONE or (msg.chat.notification == ChatNotificationState.MENTIONS and (not msg.substitutions or not msg.substitutions.is_mentioned)):
            # Shall not be notified in slave channel
            muted_on_slave = self.flag("message_muted_on_slave")
            if muted_on_slave == "silent":
                return True
            elif muted_on_slave == "mute":
                self.logger.debug("[%s] Message is muted due to slave channel settings.", xid)
                return None
        return False

    def send_message(self, msg: Message) -> Message:
        """
        Process a message from slave channel and deliver it to the user.

        Args:
            msg (Message): The message.
        """
        dedupe_key: Optional[Tuple[str, str]] = None
        claim_token: Optional[str] = None
        pending_claimed = False
        tg_dest = None
        thread_id = None
        xid = msg.uid
        old_msg = None
        try:
            slave_origin_uid = utils.chat_id_to_str(chat=msg.chat)
            if msg.edit:
                old_msg = self.msglogs.get_msg_log(slave_msg_id=msg.uid, slave_origin_uid=slave_origin_uid)
                if old_msg and old_msg.provenance == "mtproto_ingested":
                    self.logger.info("Ignoring edit for ingested synthetic message %s.", msg.uid)
                    return msg
            dedupe_key = self.claim_lifecycle.dedupe_key(msg, slave_origin_uid)
            if dedupe_key is not None:
                # Claim delivery durably with a database-backed lease so
                # concurrent workers and process restarts share the claim.
                claim_token = self.claim_lifecycle.claim(dedupe_key)
                if claim_token is None:
                    self.logger.info("[%s] Duplicate slave message is already pending delivery; skipping.", xid)
                    return msg
                pending_claimed = True

            plan = self.router.route(msg)
            msg_template, tg_dest, thread_id = plan.message_template, plan.destination, plan.thread_id

            silent = self.is_silent(msg)
            if silent is None:
                self.claim_lifecycle.release(dedupe_key, claim_token)
                return msg

            if tg_dest is None:
                self.claim_lifecycle.release(dedupe_key, claim_token)
                return msg

            # When editing message
            old_msg_id: Optional[OldMsgID] = None
            _edit_sender_bot_id: Optional[str] = None
            if msg.edit:
                if old_msg:
                    _edit_sender_bot_id = old_msg.sender_bot_id

                    if old_msg.master_msg_id_alt:
                        old_msg_id = utils.message_id_str_to_id(utils.TgChatMsgIDStr(old_msg.master_msg_id_alt))
                    else:
                        old_msg_id = utils.message_id_str_to_id(utils.TgChatMsgIDStr(old_msg.master_msg_id))
                else:
                    self.logger.info("[%s] Was supposed to edit this message, but it does not exist in database. Sending new message instead.", msg.uid)

            if _edit_sender_bot_id:
                msg.vendor_specific = msg.vendor_specific or {}
                msg.vendor_specific["_sender_bot_id"] = _edit_sender_bot_id

            with self.claim_lifecycle.renew(dedupe_key, claim_token) as ownership_lost:
                self.dispatch_message(
                    msg,
                    msg_template,
                    old_msg_id,
                    tg_dest,
                    thread_id,
                    silent,
                    dedupe_key=dedupe_key,
                    claim_token=claim_token,
                    ownership_lost=ownership_lost,
                )
        except Exception as e:
            if pending_claimed:
                self.claim_lifecycle.release(dedupe_key, claim_token)
            if isinstance(e, telegram.error.BadRequest) and e.message:
                if "Topic" in e.message:
                    try:
                        self.bot.reopen_forum_topic(chat_id=tg_dest, message_thread_id=thread_id)
                    except telegram.error.BadRequest as reopen_err:
                        self.logger.error("Failed to reopen topic (%s).", type(reopen_err).__name__)
                        if tg_dest is not None:
                            self.router.remove_topic(tg_dest, thread_id)
            else:
                self.logger.exception(
                    "Failed to process slave message %s (%s).",
                    xid,
                    type(e).__name__,
                )
        return msg

    def dispatch_message(
        self,
        msg: Message,
        msg_template: str,
        old_msg_id: Optional[OldMsgID],
        tg_dest: TelegramChatID,
        thread_id: Optional[TelegramTopicID],
        silent: bool = False,
        dedupe_key: Optional[Tuple[str, str]] = None,
        claim_token: Optional[str] = None,
        ownership_lost: Optional[threading.Event] = None,
        database_old_msg_id: Optional[OldMsgID] = None,
        target_msg_id_override: Optional[TelegramMessageID] = None,
    ):
        """Dispatch with header, destination and Telegram message ID and destinations."""

        xid = msg.uid

        target_msg_id = target_msg_id_override if target_msg_id_override is not None else self.router.resolve_reply(msg, tg_dest)

        # Generate basic reply markup
        commands: Optional[List[MessageCommand]] = None
        reply_markup: Optional[InlineKeyboardMarkup] = None

        if msg.commands:
            commands = msg.commands
            buttons = []
            for idx, i in enumerate(commands):
                buttons.append([InlineKeyboardButton(i.name, callback_data=str(idx))])
            reply_markup = InlineKeyboardMarkup(buttons)

        reactions = reactions_footer(msg.reactions)

        msg.text = msg.text or ""
        # Type dispatching
        if msg.type == MsgType.Text:
            tg_msg = self.text_delivery.text(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        elif msg.type == MsgType.Link:
            tg_msg = self.text_delivery.link(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        elif msg.type == MsgType.Sticker:
            tg_msg = self.media_delivery.sticker(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        elif msg.type == MsgType.Image:
            if self.flag("send_image_as_file"):
                tg_msg = self.file_delivery.file(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
            else:
                tg_msg = self.image_delivery.slave_message_image(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        elif msg.type == MsgType.Animation:
            tg_msg = self.media_delivery.animation(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        elif msg.type == MsgType.File:
            tg_msg = self.file_delivery.file(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        elif msg.type == MsgType.Voice:
            tg_msg = self.file_delivery.voice(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        elif msg.type == MsgType.Location:
            tg_msg = self.file_delivery.location(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        elif msg.type == MsgType.Video:
            tg_msg = self.file_delivery.video(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        elif msg.type == MsgType.Status:
            # Status messages are not to be recorded in databases
            if dedupe_key is not None and claim_token is not None:
                self.claim_lifecycle.release(dedupe_key, claim_token)
            return deliver_message_status(self.bot, msg, tg_dest, thread_id)
        elif msg.type == MsgType.Unsupported:
            tg_msg = self.text_delivery.unsupported(msg, tg_dest, thread_id, msg_template, reactions, old_msg_id, target_msg_id, reply_markup, silent)
        else:
            self.bot.send_chat_action(tg_dest, ChatAction.TYPING, message_thread_id=thread_id)
            tg_msg = self.bot.send_message(
                tg_dest,
                prefix=msg_template,
                suffix=reactions,
                disable_notification=silent,
                message_thread_id=thread_id,
                text=self._('Unknown type of message "{0}". (UT01)').format(msg.type.name),
                **send_identity(msg),
            )

        if tg_msg is None:
            self.logger.warning("[%s] Message sending returned None, skipping database logging. This may happen during shutdown or when Telegram API is unavailable.", xid)
            if dedupe_key is not None and claim_token is not None:
                self.claim_lifecycle.release(dedupe_key, claim_token)
            return

        if ownership_lost is not None and ownership_lost.is_set():
            self.logger.warning("[%s] Delivery claim ownership was lost before post-send processing.", xid)
            return

        self.logger.debug("[%s] Message is sent to the user with telegram message id %s.%s.", xid, tg_msg.chat.id, tg_msg.message_id)
        if dedupe_key is not None and claim_token is not None:
            if not self.claim_lifecycle.complete(dedupe_key, claim_token):
                self.logger.warning("[%s] Delivery claim ownership was lost before completion.", xid)
                return
        if commands:
            authorized_user_ids = (tg_msg.chat.id,) if tg_msg.chat.type == ChatType.PRIVATE else self.router.admins
            self.commands.register_command(tg_msg, ETMCommandMsgStorage(commands, coordinator.get_module_by_id(msg.author.module_id), msg_template, msg.text, authorized_user_ids))
        etm_msg = ETMMsg.from_efbmsg(msg, self.chat_manager)
        try:
            etm_msg.type_telegram = get_msg_type(tg_msg)
            etm_msg.put_telegram_file(tg_msg)
            self.msglogs.add_or_update_message_log(
                etm_msg,
                tg_msg,
                database_old_msg_id or old_msg_id,
                sender_bot_id=getattr(tg_msg, "sender_bot_id", None),
            )
        except Exception as error:
            self.logger.warning(
                "DB write failed for Telegram message %s; dropping mapping (%s).",
                getattr(tg_msg, "message_id", "?"),
                type(error).__name__,
            )
