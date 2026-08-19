import asyncio
import inspect
import logging
import os
import threading
import time
from asyncio import QueueEmpty
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from telethon import TelegramClient
from telethon.events import ChatAction, MessageDeleted, MessageEdited, NewMessage, UserUpdate
from telethon.events.common import EventCommon
from telethon.sessions import StringSession
from telethon.tl.custom import Message
from telethon.tl.types import TypeInputPeer

from .filter_content import text
from .filter_messages import message
from .filters import BaseFilter, everything
from .requests import PRIVATE_RESPONSE_WAIT_CAP, _private_response_cursor
from .utils import parse_socks5_link

CLIENT_START_TIMEOUT = 60
CLIENT_STOP_TIMEOUT = 10
PENDING_EVENT_MAX_COUNT = 256
PENDING_EVENT_MAX_AGE_SECONDS = PRIVATE_RESPONSE_WAIT_CAP + 10.0


@dataclass(frozen=True)
class EventMetadata:
    sequence: int
    arrived_at: float


class TelegramIntegrationTestHelper:
    def __init__(self, session: str, api_id: int, api_hash: str, loop: asyncio.AbstractEventLoop, bot_id: Union[int, Sequence[int]], chats: Iterable[int] = tuple()):
        """Create a Telethon client and receive events only from the configured chats."""
        proxy_env = os.environ.get("all_proxy") or os.environ.get("ALL_PROXY")
        if proxy_env and proxy_env.startswith("socks5://"):
            from socks import SOCKS5

            hostname, port, username, password = parse_socks5_link(proxy_env)
            proxy: Optional[Tuple] = (SOCKS5, hostname, port, True, username, password)
        else:
            proxy = None

        self.client: TelegramClient = TelegramClient(StringSession(session), api_id, api_hash, proxy=proxy, loop=loop, sequential_updates=True)
        self.queue: "asyncio.queues.Queue[EventCommon]" = asyncio.queues.Queue(maxsize=PENDING_EVENT_MAX_COUNT)
        self.pending_events: List[EventCommon] = []
        self._event_sequence = 0
        self._event_metadata: Dict[int, EventMetadata] = {}
        self.message_chat_map: Dict[int, TypeInputPeer] = {}
        self.chats = set(map(abs, chats))
        self._temporary_chat_counts: Dict[int, int] = {}
        self._temporary_chat_lock = threading.Lock()
        self.client.parse_mode = "html"
        bot_ids = [bot_id] if isinstance(bot_id, int) else list(bot_id)
        self._event_handlers = (
            (self.new_message_handler, NewMessage(incoming=True, from_users=bot_ids)),
            (self.deleted_message_handler, MessageDeleted()),
            (self.update_handler, UserUpdate(chats=self.chats)),
            (self.edited_message_handler, MessageEdited()),
            (self.update_handler, ChatAction(chats=self.chats)),
        )
        for handler, event_builder in self._event_handlers:
            self.client.add_event_handler(handler, event_builder)
        self.logger = logging.getLogger(__name__)

    async def update_handler(self, event):
        self.logger.debug("Got event, %s, %s", time.time(), event.to_dict())
        await self._queue_event(event)

    async def _queue_event(self, event: EventCommon) -> None:
        self._prune_pending_events()
        while self.queue.full() or self.queue.qsize() >= PENDING_EVENT_MAX_COUNT:
            try:
                dropped = self.queue.get_nowait()
            except QueueEmpty:
                break
            self.queue.task_done()
            self._release_event(dropped)
        self._event_sequence += 1
        self._event_metadata[id(event)] = EventMetadata(self._event_sequence, time.monotonic())
        self.queue.put_nowait(event)

    def event_cursor(self) -> int:
        """Return the newest observed event sequence immediately before a request trigger."""
        self._prune_pending_events()
        return self._event_sequence

    def _event_metadata_for(self, event: EventCommon) -> EventMetadata:
        metadata = self._event_metadata.get(id(event))
        if metadata is None:
            self._event_sequence += 1
            metadata = EventMetadata(self._event_sequence, time.monotonic())
            self._event_metadata[id(event)] = metadata
        return metadata

    def _matches_cursor(self, event: EventCommon, after_cursor: Optional[int]) -> bool:
        return after_cursor is None or self._event_metadata_for(event).sequence > after_cursor

    def _release_event(self, event: EventCommon) -> None:
        self._event_metadata.pop(id(event), None)

    def _event_is_expired(self, event: EventCommon) -> bool:
        return self._event_metadata_for(event).arrived_at < time.monotonic() - PENDING_EVENT_MAX_AGE_SECONDS

    def _prune_pending_events(self) -> None:
        cutoff = time.monotonic() - PENDING_EVENT_MAX_AGE_SECONDS
        retained: List[EventCommon] = []
        dropped = 0
        for event in self.pending_events:
            if self._event_metadata_for(event).arrived_at < cutoff:
                self._release_event(event)
                dropped += 1
            else:
                retained.append(event)
        overflow = max(0, len(retained) - PENDING_EVENT_MAX_COUNT)
        for event in retained[:overflow]:
            self._release_event(event)
        self.pending_events[:] = retained[overflow:]
        if dropped or overflow:
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.debug("Discarded %d expired and %d overflow integration events.", dropped, overflow)

    def clear_queue(self):
        for event in self.pending_events:
            self._release_event(event)
        self.pending_events.clear()
        while not self.queue.empty():
            try:
                event = self.queue.get_nowait()
            except QueueEmpty:
                break
            self.queue.task_done()
            self._release_event(event)

    def watch_chat(self, chat_id: int) -> None:
        """Receive bot messages from a chat created during an integration test."""
        normalized_chat_id = abs(chat_id)
        with self._temporary_chat_lock:
            self._temporary_chat_counts[normalized_chat_id] = self._temporary_chat_counts.get(normalized_chat_id, 0) + 1

    def unwatch_chat(self, chat_id: int) -> None:
        normalized_chat_id = abs(chat_id)
        with self._temporary_chat_lock:
            count = self._temporary_chat_counts.get(normalized_chat_id, 0)
            if count <= 1:
                self._temporary_chat_counts.pop(normalized_chat_id, None)
            else:
                self._temporary_chat_counts[normalized_chat_id] = count - 1

    def _watches_chat(self, chat_id: int | None) -> bool:
        if chat_id is None:
            return False
        normalized_chat_id = abs(chat_id)
        if normalized_chat_id in self.chats:
            return True
        with self._temporary_chat_lock:
            return normalized_chat_id in self._temporary_chat_counts

    async def new_message_handler(self, event: NewMessage.Event):
        if not self._watches_chat(event.chat_id):
            return
        message: Message = event.message
        self.message_chat_map[message.id] = await message.get_input_chat()
        self.logger.debug("Got new message event, %s, %s", time.time(), event.to_dict())
        await self._queue_event(event)

    async def edited_message_handler(self, event: MessageEdited.Event) -> None:
        if not self._watches_chat(event.chat_id):
            return
        self.logger.debug("Got edited message event, %s, %s", time.time(), event.to_dict())
        await self._queue_event(event)

    async def deleted_message_handler(self, event: MessageDeleted.Event):
        message_id = event.deleted_id
        if event._chat_peer is None and message_id in self.message_chat_map:
            event._chat_peer = self.message_chat_map[message_id]
            del self.message_chat_map[message_id]
        self.logger.debug("Got deleted message event, %s, %s", time.time(), event.to_dict())
        await self._queue_event(event)

    async def wait_for_event(self, event_filter: BaseFilter = everything, timeout: float = 20.0, *, after_cursor: Optional[int] = None) -> EventCommon:
        """Wait for one matching event, preserving unmatched events for later waits."""
        self._prune_pending_events()
        effective_cursor = _private_response_cursor.get() if after_cursor is None else after_cursor
        deadline = time.monotonic() + timeout
        while deadline > time.monotonic():
            for index, value in enumerate(self.pending_events):
                if self._matches_cursor(value, effective_cursor) and (event_filter is None or event_filter(value)):
                    matched = self.pending_events.pop(index)
                    self._release_event(matched)
                    return matched
            time_left = deadline - time.monotonic()
            value = await asyncio.wait_for(self.queue.get(), time_left)
            self.queue.task_done()
            if self._event_is_expired(value):
                self._release_event(value)
                continue
            if self._matches_cursor(value, effective_cursor) and (event_filter is None or event_filter(value)):
                self._release_event(value)
                return value
            self.pending_events.append(value)
            self._prune_pending_events()

    async def wait_for_message(self, event_filter: BaseFilter = everything, timeout: float = 20.0, *, after_cursor: Optional[int] = None) -> Message:
        """Wait for a message and return its entity."""
        event = await self.wait_for_event(message & event_filter, timeout=timeout, after_cursor=after_cursor)
        return event.message  # type: ignore

    async def wait_for_message_text(self, event_filter: BaseFilter = everything, timeout: float = 20.0, *, after_cursor: Optional[int] = None) -> str:
        """Wait for a text message and return its text."""
        event = await self.wait_for_event(text & event_filter, timeout=timeout, after_cursor=after_cursor)
        return event.message.text  # type: ignore

    async def _startup_step(self, phase: str, operation):
        try:
            return await asyncio.wait_for(operation, timeout=CLIENT_START_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"Telegram integration helper timed out during {phase} after {CLIENT_START_TIMEOUT} seconds") from exc

    async def _disconnect_client(self) -> None:
        for handler, event_builder in getattr(self, "_event_handlers", ()):
            self.client.remove_event_handler(handler, event_builder)
        await asyncio.wait_for(self.client.disconnect(), timeout=CLIENT_STOP_TIMEOUT)
        disconnected = getattr(self.client, "disconnected", None)
        if inspect.isawaitable(disconnected):
            await asyncio.wait_for(disconnected, timeout=CLIENT_STOP_TIMEOUT)

    async def __aenter__(self) -> "TelegramIntegrationTestHelper":
        try:
            await self._startup_step("client connect", self.client.connect())
            await self._startup_step("client get_me", self.client.get_me())
            await self._startup_step("client get_dialogs", self.client.get_dialogs())
        except BaseException:
            try:
                await self._disconnect_client()
            except BaseException:
                self.logger.exception("Failed to clean up Telegram client after startup failure")
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._disconnect_client()
