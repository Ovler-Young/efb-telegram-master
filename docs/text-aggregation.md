# Text aggregation

## Source members

Text aggregation keeps every source message as a *source member*: the full
saved source (text, author, reply excerpt, time) plus a status and a revision.
Members are stored in the main database, in `msglog.pickle`, either inside a
live container (`aggregate.children`) or as the single `source_member` of an
independent output. The same member also appears inside queued aggregation
contexts until its receipt is confirmed.

### What the status means

| Status | Meaning | What the chat shows |
|---|---|---|
| `active` | The source is live in this output. | Author, time and text. |
| `removed` | The source was removed remotely; its content is kept. | The text struck through, or `[message removed]` / `[removed]` for older containers and empty text. |
| `redirected` | The member left its container for its own independent output (it no longer fits, or it carries media or commands). | `[message moved]`, or `[moved]` when the container is long. The member's `replacement_master_msg_id` names the new output. |

`source_revision` starts at 1 and grows with every new version of the source.
A newer revision always wins over an older one; an older snapshot confirmed
late never reverses a removal or a redirect.

An edit after a removal makes the member `active` again with the next
revision. This is intended: the source is live again, and the output shows the
edited text.

### Events and allowed changes

The definition lives in `efb_telegram_master/aggregate.py`: `MemberStatus`,
`MemberEvent`, the table `MEMBER_TRANSITIONS`, and `apply_member_event()`.
These call it: receipts and redirects in the `finalize_*` methods of `db.py`,
the preview of a container update in `LiveTextAggregation.materialize`,
removals in `remove_source_member`, and source edits in `slave_message.py`.
Queued rows carry the versions it returns; merging pending changes into a
queued row keeps the higher `source_revision` and changes no status itself.
The first receipt of a container has no saved members yet and stores them as
the batch carried them. `redirect` leaves `replacement_master_msg_id` to
`finalize_member_redirect`, which sets it once the replacement output is
saved.

| Event | From | To | Notes |
|---|---|---|---|
| `new_revision` | none, `active`, `removed`, `redirected` | `active` | The revision must grow. From `redirected` it is reachable only while a routing update of the old container is queued. |
| `remove` | `active`, `removed`, `redirected` | `removed` | Revision + 1. Removing a removed member changes nothing. From `redirected` it is reachable only while a routing update of the old container is queued. |
| `redirect` | `active`, `removed`, `redirected` | `redirected` | The replacement must be a version at least as new; an already confirmed redirect changes nothing. |
| `receipt` (container) | `active`, `removed` | `active`, `removed` | The receipt's copy (`active` or `removed`) replaces the saved one when its revision is newer, or equal while the saved copy is `active`. |
| `receipt` (container) | `redirected` | `redirected` | Confirmed replacement routing survives every container receipt. |
| `source_receipt` (independent output) | none, any | `active`, `removed` | Only a strictly newer revision replaces the saved member. |
| `stale_receipt` | any | unchanged | Reported when a receipt is older than what is saved; nothing is written. |

A combination that is not in the table, a `new_revision` that does not advance
the revision, or a redirect of an older version over a newer one raises
`MemberTransitionError` (a `ValueError`). The message names the member, the
event and its current status, for example
`Source member ('module.chat', 'msg-1') revision 2 does not follow 2.` or
`A stale receipt cannot redirect a newer source version.` Such an error
means a receipt or an edit reached a member in a state it cannot be in; the
delivery is retried like any other failed receipt. Report it with the log
lines around it.

Persisted spellings (`active`, `removed`, `redirected`, the key names of the
saved member, `format_version: 1`, and the display markers above) are fixed:
rows written by earlier versions load and progress unchanged.

## Per-chat preference

Each source chat has a text aggregation preference with three values, stored in
`slavechatinfo.text_aggregation_enabled` (a nullable boolean) and cached in
memory at startup (`_text_aggregation_overrides` in `db.py`):

| Stored value | Meaning |
|---|---|
| `NULL` | Inherit: follow the configured rules. |
| `True` | Aggregation is on for this chat, regardless of rules. |
| `False` | Aggregation is off for this chat, regardless of rules. |

Whether a new text message is aggregated is decided in this order:

1. The global `text_aggregation` flag. If it is off, nothing is aggregated, whatever the chat preference says.
2. The chat's stored value, when it is `True` or `False`.
3. Otherwise `text_aggregation_rules` when configured; else `text_aggregation_origins` and `text_aggregation_chat_types` (a missing filter allows everything). The decision is made in `Flags.allows_text_aggregation` in `utils.py`.

Messages with commands and non-text messages are never aggregated. Edits of a
source that is already a member are not subject to this decision.

### The `/aggregation` command

Bot admins use `/aggregation` in a group or topic linked to a source chat:

| Command | Effect |
|---|---|
| `/aggregation` | Show the current state and where it comes from (`inherited`, `chat override: on`, `chat override: off`). |
| `/aggregation on` | Store `True`. |
| `/aggregation off` | Store `False`. |
| `/aggregation reset` | Store `NULL` (inherit again). |

Any other argument gets the usage text. When a group links several source
chats, the command asks which chat to change. If the global switch is off, the
reply says so, because it overrides the preference.

## Queue row kinds

Every state machine in the queue and in text aggregation is listed in
[State machines](state-machines.md).

Every unpublished step of text aggregation is a row in the outbound queue. Its
`log_context` holds the marker byte `\x03` and a pickled dict whose `kind`
says what the row is doing; two flags, `frozen` and `handoff`, refine it.
These spellings are persisted and fixed. The row's delivery columns (queued,
in flight, held, `sent_pending`) are a separate machine, described in
`docs/delivery-recovery.md`.

The definition lives in `efb_telegram_master/aggregation_states.py`:
`ContextKind` (the stored spellings), `ContextState`, `ContextEvent`, the table
`CONTEXT_TRANSITIONS` and `context_state()`. The queue mutators in
`outbound.py` (`replace_logical_aggregation`, `close_aggregation_destination`,
`materialize_aggregation`, `handoff_aggregation`, `cancel_logical_aggregation`,
`cancel_pending_source`, `release_delivery_attempt`, and the batch split in
`enqueue_many`), `LiveTextAggregation.materialize` and `reconcile`, and
`TelegramBotManager.execute_queued_call` check it through
`check_context_transition()` before they write.

| State | Stored as | Meaning |
|---|---|---|
| `logical` | `kind='logical'` | An unsent batch of new members. It can still grow. |
| `member_update` | `kind='member_update'` | Pending source edits or removals for a confirmed container. `container_id` is empty until the container's first receipt; a routing update (`routing_hint`) marks a member as moved. |
| `aggregate` | `kind='aggregate'` | The frozen container send or edit, with its sender fixed. A new batch keeps its logical form in `retry_context`. |
| `source` | `kind='source'` or `'redirect'`, not `frozen` | One member's own output, not yet attempted. |
| `frozen_source` | `kind='source'`, `frozen` | The frozen standalone send, edit or removal edit of one member. |
| `frozen_redirect` | `kind='redirect'`, `frozen` | The frozen output of a member that left its container. |
| `handed_off` | `kind` `aggregate`, `source` or `redirect`, `handoff` | The receipt is written to MsgLog and passed to the rows behind it; the row is removed next. |
| `boundary` | `kind='boundary'` | An ordinary request (media, commands, a full-content attachment) ordered between batches. It is logged through its own `legacy_context`. |
| `cancellation` | `kind='cancellation'` in `source_cancellations` | A withdrawn unpublished member, kept so a later edit is not published. Nothing removes it: one small record per withdrawn source stays for the life of the queue file. |

| Event | From | To | Where |
|---|---|---|---|
| `replace` | `logical`, `member_update` | same | Append a member, merge an edit or removal, keep the head of a split batch. |
| `close_destination` | `logical`, `member_update`, `aggregate`, `handed_off` | same | A new Telegram message in the destination ends appending. Of the `handed_off` rows only a handed-off aggregate takes it, so a retried receipt cannot reopen the container. Other rows have no append flags and are skipped. |
| `materialize` | `logical`, `member_update` | `aggregate` | First attempt. |
| `materialize` | `source` | `frozen_source`, `frozen_redirect` | First attempt; the saved source decides which. |
| `materialize` | `aggregate`, `frozen_source`, `frozen_redirect`, `boundary` | same | Frozen requests are sent as stored. A removal edit is rebuilt from the saved standalone message, which also upgrades a removal stored in the former `delete_message` format. |
| `execute` | `aggregate`, `frozen_source`, `frozen_redirect`, `boundary` | same | The Telegram call. |
| `release` | `aggregate` | `logical`, `aggregate` | A definite rejection: a new batch returns to its logical form, a container update stays frozen for its owner. |
| `release` | `frozen_source`, `frozen_redirect`, `boundary` | same | |
| `receipt` | `aggregate`, `frozen_source`, `frozen_redirect` | `handed_off` | Write MsgLog, update the rows behind it. |
| `receipt` | `handed_off` | `handed_off` | A retry after the row could not be removed; nothing is applied twice. |
| `receipt` | `boundary` | `boundary` | Logged through its `legacy_context`. |
| `withdraw` | `logical`, `source`, `frozen_source` | none | Only before any attempt: an emptied batch, a batch replaced by its split, an unpublished member's pending outputs. |

A row is created as `logical`, `member_update`, `source`, `frozen_source` (a
removal edit of a confirmed standalone message) or `boundary`. Updating the
rows behind a confirmed one (setting their container, owner or base) changes
data, not their state.

Older rows keep loading: contexts without `appendable`, `container_id` or
`routing_hint` read the defaults, and a frozen removal stored as
`delete_message` is rebuilt as an edit. `generation` and `recent` have no
default: every stored batch carries them, and a batch row without them stops
the outbound worker at its first attempt.

### When a row is in a state its event does not accept

The check raises `IllegalAggregationTransitionError` and writes nothing. Its
message names the row, its kind and the event, for example
`Aggregation queue row 12 of kind 'member_update' does not accept materialize; the row was left unchanged. Deferred source update requires its confirmed container.`

* **At the first attempt or the Telegram call** (`materialize`, `execute`), the
  outbound worker stops, as for any queue persistence error, and stops again
  after a restart. Find the row with
  `python3 -m efb_telegram_master.queue_report --data-dir <channel data directory>`
  and report the message with the log lines around it.
* **At receipt reconciliation**, the scheduler logs once per row
  `Receipt of queue row 12 (aggregation kind 'logical') cannot be reconciled; it stays queued and is retried: ...`
  and keeps retrying on the usual backoff. Other rows continue.
* **For a merge, a withdrawal of a pending output or a release**, the row is
  left as it is and a WARNING says why (`Aggregation batch not replaced: ...`,
  `Pending source not withdrawn: ...`, `Send attempt not released: ...`). A
  refused merge or withdrawal is handled like a row that was already sent: the
  change goes into a new row.

## Live streams

A live stream is the in-memory append state of one source in one destination
(the key is origin, chat and topic). It is never stored; at startup it is
rebuilt from the queued rows. It decides whether a new member joins the
current container or starts a new one.

The definition lives in `efb_telegram_master/aggregation_states.py`:
`StreamState`, `StreamEvent`, `STREAM_TRANSITIONS` and `CloseReason`, used by
`LiveTextAggregation` in `live_aggregate.py`.

| State | Meaning |
|---|---|
| `absent` | No member or independent delivery for this key since startup. |
| `open` | The next member may join the current batch or edit the current container. |
| `closed` | The current container ended; the next member starts a new one. The previous container is remembered for one `reopen`. |

| Event | absent | open | closed |
|---|---|---|---|
| `append` | open | open | open |
| `close` | absent (no-op) | closed | closed |
| `reopen` | absent (no-op) | open | open (the previous container is restored) |

`LiveTextAggregation` changes a stream's state only through `_Stream.apply`,
which follows `STREAM_TRANSITIONS`. `reopen` acts only when the discarded row
is still the stream's last row; otherwise a later row has already continued
the run and nothing changes. Both no-ops are logged at DEBUG. A container
receipt changes no stream state: it makes the confirmed container the one to
edit next, and only when it belongs to the stream's current generation.

Every close names its reason (`CloseReason`):

| Reason | Cause |
|---|---|
| `telegram-destination` | A new Telegram message arrived in the destination. |
| `route-changed` | The source now delivers to another chat or topic. |
| `idle` | No new member for `text_aggregation_idle_seconds`. |
| `capacity` | The pending batch is full. |
| `independent-delivery` | A media, command or oversized message is sent on its own. |
| `split-tail-<kind>` | Splitting a pending batch left a `boundary`, `source` or `redirect` row last. |

At most once a minute the closes are logged as a WARNING with a tally, for example
`Live aggregation container closes in the last interval: {'idle': 3, 'capacity': 1} (latest capacity)`.
Use it to tell which rule split a run of messages into several containers.
`capacity` closes follow `text_aggregation_max_members` and
`text_aggregation_max_payload_bytes`.
