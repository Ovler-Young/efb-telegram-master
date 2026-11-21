"""Lifecycle shared by inline selection sessions.

The /aggregation source chooser and the member-reply chooser each keep an
in-memory session per keyboard token. The states, events and allowed
transitions below are the one definition both use. Handlers change a session
only through advance(), and an action only ever sees a session returned by
find_session(). A terminal session leaves the store, so a later action on its
token is rejected as an unknown selection.
"""

import time
from enum import Enum
from typing import Protocol, TypeVar

SESSION_SECONDS = 900


class SelectionState(Enum):
    OPEN = 'open'
    PAGED = 'paged'
    CHOSEN = 'chosen'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'
    INVALIDATED = 'invalidated'


class SelectionEvent(Enum):
    PAGE = 'page'
    SELECT = 'select'
    CANCEL = 'cancel'
    EXPIRE = 'expire'
    INVALIDATE = 'invalidate'


LIVE_STATES = frozenset({SelectionState.OPEN, SelectionState.PAGED})
TERMINAL_STATES = frozenset({SelectionState.CHOSEN, SelectionState.CANCELLED,
                             SelectionState.EXPIRED, SelectionState.INVALIDATED})

# (state, event) -> next state. A pair not listed here is an illegal transition.
TRANSITIONS: dict[tuple[SelectionState, SelectionEvent], SelectionState] = {
    (SelectionState.OPEN, SelectionEvent.PAGE): SelectionState.PAGED,
    (SelectionState.PAGED, SelectionEvent.PAGE): SelectionState.PAGED,
    (SelectionState.OPEN, SelectionEvent.SELECT): SelectionState.CHOSEN,
    (SelectionState.PAGED, SelectionEvent.SELECT): SelectionState.CHOSEN,
    (SelectionState.OPEN, SelectionEvent.CANCEL): SelectionState.CANCELLED,
    (SelectionState.PAGED, SelectionEvent.CANCEL): SelectionState.CANCELLED,
    (SelectionState.OPEN, SelectionEvent.EXPIRE): SelectionState.EXPIRED,
    (SelectionState.PAGED, SelectionEvent.EXPIRE): SelectionState.EXPIRED,
    (SelectionState.OPEN, SelectionEvent.INVALIDATE): SelectionState.INVALIDATED,
    (SelectionState.PAGED, SelectionEvent.INVALIDATE): SelectionState.INVALIDATED,
}


class InvalidSelectionTransition(RuntimeError):
    """An event was applied to a selection session in a state that does not allow it."""


class SelectionSession(Protocol):
    requester_id: int
    chat_id: int
    message_id: int | None
    expires_at: float
    state: SelectionState


SessionT = TypeVar('SessionT', bound=SelectionSession)


def advance(sessions: dict[str, SessionT], token: str, session: SessionT, event: SelectionEvent) -> None:
    target = TRANSITIONS.get((session.state, event))
    if target is None:
        raise InvalidSelectionTransition(f'{event.value} is not allowed while {session.state.value}')
    session.state = target
    if target in TERMINAL_STATES:
        sessions.pop(token, None)


def find_session(sessions: dict[str, SessionT], token: str) -> SessionT | None:
    """Return the session an action may still use. An expired session is expired and removed."""
    session = sessions.get(token)
    if session is None or session.state not in LIVE_STATES:
        return None
    if session.expires_at <= time.monotonic():
        advance(sessions, token, session, SelectionEvent.EXPIRE)
        return None
    return session


def expire_stale(sessions: dict[str, SessionT]) -> None:
    """Expire every session past its deadline before another session is stored."""
    now = time.monotonic()
    for token, session in list(sessions.items()):
        if session.expires_at <= now:
            advance(sessions, token, session, SelectionEvent.EXPIRE)


def is_owner(session: SelectionSession, user_id: int, chat_id: int, message_id: int) -> bool:
    return (session.requester_id == user_id and session.chat_id == chat_id
            and session.message_id == message_id)
