"""Destination, topic, reply, and template resolution for slave messages."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Optional

from ehforwarderbot import Message
from ehforwarderbot.chat import GroupChat, PrivateChat, SystemChat
from ehforwarderbot.types import MessageID

from efb_telegram_master.chat.chat_destination_cache import ChatDestinationCache
from efb_telegram_master.chat.chat_object_cache import ChatObjectCacheManager
from efb_telegram_master.core import utils
from efb_telegram_master.core.constants import Emoji
from efb_telegram_master.core.utils import TelegramChatID, TelegramMessageID, TelegramTopicID
from efb_telegram_master.delivery.slave_delivery_types import DeliveryPlan


class SlaveMessageRouter:
    """Resolve Telegram delivery details from explicit repositories and services."""

    FORUM_CHAT_CACHE_TTL = 3600

    def __init__(
        self,
        bot,
        msglogs,
        chat_associations,
        chat_dest_cache: ChatDestinationCache,
        chat_manager: ChatObjectCacheManager,
        admins: list[int],
        topic_group: Optional[TelegramChatID],
        topic_sync,
        logger: logging.Logger,
    ) -> None:
        self.bot = bot
        self.msglogs = msglogs
        self.chat_associations = chat_associations
        self.chat_dest_cache = chat_dest_cache
        self.chat_manager = chat_manager
        self.admins = admins
        self.topic_group = topic_group
        self.topic_sync = topic_sync
        self.logger = logger
        self._known_forum_chat_ids: dict[int, float] = {}
        self._known_forum_chat_ids_lock = threading.Lock()

    @contextmanager
    def _timed_phase(self, xid: Optional[MessageID], phase_name: str):
        started = time.monotonic()
        try:
            yield
        finally:
            self.logger.debug("[%s] %s finished in %.3fs.", xid, phase_name, time.monotonic() - started)

    def _is_known_forum_chat(self, destination: TelegramChatID) -> bool:
        with self._known_forum_chat_ids_lock:
            cached_at = self._known_forum_chat_ids.get(int(destination))
            if cached_at is None:
                return False
            if time.monotonic() - cached_at <= self.FORUM_CHAT_CACHE_TTL:
                return True
            del self._known_forum_chat_ids[int(destination)]
            return False

    def _is_forum_chat(self, xid: Optional[MessageID], destination: TelegramChatID) -> bool:
        if self._is_known_forum_chat(destination):
            self.logger.debug("[%s] get_chat_info skipped for Telegram chat %s (known forum chat).", xid, destination)
            return True
        with self._timed_phase(xid, f"get_chat_info for Telegram chat {destination}"):
            is_forum = bool(self.bot.get_chat_info(destination).is_forum)
        self.logger.debug("[%s] get_chat_info for Telegram chat %s returned is_forum=%s.", xid, destination, is_forum)
        if is_forum:
            with self._known_forum_chat_ids_lock:
                self._known_forum_chat_ids[int(destination)] = time.monotonic()
        return is_forum

    def route(self, msg: Message) -> DeliveryPlan:
        xid = msg.uid
        chat = self.chat_manager.update_chat_obj(msg.chat)
        msg.chat = chat
        msg.author = self.chat_manager.get_or_enrol_member(chat, msg.author)
        chat_uid = utils.chat_id_to_str(chat=chat)
        with self._timed_phase(xid, "Destination chat association lookup"):
            associated = self.chat_associations.get_chat_assoc(slave_uid=chat_uid)
        linked_chat = associated[0] if associated else None
        singly_linked = bool(linked_chat)
        if linked_chat:
            slaves = self.chat_associations.get_chat_assoc(master_uid=linked_chat)
            if slaves and len(slaves) > 1:
                singly_linked = False
                self.logger.debug("[%s] Sender is linked with other chats in a Telegram group.", xid)
        destination = TelegramChatID(int(utils.chat_id_str_to_id(linked_chat)[1])) if linked_chat else TelegramChatID(self.admins[0])
        thread_id: Optional[TelegramTopicID] = None
        if self.topic_group and not isinstance(chat, SystemChat):
            destination = TelegramChatID(int(utils.chat_id_str_to_id(linked_chat)[1])) if linked_chat else TelegramChatID(self.topic_group)
            if self._is_forum_chat(xid, destination):
                with self._timed_phase(xid, f"Topic thread lookup for Telegram chat {destination}"):
                    existing_thread_id = self.chat_associations.get_topic_thread_id(slave_uid=chat_uid, topic_chat_id=destination)
                self.logger.debug("[%s] Topic thread lookup for Telegram chat %s returned existing_thread_id=%s.", xid, destination, existing_thread_id)
                with self._timed_phase(xid, f"Topic creation/resolution for Telegram chat {destination}"):
                    thread_id = self.topic_sync.create_topic(slave_uid=chat_uid, telegram_chat_id=destination)
                self.logger.debug("[%s] Topic creation/resolution for Telegram chat %s returned thread_id=%s.", xid, destination, thread_id)
        if not linked_chat:
            singly_linked = False
        if thread_id:
            singly_linked = True
        if self.chat_dest_cache.get(str(destination)) != chat_uid:
            self.chat_dest_cache.remove(str(destination))
        return DeliveryPlan(self.generate_message_template(msg, singly_linked), destination, thread_id)

    def resolve_reply(self, msg: Message, destination: TelegramChatID) -> Optional[TelegramMessageID]:
        if not isinstance(msg.target, Message):
            return None
        self.logger.debug("[%s] Message is replying to slave message %s.", msg.uid, msg.target.uid)
        log = self.msglogs.get_msg_log(slave_msg_id=msg.target.uid, slave_origin_uid=utils.chat_id_to_str(chat=msg.target.chat))
        if not log:
            self.logger.debug("[%s] Target message %s is not found in database.", msg.uid, msg.target.uid)
            return None
        if log.provenance == "mtproto_ingested":
            self.logger.info("[%s] Ignoring reply to ingested synthetic message %s.", msg.uid, msg.target.uid)
            return None
        target = utils.message_id_str_to_id(utils.TgChatMsgIDStr(log.master_msg_id))
        if not target or target[0] != int(destination):
            self.logger.error("[%s] Trying to reply to a message not from this chat. Message destination: %s. Target message: %s.", msg.uid, destination, target)
            return None
        return TelegramMessageID(target[1])

    def remove_topic(self, destination: TelegramChatID, thread_id: Optional[TelegramTopicID]) -> None:
        self.chat_associations.remove_topic_assoc(topic_chat_id=destination, message_thread_id=thread_id)

    def generate_message_template(self, msg: Message, singly_linked: bool) -> str:
        member_name = msg.author.long_name if isinstance(msg.chat, GroupChat) else ""
        if isinstance(msg.chat, GroupChat):
            self.logger.debug("[%s] Message is from a group.", msg.uid)
        if singly_linked:
            return f"{member_name}:" if member_name else (f"{msg.author.long_name}:" if msg.chat != msg.author else "")
        if isinstance(msg.chat, PrivateChat):
            name = msg.chat.long_name if msg.chat.other == msg.author else f"{msg.chat.long_name}, {msg.author.long_name}"
            return f"{msg.chat.channel_emoji}{Emoji.USER} {name}:"
        if isinstance(msg.chat, GroupChat):
            return f"{msg.chat.channel_emoji}{Emoji.GROUP} {member_name} [{msg.chat.long_name}]:"
        if isinstance(msg.chat, SystemChat):
            name = msg.chat.long_name if msg.chat.other == msg.author else f"{msg.chat.long_name}, {msg.author.long_name}"
            return f"{msg.chat.channel_emoji}{Emoji.SYSTEM} {name}:"
        return f"{Emoji.UNKNOWN} {msg.author.long_name} ({msg.chat.display_name}):"
