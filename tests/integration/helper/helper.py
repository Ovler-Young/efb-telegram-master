import asyncio
import inspect
import logging
import os
import threading
import time
from asyncio import QueueEmpty
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar, Union

from telethon import TelegramClient
from telethon.events import ChatAction, MessageDeleted, MessageEdited, NewMessage, UserUpdate
from telethon.events.common import EventCommon
from telethon.sessions import StringSession
from telethon.tl.custom import Message
from telethon.tl.types import MessageEmpty, TypeInputPeer

from . import filters
from .filters import BaseFilter
from .utils import parse_socks5_link

CLIENT_START_TIMEOUT = 60
CLIENT_STOP_TIMEOUT = 10
PRIVATE_RESPONSE_WAIT_CAP = 65.0
MESSAGE_STATE_POLL_INTERVAL_SECONDS = 1.0
NEW_MESSAGE_PAGE_SIZE = 20
NEW_MESSAGE_MAX_PAGES_PER_POLL = 3
PENDING_EVENT_MAX_COUNT = 256
PENDING_EVENT_MAX_AGE_SECONDS = PRIVATE_RESPONSE_WAIT_CAP + 10.0
Response = TypeVar("Response")
_private_response_cursor: ContextVar[Optional[int]] = ContextVar("private_response_cursor", default=None)


@dataclass(frozen=True)
class EventMetadata:
    sequence: int
    arrived_at: float


async def wait_for_limiter_slot(peek_delay: Callable[[], float], *, cap: float = PRIVATE_RESPONSE_WAIT_CAP) -> None:
    """Wait for one outbound limiter slot, never beyond its 60-second window plus margin."""
    deadline = time.monotonic() + cap
    while True:
        delay = max(0.0, peek_delay())
        if delay == 0.0:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError(f"Outbound limiter did not free a slot within {cap:g} seconds")
        await asyncio.sleep(min(delay, remaining))


async def wait_for_private_response(
    peek_delay: Callable[[], float],
    trigger: Callable[[], Awaitable[object]],
    receive: Callable[[float], Awaitable[Response]],
    *,
    cap: float = PRIVATE_RESPONSE_WAIT_CAP,
    response_cursor: Optional[Callable[[], int]] = None,
) -> Response:
    """Use one deadline for the limiter wait, command, and its response."""

    async def wait() -> Response:
        deadline = time.monotonic() + cap
        await wait_for_limiter_slot(peek_delay, cap=deadline - time.monotonic())
        cursor = response_cursor() if response_cursor else None
        cursor_token = _private_response_cursor.set(cursor)
        try:
            await trigger()
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise TimeoutError(f"Private response did not arrive within {cap:g} seconds")
            return await receive(remaining)
        finally:
            _private_response_cursor.reset(cursor_token)

    return await asyncio.wait_for(wait(), timeout=cap)


async def wait_for_message_state(
    client: TelegramClient,
    chat_id: int,
    message_id: int,
    expected: Callable[[Message], bool],
    *,
    timeout: float = 20.0,
) -> Message:
    """Observe one known Telegram message until its state satisfies ``expected``."""
    deadline = time.monotonic() + timeout
    last_state = "missing"
    first_attempt = True
    while True:
        if not first_attempt and time.monotonic() >= deadline:
            raise TimeoutError(f"Telegram message {chat_id}.{message_id} did not reach the expected state within {timeout:g} seconds; last_state={last_state}")
        first_attempt = False
        result = await client.get_messages(chat_id, ids=message_id)
        if isinstance(result, Message):
            last_state = repr(result.to_dict())
            if expected(result):
                return result
        elif result is not None and not isinstance(result, MessageEmpty):
            raise TypeError(f"Telegram get_messages returned {type(result).__name__} for exact message {chat_id}.{message_id}")
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError(f"Telegram message {chat_id}.{message_id} did not reach the expected state within {timeout:g} seconds; last_state={last_state}")
        await asyncio.sleep(min(MESSAGE_STATE_POLL_INTERVAL_SECONDS, remaining))


async def wait_for_new_message_after(
    client: TelegramClient,
    chat_id: int,
    after_message_id: int,
    expected: Callable[[Message], bool],
    *,
    timeout: float = 20.0,
) -> Message:
    """Observe the earliest matching message after a known boundary with bounded pagination."""
    deadline = time.monotonic() + timeout
    last_state = "missing"
    scan_after_message_id = after_message_id
    while True:
        page_after_message_id = scan_after_message_id
        exhausted = False
        for _ in range(NEW_MESSAGE_MAX_PAGES_PER_POLL):
            result = await client.get_messages(
                chat_id,
                min_id=after_message_id,
                offset_id=page_after_message_id,
                limit=NEW_MESSAGE_PAGE_SIZE,
                reverse=True,
            )
            if not isinstance(result, list):
                if result is not None:
                    raise TypeError(f"Telegram get_messages returned {type(result).__name__} for messages after {chat_id}.{after_message_id}")
                exhausted = True
                break
            page = [current for current in result if isinstance(current, Message) and current.id > page_after_message_id]
            if not page:
                exhausted = True
                break
            for current in page:
                last_state = repr(current.to_dict())
                if expected(current):
                    return current
            page_after_message_id = page[-1].id
            if len(page) < NEW_MESSAGE_PAGE_SIZE:
                exhausted = True
                break
        if not exhausted:
            scan_after_message_id = page_after_message_id
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError(f"Telegram did not create the expected message after {chat_id}.{after_message_id} within {timeout:g} seconds; last_state={last_state}")
        await asyncio.sleep(min(MESSAGE_STATE_POLL_INTERVAL_SECONDS, remaining))


class TelegramIntegrationTestHelper:
    def __init__(self, session: str, api_id: int, api_hash: str, loop: asyncio.AbstractEventLoop, bot_id: Union[int, Sequence[int]], chats: Iterable[int] = tuple()):
        """
        Need to create a client with API key, hash, and a session file
        Need a list of whitelisted chat IDs

        Create a queue for incoming messages.

        Args:
            session: Session authorization string
            api_id: API ID of Telegram client
            api_hash: API Hash of Telegram client
            loop: Event loop to run the client on, (can be provided by ``pytest-asyncio``)
        """

        # Build proxy parameters
        # Currently only support SOCKS5 proxy in ALL_PROXY environment variable
        proxy_env = os.environ.get("all_proxy") or os.environ.get("ALL_PROXY")
        if proxy_env and proxy_env.startswith("socks5://"):
            from socks import SOCKS5

            hostname, port, username, password = parse_socks5_link(proxy_env)
            proxy: Optional[Tuple] = (SOCKS5, hostname, port, True, username, password)
        else:
            proxy = None

        # Telethon client to use
        self.client: TelegramClient = TelegramClient(StringSession(session), api_id, api_hash, proxy=proxy, loop=loop, sequential_updates=True)

        # Queue for incoming messages
        self.queue: "asyncio.queues.Queue[EventCommon]" = asyncio.queues.Queue(maxsize=PENDING_EVENT_MAX_COUNT)
        # Events may arrive in a different order than the assertions that
        # consume them. Keep unmatched events for a later, compatible wait.
        self.pending_events: List[EventCommon] = []
        self._event_sequence = 0
        self._event_metadata: Dict[int, EventMetadata] = {}

        # Collect mappings from message ID to its chat (as Telegram API is not sending them)
        self.message_chat_map: Dict[int, TypeInputPeer] = dict()

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
        # record the mapping of message ID and its chat
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
        # Try to recover chat of the message from the mapping
        message_id = event.deleted_id
        if event._chat_peer is None and message_id in self.message_chat_map:
            input_peer = self.message_chat_map[message_id]
            event._chat_peer = input_peer
            del self.message_chat_map[message_id]
        self.logger.debug("Got deleted message event, %s, %s", time.time(), event.to_dict())
        await self._queue_event(event)

    async def wait_for_event(self, event_filter: BaseFilter = filters.everything, timeout: float = 20.0, *, after_cursor: Optional[int] = None) -> EventCommon:
        """
        Args:
            event_filter: Filter updates to collect
            timeout: raises an exception when no update is found in the
                indicated time

        Returns:
            the update

        Raises:
            :exc:`asyncio.TimeoutError`: when the request timed out
        """
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

    async def wait_for_message(self, event_filter: BaseFilter = filters.everything, timeout: float = 20.0, *, after_cursor: Optional[int] = None) -> Message:
        """Short cut for “Wait for a message and return its entity”."""
        event = await self.wait_for_event(filters.message & event_filter, timeout=timeout, after_cursor=after_cursor)
        # noinspection PyUnresolvedReferences
        return event.message  # type: ignore

    async def wait_for_message_text(self, event_filter: BaseFilter = filters.everything, timeout: float = 20.0, *, after_cursor: Optional[int] = None) -> str:
        """Short cut for “Wait for a text message and return its text”."""
        event = await self.wait_for_event(filters.text & event_filter, timeout=timeout, after_cursor=after_cursor)
        # noinspection PyUnresolvedReferences
        return event.message.text  # type: ignore

    # Context management
    # ------------------

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

            # Issue a high level command to start receiving message
            await self._startup_step("client get_me", self.client.get_me())
            # Fill the entity cache
            await self._startup_step("client get_dialogs", self.client.get_dialogs())
        except BaseException:
            try:
                await self._disconnect_client()
            except BaseException:
                self.logger.exception("Failed to clean up Telegram client after startup failure")
            raise

        return self

    def __enter__(self) -> "TelegramIntegrationTestHelper":
        """
        Start the client and return the helper

        Returns:
            self
        """
        self.client.loop.run_until_complete(self.__aenter__())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._disconnect_client()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Trigger the event to end the main async task."""
        self.client.loop.run_until_complete(self.__aexit__(exc_type, exc_val, exc_tb))
