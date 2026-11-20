"""History replay entries: states, events and allowed transitions.

A history replay entry is one ``historymigrationentry`` row. Its state is not
stored in a column; it follows from the entry's ``generation``, the
generation its target pointer (``historymigrationtarget``) publishes, and
whether the outbound queue holds the entry's ownership key. ``entry_state``
reads those facts into an ``EntryState``; ``ENTRY_TRANSITIONS`` lists the
removals that are applied. The startup sweep in ``db`` and the replay lanes in
``chat_binding`` read each entry's state from rows they already hold and
act only when the table accepts the event for it.
"""
from enum import Enum


class EntryState(Enum):
    ABSENT = "absent"        # no row: not yet staged, or removed
    STAGED = "staged"        # outside the published generation: being staged, or left by a newer publication
    PUBLISHED = "published"  # in the published generation and not yet owned by the queue
    QUEUED = "queued"        # the queue owns its key; the row awaits removal


class EntryEvent(Enum):
    SUPERSEDE = "supersede"  # removed because another generation is published, or at startup
    FINISH = "finish"        # removed after the queue took ownership


# (state, event) -> next state. A pair not listed here is not applied.
# Publishing a generation moves the pointer away from the previous one, whose
# remaining entries then read as staged and are superseded.
ENTRY_TRANSITIONS: dict[tuple[EntryState, EntryEvent], EntryState] = {
    (EntryState.STAGED, EntryEvent.SUPERSEDE): EntryState.ABSENT,
    # Also after a restart between the queue commit and the row removal: the
    # entry is removed, never enqueued again.
    (EntryState.QUEUED, EntryEvent.FINISH): EntryState.ABSENT,
}


def entry_state(*, published: bool, owned: bool = False) -> EntryState:
    """Read an existing entry into its state.

    ``published`` is whether the entry's generation is the one its target
    publishes; a legacy entry (no generation) counts as published while its
    target has no pointer. ``owned`` is whether the queue holds its ownership
    key; it only separates queued from published entries.
    """
    if not published:
        return EntryState.STAGED
    return EntryState.QUEUED if owned else EntryState.PUBLISHED
