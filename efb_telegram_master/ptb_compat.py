import asyncio
from collections.abc import Callable, Collection, Coroutine
from functools import wraps
from typing import ParamSpec, Protocol, TypeAlias, TypeVar

from telegram import Chat, Message, MessageOriginChannel
from telegram.ext import filters

FilterUserId: TypeAlias = int | Collection[int] | None
P = ParamSpec("P")
T = TypeVar("T")


class MessageIdentifier(Protocol):
    message_id: int


class SupportsSendMessage(Protocol):
    def send_message(self, chat_id: int, text: str, **kwargs: object) -> MessageIdentifier:
        ...


class _UpdateFilters:
    message = filters.UpdateType.MESSAGE
    channel_post = filters.UpdateType.CHANNEL_POST
    edited_message = filters.UpdateType.EDITED_MESSAGE
    edited_channel_post = filters.UpdateType.EDITED_CHANNEL_POST


class _StatusUpdateFilters:
    migrate = filters.StatusUpdate.MIGRATE
    new_chat_members = filters.StatusUpdate.NEW_CHAT_MEMBERS
    left_chat_member = filters.StatusUpdate.LEFT_CHAT_MEMBER


class _FiltersCompat:
    all = filters.ALL
    text = filters.TEXT
    photo = filters.PHOTO
    sticker = filters.Sticker.ALL
    document = filters.Document.ALL
    venue = filters.VENUE
    location = filters.LOCATION
    audio = filters.AUDIO
    voice = filters.VOICE
    video = filters.VIDEO
    contact = filters.CONTACT
    video_note = filters.VIDEO_NOTE
    dice = filters.Dice.ALL
    passport_data = filters.PASSPORT_DATA
    invoice = filters.INVOICE
    game = filters.GAME
    successful_payment = filters.SUCCESSFUL_PAYMENT
    poll = filters.POLL
    update = _UpdateFilters()
    status_update = _StatusUpdateFilters()

    @staticmethod
    def regex(pattern: str):
        return filters.Regex(pattern)

    @staticmethod
    def user(*, user_id: FilterUserId):
        return filters.User(user_id=user_id)


Filters = _FiltersCompat()


def threaded_callback(
    callback: Callable[P, T],
) -> Callable[P, Coroutine[None, None, T]]:
    """Run a synchronous PTB callback in a worker thread."""

    @wraps(callback)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await asyncio.to_thread(callback, *args, **kwargs)

    return wrapper


def get_forwarded_chat(message: Message) -> Chat | None:
    """Return the forwarded chat across PTB 13 and PTB 20+ message models."""
    chat = getattr(message, "forward_from_chat", None)
    if chat is not None:
        return chat

    forward_origin = getattr(message, "forward_origin", None)
    if isinstance(forward_origin, MessageOriginChannel):
        return forward_origin.chat
    return None


def sync_reply_text(
    bot_manager: SupportsSendMessage,
    message: Message,
    text: str,
    *,
    quote: bool = False,
    **kwargs: object,
) -> MessageIdentifier:
    if quote:
        kwargs.setdefault("reply_to_message_id", message.message_id)
    return bot_manager.send_message(message.chat.id, text=text, **kwargs)


def sync_reply_html(
    bot_manager: SupportsSendMessage,
    message: Message,
    text: str,
    *,
    quote: bool = False,
    **kwargs: object,
) -> MessageIdentifier:
    kwargs.setdefault("parse_mode", "HTML")
    return sync_reply_text(bot_manager, message, text, quote=quote, **kwargs)
