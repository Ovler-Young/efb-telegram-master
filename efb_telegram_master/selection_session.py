"""Lifecycle shared by inline selection sessions.

The /aggregation source chooser and the member-reply chooser each keep an
in-memory session per keyboard token. An action only ever sees a
session returned by find_session(). A finished or expired session leaves the
store, so a later action on its token is rejected as an unknown selection.
"""

import time
from typing import Protocol, TypeVar

SESSION_SECONDS = 900


class SelectionSession(Protocol):
    requester_id: int
    chat_id: int
    message_id: int | None
    expires_at: float


SessionT = TypeVar('SessionT', bound=SelectionSession)


def find_session(sessions: dict[str, SessionT], token: str) -> SessionT | None:
    """Return the session an action may still use. An expired session is expired and removed."""
    session = sessions.get(token)
    if session is None:
        return None
    if session.expires_at <= time.monotonic():
        sessions.pop(token, None)
        return None
    return session


def expire_stale(sessions: dict[str, SessionT]) -> None:
    """Expire every session past its deadline before another session is stored."""
    now = time.monotonic()
    for token, session in list(sessions.items()):
        if session.expires_at <= now:
            sessions.pop(token, None)


def is_owner(session: SelectionSession, user_id: int, chat_id: int, message_id: int) -> bool:
    return (session.requester_id == user_id and session.chat_id == chat_id
            and session.message_id == message_id)
