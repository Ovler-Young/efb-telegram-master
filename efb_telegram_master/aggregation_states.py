"""Queued aggregation context kinds: states, events and allowed transitions.

An aggregation queue row stores its workflow step in ``log_context``: the
marker byte ``\\x03`` and a pickled dict whose ``kind`` is one of the
``ContextKind`` spellings below, plus the ``frozen`` and ``handoff`` flags.
Those strings are written to existing queues and must keep their spelling;
contexts store plain ``str``, never these classes. ``context_state`` reads a
decoded context into a ``ContextState``; ``CONTEXT_TRANSITIONS`` lists the
states each event may lead to. The queue mutators and the live aggregation
runtime consult ``next_context_states`` before they write, so a row in a
state an event does not accept is rejected or reported instead of changed.

The delivery columns of the same row (queued, in flight, held, sent_pending)
are a separate machine in ``delivery_states``.
"""
from enum import Enum


class ContextKind:
    """Persisted spellings of an aggregation context's ``kind``; never change them."""

    LOGICAL = "logical"
    MEMBER_UPDATE = "member_update"
    AGGREGATE = "aggregate"
    SOURCE = "source"
    REDIRECT = "redirect"
    BOUNDARY = "boundary"
    # Stored in ``source_cancellations``, never in ``outbound_queue``.
    CANCELLATION = "cancellation"


class ContextState(Enum):
    """What an aggregation row is doing; derived from ``kind``, ``frozen`` and ``handoff``."""

    ABSENT = "absent"                    # no row: not yet enqueued, or removed
    LOGICAL = "logical"                  # unsent batch of new members, may still grow
    MEMBER_UPDATE = "member_update"      # pending edit of a confirmed container
    AGGREGATE = "aggregate"              # frozen container request awaiting its receipt
    SOURCE = "source"                    # independent output of one member, not yet frozen
    FROZEN_SOURCE = "frozen_source"      # frozen standalone send or edit of one member
    FROZEN_REDIRECT = "frozen_redirect"  # frozen output of a member that left its container
    HANDED_OFF = "handed_off"            # receipt applied to MsgLog and successors; awaits removal
    BOUNDARY = "boundary"                # ordinary request ordered between batches


class ContextEvent(Enum):
    REPLACE = "replace"
    CLOSE_DESTINATION = "close_destination"
    MATERIALIZE = "materialize"
    EXECUTE = "execute"
    RELEASE = "release"
    RECEIPT = "receipt"
    WITHDRAW = "withdraw"


def _from(states, event: ContextEvent, *targets: ContextState) -> dict:
    return {(state, event): frozenset(targets) for state in states}


_S = ContextState
_E = ContextEvent
_FROZEN = (_S.AGGREGATE, _S.FROZEN_SOURCE, _S.FROZEN_REDIRECT, _S.BOUNDARY)

CONTEXT_TRANSITIONS: dict[tuple[ContextState, ContextEvent], frozenset[ContextState]] = {
    # Appending, merging an edit or removal, or keeping the head of a split batch.
    **{(state, _E.REPLACE): frozenset({state}) for state in (_S.LOGICAL, _S.MEMBER_UPDATE)},
    # A new Telegram message in the destination ends appending; only rows that
    # carry append flags take it. In ``HANDED_OFF`` that is a handed-off
    # aggregate, which still records it so a retried receipt after a restart
    # cannot reopen the container; ``close_aggregation_destination`` skips
    # handed-off independent outputs.
    **{(state, _E.CLOSE_DESTINATION): frozenset({state})
       for state in (_S.LOGICAL, _S.MEMBER_UPDATE, _S.AGGREGATE, _S.HANDED_OFF)},
    (_S.LOGICAL, _E.MATERIALIZE): frozenset({_S.AGGREGATE}),
    (_S.MEMBER_UPDATE, _E.MATERIALIZE): frozenset({_S.AGGREGATE}),
    # The first attempt decides between a standalone output and a redirect.
    (_S.SOURCE, _E.MATERIALIZE): frozenset({_S.FROZEN_SOURCE, _S.FROZEN_REDIRECT}),
    # Frozen requests are sent as stored. A removal edit is rebuilt from the
    # saved standalone message, which also upgrades the former delete format.
    **{(state, _E.MATERIALIZE): frozenset({state}) for state in _FROZEN},
    **{(state, _E.EXECUTE): frozenset({state}) for state in _FROZEN},
    # A definite rejection rewinds a new batch to its logical form; a container
    # update and an independent output stay frozen for their fixed owner.
    (_S.AGGREGATE, _E.RELEASE): frozenset({_S.LOGICAL, _S.AGGREGATE}),
    **{(state, _E.RELEASE): frozenset({state}) for state in (_S.FROZEN_SOURCE, _S.FROZEN_REDIRECT, _S.BOUNDARY)},
    # A receipt is applied once; a retry after a failed row removal changes nothing.
    **_from((_S.AGGREGATE, _S.FROZEN_SOURCE, _S.FROZEN_REDIRECT, _S.HANDED_OFF), _E.RECEIPT, _S.HANDED_OFF),
    (_S.BOUNDARY, _E.RECEIPT): frozenset({_S.BOUNDARY}),
    # Only rows no attempt has started on: an emptied batch, a batch replaced by
    # its split, or an unpublished member's pending outputs.
    **_from((_S.LOGICAL, _S.SOURCE, _S.FROZEN_SOURCE), _E.WITHDRAW, _S.ABSENT),
}


def context_state(context: dict | None) -> ContextState:
    """Read a decoded context into its state. None means no aggregation row."""
    if context is None:
        return ContextState.ABSENT
    kind = context.get("kind")
    if kind in {ContextKind.AGGREGATE, ContextKind.SOURCE, ContextKind.REDIRECT} and context.get("handoff"):
        return ContextState.HANDED_OFF
    if kind in {ContextKind.SOURCE, ContextKind.REDIRECT}:
        if not context.get("frozen"):
            return ContextState.SOURCE
        return ContextState.FROZEN_SOURCE if kind == ContextKind.SOURCE else ContextState.FROZEN_REDIRECT
    try:
        return ContextState(kind)
    except ValueError:
        raise ValueError(f"Unknown aggregation context kind {kind!r}.") from None


def next_context_states(state: ContextState, event: ContextEvent) -> frozenset[ContextState]:
    """The states ``event`` may lead to from ``state``; empty when it is not allowed."""
    return CONTEXT_TRANSITIONS.get((state, event), frozenset())


def _member_identities(context: dict) -> set[tuple[str, str]]:
    members = list(context.get("members") or []) + list(context.get("updates") or [])
    if context.get("member"):
        members.append(context["member"])
    if context.get("aggregate"):
        members += context["aggregate"].get("children") or []
    return {(member["origin_uid"], member["source_id"]) for member in members}


def needs_delivered_predecessor(context: dict, predecessor: dict | None) -> bool:
    """Whether a row must wait until its predecessor is on Telegram, not only sent first.

    A predecessor that keeps failing stops being an ordering barrier, but an edit
    of a container, an append into the predecessor's container and another
    output of a member the predecessor carries need its Telegram message, so they
    keep waiting. Everything else only keeps order and may go first.
    ``predecessor`` is the predecessor's decoded context, None when it has none.
    """
    kind = context.get("kind")
    if kind == ContextKind.MEMBER_UPDATE:
        return True
    if kind == ContextKind.AGGREGATE:
        return bool(context.get("container_id"))
    if kind == ContextKind.LOGICAL:
        return bool(context.get("inherit")) and not context.get("incoming_boundary")
    if kind in {ContextKind.SOURCE, ContextKind.REDIRECT} and predecessor is not None:
        return bool(_member_identities(context) & _member_identities(predecessor))
    return False


class CloseReason:
    """Why a container closed; logged and counted, never persisted."""

    TELEGRAM_DESTINATION = "telegram-destination"  # a new Telegram message in the destination
    ROUTE_CHANGED = "route-changed"                # the source now delivers to another chat or topic
    IDLE = "idle"                                  # no new member for text_aggregation_idle_seconds
    CAPACITY = "capacity"                          # the pending batch is full
    INDEPENDENT_DELIVERY = "independent-delivery"  # a non-text or command message is sent on its own
    SPLIT_TAIL = "split-tail-"                     # prefix; followed by the kind of the new tail row

    @classmethod
    def split_tail(cls, kind: str) -> str:
        return cls.SPLIT_TAIL + kind
