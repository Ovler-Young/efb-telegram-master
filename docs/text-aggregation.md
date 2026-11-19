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
`MemberEvent`, the table `MEMBER_TRANSITIONS`, and `apply_member_event()`. Every
place that changes a member's status goes through that function: the
`finalize_*` methods in `db.py`, the member-update merge, source removal and
pending-member changes in `live_aggregate.py`, and source edits in
`slave_message.py`.

| Event | From | To | Notes |
|---|---|---|---|
| `new_revision` | none, `active`, `removed`, `redirected` | `active` | The revision must grow. From `redirected` it is reachable only while a routing update of the old container is queued. |
| `remove` | `active`, `removed`, `redirected` | `removed` | Revision + 1. Removing a removed member changes nothing. From `redirected` it is reachable only while a routing update of the old container is queued. |
| `redirect` | `active`, `removed`, `redirected` | `redirected` | The replacement must be a version at least as new; an already confirmed redirect changes nothing. |
| `receipt` (container) | `active`, `removed` | `active`, `removed` | A newer or equal-revision `active` copy replaces the saved one. |
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
