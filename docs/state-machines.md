# State machines

Every lifecycle that changes persisted rows or drives the outbound worker has
one definition in code, and the code that changes that state goes through it.
A change the definition does not allow is rejected or logged with the row and
the attempted event; it is never applied silently. This page lists them; the
linked sections give states, transitions, log lines and operator actions.

| Machine | Defined in | States | Kept in | Details |
|---|---|---|---|---|
| Queue row delivery | `delivery_states.py`: `RowState`, `Event`, `TRANSITIONS` | waiting, deferred, in_flight, uncertain, invalid_payload, history_failed, supplement_failed, aggregation_failed, unrecognized_hold, sent_pending | `outbound_queue.delivery_state`, `delivery_hold`, `reconcile_after` | [delivery-recovery.md, Queue row states](delivery-recovery.md#queue-row-states) |
| Ordering link | `delivery_states.py`: the SQL builders `waiting_sql`, `barrier_sql`, `failing_sql`, `backing_off_sql` and `ready_sql` (copied in `queue_report.py`, except `failing_sql`); `aggregation_states.py`: `needs_delivered_predecessor` | free, barrier, failing, void, cycle | `outbound_queue.predecessor_id`, `reconcile_attempts`, `reconcile_after` | [delivery-recovery.md, Ordering links](delivery-recovery.md#ordering-links) |
| Dispatch pass | `outbound.py`: `OutboundQueueScheduler.dispatch_once`; no state enum | stopped, stalled, cycle, permit_leak, busy, deferred, idle | memory | [delivery-recovery.md, Outbound worker](delivery-recovery.md#outbound-worker) |
| Worker slot | `outbound.py`: `WorkerSlots`, `ReservedSlot` | free, reserved, owned | memory | [delivery-recovery.md, Worker slots](delivery-recovery.md#worker-slots) |
| Head cache | `outbound.py`: `OutboundQueueScheduler._heal_heads`; the dirty destinations are `OutboundQueue._dirty_destinations` (`mark_destination_dirty`, `take_dirty_destinations`); no state enum | fresh, incremental, heal required | memory | [delivery-recovery.md, Head cache](delivery-recovery.md#head-cache) |
| Aggregation row | `aggregation_states.py`: `ContextKind`, `ContextState`, `ContextEvent`, `CONTEXT_TRANSITIONS` | logical, member_update, aggregate, source, frozen_source, frozen_redirect, handed_off, boundary | `outbound_queue.log_context` (marker `\x03`); `source_cancellations.log_context` for the withdrawn-source record (kind `cancellation`) | [text-aggregation.md, Queue row kinds](text-aggregation.md#queue-row-kinds) |
| Batch window | `live_aggregate.py`: the `waiting` and `due` fields of a logical or member_update context, read by `LiveTextAggregation.deadline`, which `dispatch_once` asks through `queued_aggregation_deadline`; no state enum | not waiting (due on arrival), waiting (a backfill-late first member, or the third member within 3 seconds: due `text_aggregation_window_seconds` after the first member), due | `outbound_queue.log_context` | This row |
| Live stream | `aggregation_states.py`: `StreamState`, `StreamEvent`, `STREAM_TRANSITIONS` | absent, open, closed | memory; rebuilt from queue rows at startup | [text-aggregation.md, Live streams](text-aggregation.md#live-streams) |
| Independent-delivery reservation | `live_aggregate.py`: `LiveTextAggregation.independent` takes the scheduler lock and `note_independent_enqueued` releases it after the first durable enqueue, or `independent` releases it on exit; a one-element `reserved` flag, no state enum | free, reserved, released | memory | [text-aggregation.md, Live streams](text-aggregation.md#live-streams) |
| Source member | `aggregate.py`: `MemberStatus`, `MemberEvent`, `MEMBER_TRANSITIONS` | active, removed, redirected | `msglog.pickle` and queued aggregation contexts; `source_cancellations.log_context` for a member removed while its change was still queued (kind `cancellation`) | [text-aggregation.md, Source members](text-aggregation.md#source-members) |
| Selection session | `selection_session.py`: `SelectionState`, `SelectionEvent`, `TRANSITIONS` | open, paged, chosen, cancelled, expired, invalidated | memory (15 minutes) | `selection_session.py` docstring; [text-aggregation.md, The `/aggregation` command](text-aggregation.md#the-aggregation-command) |
| History replay entry | `history_states.py`: `EntryState`, `EntryEvent`, `ENTRY_TRANSITIONS`, `entry_state` | absent, staged, published, queued | derived from `historymigrationentry.generation`, `historymigrationtarget` and the queue's `history_ownership` keys | [history-replay.md, Persistent state and compatibility](history-replay.md#persistent-state-and-compatibility) |
| Chat preference | no definition object | inherit (`NULL`), on, off | `slavechatinfo.text_aggregation_enabled` | [text-aggregation.md, Per-chat preference](text-aggregation.md#per-chat-preference) |
| Author emoji | `custom_emoji.py`: `AuthorEmojiState`, `AuthorEmojiEvent`, `AUTHOR_EMOJI_MACHINE` | absent, queued, creating, ready, failed, check queued, checking | memory; `ready` is persisted as `useremojicache` rows (`avatar_hash` holds the fingerprint of the avatar the emoji shows; an emoji made before this version has none and is made again once, on its author's next message, and its old sticker is removed) | README.rst, "Optional: Custom emoji for message authors" (`user_emoji` key) |
| User emoji feature | `custom_emoji.py`: `FeatureState`, `FeatureEvent`, `FEATURE_MACHINE` | enabled, disabled-by-config, unavailable | memory | README.rst, "Optional: Custom emoji for message authors" (`user_emoji` key) |
| Sticker set allocator | `custom_emoji.py`: `StickerSetState`, `StickerSetEvent`, `STICKER_SET_MACHINE` | unknown, open, full, error | memory | README.rst, "Optional: Custom emoji for message authors" (`user_emoji` key) |

Persisted spellings (`delivery_state`, hold prefixes, context kinds, member
statuses) are fixed across versions: rows written by an older version load and
progress under these definitions.
