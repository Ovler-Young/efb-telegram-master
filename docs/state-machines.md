# State machines

Every lifecycle that changes persisted rows or drives the outbound worker has
one definition in code, and the code that changes that state goes through it.
A change the definition does not allow is rejected or logged with the row and
the attempted event; it is never applied silently. This page lists them; the
linked sections give states, transitions, log lines and operator actions.

| Machine | Defined in | States | Kept in | Details |
|---|---|---|---|---|
| Queue row delivery | `delivery_states.py`: `RowState`, `Event`, `TRANSITIONS` | waiting, deferred, in_flight, uncertain, invalid_payload, history_failed, supplement_failed, aggregation_failed, unrecognized_hold, sent_pending | `outbound_queue.delivery_state`, `delivery_hold`, `reconcile_after` | [delivery-recovery.md, Queue row states](delivery-recovery.md#queue-row-states) |
| Ordering link | `delivery_states.py`: `BARRIER_STATES`, `is_ready` and the SQL builders (copied in `queue_report.py`) | free, barrier, void, cycle | `outbound_queue.predecessor_id` | [delivery-recovery.md, Ordering links](delivery-recovery.md#ordering-links) |
| Dispatch pass | `outbound.py`: `PassOutcome`, returned by `OutboundQueueScheduler.dispatch_once` | stopped, stalled, cycle, permit_leak, busy, deferred, idle | memory | [delivery-recovery.md, Outbound worker](delivery-recovery.md#outbound-worker) |
| Worker slot | `outbound.py`: `WorkerSlots`, `ReservedSlot` | free, reserved, owned | memory | [delivery-recovery.md, Worker slots](delivery-recovery.md#worker-slots) |
| Head cache | `outbound.py`: `OutboundQueueScheduler` (`_heal_heads`, dirty destinations); no state enum | fresh, incremental, heal required | memory | [delivery-recovery.md, Head cache](delivery-recovery.md#head-cache) |
| Aggregation row | `aggregation_states.py`: `ContextKind`, `ContextState`, `ContextEvent`, `CONTEXT_TRANSITIONS` | logical, member_update, aggregate, source, frozen_source, frozen_redirect, handed_off, boundary, cancellation | `outbound_queue.log_context` (marker `\x03`), `source_cancellations` | [text-aggregation.md, Queue row kinds](text-aggregation.md#queue-row-kinds) |
| Live stream | `aggregation_states.py`: `StreamState`, `StreamEvent`, `STREAM_TRANSITIONS` | absent, open, closed | memory; rebuilt from queue rows at startup | [text-aggregation.md, Live streams](text-aggregation.md#live-streams) |
| Source member | `aggregate.py`: `MemberStatus`, `MemberEvent`, `MEMBER_TRANSITIONS` | active, removed, redirected | `msglog.pickle` and queued aggregation contexts | [text-aggregation.md, Source members](text-aggregation.md#source-members) |
| Selection session | `selection_session.py`: `SelectionState`, `SelectionEvent`, `TRANSITIONS` | open, paged, chosen, cancelled, expired, invalidated | memory (15 minutes) | `selection_session.py` docstring; [text-aggregation.md, The `/aggregation` command](text-aggregation.md#the-aggregation-command) |
| History replay entry | no definition object; `ChatBindingManager` runs the per-(chat, topic) lanes and a once-per-process ownership sweep | staged, published, queued | `historymigrationentry`, `historymigrationtarget` | [history-replay.md, Persistent state and compatibility](history-replay.md#persistent-state-and-compatibility) |
| Chat preference | no definition object | inherit (`NULL`), on, off | `slavechatinfo.text_aggregation_enabled` | [text-aggregation.md, Per-chat preference](text-aggregation.md#per-chat-preference) |
| Author emoji | `custom_emoji.py`: `AuthorEmojiState`, `AuthorEmojiEvent`, `AUTHOR_EMOJI_MACHINE` | absent, queued, creating, ready, failed, check queued, checking | memory; `ready` is persisted as `useremojicache` rows (`avatar_hash` holds the fingerprint of the avatar the emoji shows) | README.rst, "Optional: Custom emoji for message authors" (`user_emoji` key) |
| User emoji feature | `custom_emoji.py`: `FeatureState`, `FeatureEvent`, `FEATURE_MACHINE` | enabled, disabled-by-config, unavailable | memory | README.rst, "Optional: Custom emoji for message authors" (`user_emoji` key) |
| Sticker set allocator | `custom_emoji.py`: `StickerSetState`, `StickerSetEvent`, `STICKER_SET_MACHINE` | unknown, open, full, error | memory | README.rst, "Optional: Custom emoji for message authors" (`user_emoji` key) |

Persisted spellings (`delivery_state`, hold prefixes, context kinds, member
statuses) are fixed across versions: rows written by an older version load and
progress under these definitions.
