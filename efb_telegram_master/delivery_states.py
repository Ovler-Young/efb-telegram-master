"""Outbound queue row states, hold spellings, events and allowed transitions.

A queue row's delivery state is stored in two columns: ``delivery_state``
(``queued`` or ``sent_pending``) and the free-form ``delivery_hold``. The
values below are the exact strings written to existing queues and must keep
their spelling. ``classify`` reads a stored row into a ``RowState``;
``TRANSITIONS`` lists every event each state accepts and the state it leads
to. Queue mutators consult ``next_state`` before they write, so an event a
state does not accept is rejected or reported instead of being applied.

The ordering-link rule (which predecessor still defers its dependent) is
defined here too, as SQL fragments. ``queue_report.py`` runs without the
package and keeps a copy of the builders it needs; a unit test keeps those
copies identical to these.
"""
from enum import Enum

# Persisted ``delivery_state`` values.
QUEUED = "queued"
SENT_PENDING = "sent_pending"

# ``reconcile_attempts`` of a queued row counts its failed attempts in a row
# (Telegram flood waits excluded). From this many on, the row no longer defers
# the rows ordered after it: it keeps retrying and is delivered late. A send
# interrupted by a restart is raised to this count when the queue opens.
FAILED_ATTEMPTS_LIMIT = 3


class HoldKind(Enum):
    """Persisted ``delivery_hold`` spellings; a value ending in ``:`` is a prefix."""

    IN_FLIGHT = "in_flight"
    INVALID_PAYLOAD = "invalid_payload"
    UNCERTAIN = "uncertain:"
    HISTORY_FAILED = "history_failed:"
    SUPPLEMENT_FAILED = "supplement_failed:"
    AGGREGATION_FAILED = "aggregation_failed:"


# Recorded by ``python -m efb_telegram_master.hold_send`` for a send an operator
# saw delivered; ``/confirm_send`` then accepts any configured bot as its author.
OPERATOR_OBSERVED_DELIVERY = "operator_observed_delivery"
OPERATOR_HOLD = HoldKind.UNCERTAIN.value + OPERATOR_OBSERVED_DELIVERY


class RowState(Enum):
    """What a stored queue row is doing; derived from its columns, never persisted."""

    ABSENT = "absent"                        # no row: not yet enqueued, or removed
    WAITING = "waiting"                      # queued, no hold
    DEFERRED = "deferred"                    # waiting, retry deadline still ahead
    IN_FLIGHT = "in_flight"                  # send attempt recorded, outcome unknown
    UNCERTAIN = "uncertain"                  # ambiguous result: may have been delivered
    INVALID_PAYLOAD = "invalid_payload"      # stored request cannot be decoded
    HISTORY_FAILED = "history_failed"        # history replay rejected
    SUPPLEMENT_FAILED = "supplement_failed"  # supplemental attachment rejected
    AGGREGATION_FAILED = "aggregation_failed"  # aggregation row failed terminally
    UNRECOGNIZED_HOLD = "unrecognized_hold"  # hold text this version does not know
    SENT_PENDING = "sent_pending"            # Telegram receipt saved, MsgLog write due


_HOLD_STATES = {
    HoldKind.IN_FLIGHT: RowState.IN_FLIGHT,
    HoldKind.INVALID_PAYLOAD: RowState.INVALID_PAYLOAD,
    HoldKind.UNCERTAIN: RowState.UNCERTAIN,
    HoldKind.HISTORY_FAILED: RowState.HISTORY_FAILED,
    HoldKind.SUPPLEMENT_FAILED: RowState.SUPPLEMENT_FAILED,
    HoldKind.AGGREGATION_FAILED: RowState.AGGREGATION_FAILED,
}

# Rows kept for an operator: never sent automatically and never an ordering barrier.
HELD_STATES = frozenset({
    RowState.UNCERTAIN, RowState.INVALID_PAYLOAD, RowState.HISTORY_FAILED,
    RowState.SUPPLEMENT_FAILED, RowState.AGGREGATION_FAILED, RowState.UNRECOGNIZED_HOLD,
})


class Event(Enum):
    BEGIN_ATTEMPT = "begin_attempt"
    MATERIALIZE = "materialize"
    RELEASE_ATTEMPT = "release_attempt"
    HOLD_UNCERTAIN = "hold_uncertain"
    OPERATOR_HOLD = "operator_hold"
    HOLD_INVALID_PAYLOAD = "hold_invalid_payload"
    HOLD_FAILED_HISTORY = "hold_failed_history"
    HOLD_FAILED_SUPPLEMENT = "hold_failed_supplement"
    HOLD_FAILED_AGGREGATION = "hold_failed_aggregation"
    DEFER_ACQUISITION = "defer_acquisition"
    DEFER_FAILED_SEND = "defer_failed_send"
    RECORD_COMPLETION = "record_completion"
    OPERATOR_CONFIRM = "operator_confirm"
    DEFER_RECONCILIATION = "defer_reconciliation"
    REMOVE = "remove"
    WITHDRAW = "withdraw"


def _from(states, event: Event, target: RowState) -> dict:
    return {(state, event): target for state in states}


_ATTEMPTABLE = (RowState.WAITING, RowState.DEFERRED)
_UNSETTLED = (RowState.WAITING, RowState.DEFERRED, RowState.IN_FLIGHT)

TRANSITIONS: dict[tuple[RowState, Event], RowState] = {
    **_from(_ATTEMPTABLE, Event.BEGIN_ATTEMPT, RowState.IN_FLIGHT),
    **_from(_ATTEMPTABLE, Event.MATERIALIZE, RowState.IN_FLIGHT),
    (RowState.IN_FLIGHT, Event.RELEASE_ATTEMPT): RowState.WAITING,
    **_from(_UNSETTLED, Event.HOLD_UNCERTAIN, RowState.UNCERTAIN),
    **_from(_UNSETTLED + (RowState.UNCERTAIN,), Event.OPERATOR_HOLD, RowState.UNCERTAIN),
    **_from(_UNSETTLED, Event.HOLD_INVALID_PAYLOAD, RowState.INVALID_PAYLOAD),
    **_from(_UNSETTLED, Event.HOLD_FAILED_HISTORY, RowState.HISTORY_FAILED),
    **_from(_UNSETTLED, Event.HOLD_FAILED_SUPPLEMENT, RowState.SUPPLEMENT_FAILED),
    **_from(_UNSETTLED, Event.HOLD_FAILED_AGGREGATION, RowState.AGGREGATION_FAILED),
    **_from(_UNSETTLED, Event.DEFER_ACQUISITION, RowState.DEFERRED),
    # A send that failed with a retryable error; its attempt was released first.
    # Earlier failures only count; the one that makes the row failing stores its
    # backoff.
    **_from(_ATTEMPTABLE, Event.DEFER_FAILED_SEND, RowState.DEFERRED),
    **_from(_UNSETTLED, Event.RECORD_COMPLETION, RowState.SENT_PENDING),
    **_from((RowState.IN_FLIGHT, *HELD_STATES), Event.OPERATOR_CONFIRM, RowState.SENT_PENDING),
    (RowState.SENT_PENDING, Event.DEFER_RECONCILIATION): RowState.SENT_PENDING,
    **_from(_UNSETTLED + (RowState.SENT_PENDING,), Event.REMOVE, RowState.ABSENT),
    # Cancelling an empty batch or a pending source, or splitting a batch into
    # rows that replace it, removes only a row no attempt has started on.
    **_from(_ATTEMPTABLE, Event.WITHDRAW, RowState.ABSENT),
}


def next_state(state: RowState, event: Event) -> RowState | None:
    """The state ``event`` leads to from ``state``, or None when it is not allowed."""
    return TRANSITIONS.get((state, event))


def parse_hold(hold: str) -> tuple[HoldKind, str] | None:
    """Split a stored hold into its kind and detail; None for an unknown spelling."""
    for kind in HoldKind:
        if kind.value.endswith(":"):
            if hold.startswith(kind.value):
                return kind, hold[len(kind.value):]
        elif hold == kind.value:
            return kind, ""
    return None


def format_hold(kind: HoldKind, detail: str = "") -> str:
    """The exact text stored for a hold of ``kind``."""
    if kind.value.endswith(":"):
        if not detail:
            raise ValueError(f"A {kind.value} hold needs a detail.")
        return kind.value + detail
    if detail:
        raise ValueError(f"A {kind.value} hold has no detail.")
    return kind.value


def error_detail(error: BaseException, *, with_cause: bool = False) -> str:
    """Class names only: holds never store messages, requests or credentials."""
    detail = type(error).__name__
    if with_cause and error.__cause__ is not None:
        detail += "/" + type(error.__cause__).__name__
    return detail


def classify(
    delivery_state: str | None, delivery_hold: str | None,
    reconcile_after: float = 0.0, now: float | None = None,
) -> RowState:
    """Read stored columns into a state. ``delivery_state`` None means no row.

    ``reconcile_after`` is the retry deadline after a failed acquisition or send
    while a row is queued and the receipt-reconciliation deadline once it is
    ``sent_pending``; only the former makes a row DEFERRED, and only when
    ``now`` is given.
    """
    if delivery_state is None:
        return RowState.ABSENT
    if delivery_state == SENT_PENDING:
        return RowState.SENT_PENDING
    if delivery_state != QUEUED:
        raise ValueError(f"Unknown queue delivery_state {delivery_state!r}.")
    if delivery_hold is None:
        if now is not None and reconcile_after and reconcile_after > now:
            return RowState.DEFERRED
        return RowState.WAITING
    parsed = parse_hold(delivery_hold)
    return RowState.UNRECOGNIZED_HOLD if parsed is None else _HOLD_STATES[parsed[0]]


# SQL builders. They emit SQLite 3.31-compatible SQL. ``alias`` qualifies the
# columns of the row being tested; an empty alias leaves them unqualified.

def waiting_sql(alias: str = "") -> str:
    """Rows the scheduler may pick: queued and unheld."""
    prefix = f"{alias}." if alias else ""
    return f"{prefix}delivery_state = '{QUEUED}' AND {prefix}delivery_hold IS NULL"


def failing_sql(alias: str) -> str:
    """The row ``alias`` is waiting or in flight after too many failed attempts."""
    return (
        f"({alias}.delivery_state = '{QUEUED}' "
        f"AND {alias}.reconcile_attempts >= {FAILED_ATTEMPTS_LIMIT} "
        f"AND ({alias}.delivery_hold IS NULL "
        f"OR {alias}.delivery_hold = '{HoldKind.IN_FLIGHT.value}'))"
    )


def barrier_sql(alias: str) -> str:
    """The row ``alias`` still defers its dependents: awaiting MsgLog, or waiting or
    in flight with fewer than ``FAILED_ATTEMPTS_LIMIT`` failed attempts."""
    return (
        f"({alias}.delivery_state = '{SENT_PENDING}' "
        f"OR ({alias}.delivery_state = '{QUEUED}' "
        f"AND {alias}.reconcile_attempts < {FAILED_ATTEMPTS_LIMIT} "
        f"AND ({alias}.delivery_hold IS NULL "
        f"OR {alias}.delivery_hold = '{HoldKind.IN_FLIGHT.value}')))"
    )


def backing_off_sql(alias: str, now: str) -> str:
    """The row ``alias`` failed too often and waits for its retry deadline.

    It is not its destination's head meanwhile, so the destination's later rows
    are elected; once the deadline passes it is the oldest row again and is
    retried. ``now`` is an SQL expression for the current Unix time.
    """
    return (
        f"({alias}.reconcile_attempts >= {FAILED_ATTEMPTS_LIMIT} "
        f"AND {alias}.reconcile_after > {now})"
    )


def ready_sql(alias: str, predecessor: str = "predecessor") -> str:
    """Waiting rows that no ordering link defers: candidates for a destination head."""
    return (
        f"{waiting_sql(alias)} AND ({alias}.predecessor_id IS NULL OR NOT EXISTS ("
        f"SELECT 1 FROM outbound_queue AS {predecessor} "
        f"WHERE {predecessor}.id = {alias}.predecessor_id "
        f"AND {predecessor}.id != {alias}.id "
        f"AND {barrier_sql(predecessor)}))"
    )
