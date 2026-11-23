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
`MemberEvent`, and `apply_member_event()`.
These call it: receipts and redirects in the `finalize_*` methods of `db.py`,
the preview of a container update in `LiveTextAggregation.materialize`,
removals in `remove_source_member`, and source edits in `slave_message.py`.
Queued rows carry the versions it returns; merging pending changes into a
queued row keeps the higher `source_revision` and changes no status itself.
The first receipt of a container has no saved members yet and stores them as
the batch carried them. `redirect` leaves `replacement_master_msg_id` to
`finalize_member_redirect`, which sets it once the replacement output is
saved.

A newer revision wins: a `new_revision` must advance the revision, and
removing a removed member changes nothing. A `redirect` needs a version at
least as new as the saved one, and an already confirmed redirect changes
nothing. A container receipt replaces the saved member when its revision is
newer, or equal while the saved member is `active`; confirmed replacement
routing (`redirected`) survives every container receipt. An independent
output's receipt replaces the saved member only with a strictly newer
revision. A receipt older than what is saved is reported as `stale_receipt`
and nothing is written.

A `new_revision` that does not advance the revision, or a redirect of an
older version over a newer one, raises `MemberTransitionError` (a
`ValueError`). The message names the member, for example
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

The state machines of text aggregation are described in this guide, and the
queue's in the [delivery recovery guide](delivery-recovery.md).

Every unpublished step of text aggregation is a row in the outbound queue. Its
`log_context` holds the marker byte `\x03` and a pickled dict whose `kind`
says what the row is doing; two flags, `frozen` and `handoff`, refine it.
These spellings are persisted and fixed. The row's delivery columns (queued,
in flight, held, `sent_pending`) are a separate machine, described in
`docs/delivery-recovery.md`.

The definition lives in `efb_telegram_master/aggregation_states.py`:
`ContextKind` (the stored spellings), `ContextState` and `context_state()`.

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

A row takes a Telegram receipt only when it is an aggregate, a frozen output,
already handed off or a boundary; `check_receipt()` in `outbound.py` refuses any
other.

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

* **At the first attempt** (`materialize`), the
  outbound worker stops, as for any queue persistence error, and stops again
  after a restart. Find the row with
  `python3 -m efb_telegram_master.queue_report --data-dir <channel data directory>`
  and report the message with the log lines around it. When the queue is in
  PostgreSQL, add `--dsn "host=... dbname=... user=..."` (the password comes from
  `PGPASSWORD` or `~/.pgpass`, never the command line). An install whose
  `database.options` sets `search_path` puts the same schema in the DSN
  (`options=-csearch_path=<schema>`). With `--dsn`, `--data-dir` is used only
  to size `outbound-media/`, and the script needs only `psycopg2`. Without `--dsn` it
  refuses a data directory whose queue moved to PostgreSQL, because the retained
  SQLite copy shows stale rows.
* **At receipt reconciliation**, the scheduler logs once per row
  `Receipt of queue row 12 (aggregation kind 'logical') cannot be reconciled; it stays queued and is retried: ...`
  and keeps retrying on the usual backoff. Other rows continue.

## Live streams

A live stream is the in-memory append state of one source in one destination
(the key is origin, chat and topic). It is never stored; at startup it is
rebuilt from the queued rows. It decides whether a new member joins the
current container or starts a new one.

The definition of `CloseReason` lives in
`efb_telegram_master/aggregation_states.py`; `LiveTextAggregation` in
`live_aggregate.py` closes and reopens streams. A `close` ends the current
container and remembers it until the next close or reopen. A `reopen` restores
the remembered container, and acts only when the discarded row is still the
stream's last row; otherwise a later row has already continued the run and
nothing changes. An append leaves the remembered container alone, so a
`reopen` after members were appended and withdrawn still restores it. A
container receipt changes no stream state: it makes the confirmed container the
one to edit next, and only when it belongs to the stream's current generation.

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

## Author custom emoji

An author header in a combined message shows the author's custom emoji (the
`user_emoji` section of the README) when the emoji was ready as the source
arrived; otherwise it shows the plain header, and the emoji is created in the
background for that author's later messages. A source already in a container
keeps the header it arrived with when the container is edited later. The
container is rendered from memory when it is sent or edited, without waiting
for an avatar download or emoji creation, and a sender bot that may not send
custom emoji gets plain headers.
