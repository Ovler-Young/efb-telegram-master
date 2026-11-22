# Recovering an unconfirmed send without sending it again

A timeout or disconnected HTTP response is not proof that Telegram rejected a
message. The remote side may have accepted it. Message-creating queue operations
and text/caption edits persist `delivery_hold='in_flight'` before executor
submission. The hold is cleared only for a definite rejection/pre-send connection
failure, or by saving an actual completion receipt. An interrupted attempt stays
held across restarts. An ambiguous failure of a send is stored as
`uncertain:<exception-class>/<cause-class>`. Whenever a row is held after a
Telegram error (uncertain or rejected), the error's text is kept next to the hold
in `hold_reason`, cut to 200 characters: Telegram's description for a rejection,
such as `Chat not found`, or the transport's, such as `Timed out`. Other
exceptions leave it empty. No message contents, request objects, tokens or exception
tracebacks are stored in either column.

Held rows keep their payload, sidecar and original MsgLog context. They are not
silently marked sent. Other runnable rows, including newer
messages to the same chat, continue. Definite RetryAfter rejections still retry
at the requested deadline. ConnectError/ConnectTimeout/PoolTimeout causes are
also retryable because the HTTP request was not sent. Read/write failures and
unknown network outcomes are not blindly retried for message-creating calls.

A text or caption edit (`edit_message_text`, `edit_message_caption`) whose
result is ambiguous for the same reasons is not held, unless it appends new
messages to an aggregated container: that edit is their only copy, so it is held
like a send. Repeating any other edit over the same text is harmless, while
replaying an old edit over newer text is not. So the edit is released and tried
once more after the transport retry delay, still ahead of the later rows of its
chat and topic. If Telegram answers that try with "message is not modified", the
earlier try was applied: a text edit is completed as delivered and its MsgLog
update is written. A caption edit is held as
`uncertain:InvalidTelegramResponseError/BadRequest` instead, because only
Telegram's answer would tell MsgLog the message's media. The edit is dropped, and
its MsgLog update with it, when a later queued row edits the same message with its
complete text (an edit made in the bot or a source-side edit of a standalone
message), or when Telegram did not confirm an earlier try of it either. That
earlier try is recorded in `unconfirmed_at`, so a second ambiguous result drops
the edit after a restart too. A try refused before it reached Telegram (a
connection failure) is retried as usual and does not count. A later update of an
aggregated container does not count as a newer edit. It is built from the
confirmed container and does not carry this edit's content, so the older edit
gets its second try first. The log shows `Telegram did not confirm the edit ...
trying it once more` or `... dropped because ...`. A blocking caller of a dropped
edit gets the transport error. An edit interrupted by a restart stays
`in_flight`, like a send.

This is not an exactly-once transport guarantee: after loss of the response,
remote success requires evidence. No increasing retry count, arbitrary file-size
limit, or longer timeout can supply it; the destination chat can. With
`api_id` and `api_hash` set, the bot reads the chat back and ends such holds by
itself ([Checking held sends against the chat](#checking-held-sends-against-the-chat)).
Otherwise an operator ends them with `/confirm_send`.

## Checking held sends against the chat

Add both keys to the channel's `config.yaml` (the values of an application
registered at my.telegram.org). Without both, nothing is read, no extra login
is made, and held sends wait for `/confirm_send`.

```yaml
token: "12345678:1a2b3c4d5e6g7h8i9j"
api_id: 123456
api_hash: "0123456789abcdef0123456789abcdef"
```

Each held send is read back by the bot that attempted it: the main bot, or the
auxiliary bot recorded in `attempt_sender_bot_id`, logs in with its own token
over MTProto, keeps its session in memory only and takes no updates. It only
looks up the chat and reads messages by id; it never sends, edits or deletes. A
row whose bot is no longer configured stays held.

The check runs in its own thread, about a minute after the send worker starts
and then every 10 minutes, so the first pass covers every send held at
startup. It reads rows held as `uncertain:<Class>` with a recorded attempt time,
in supergroups, for `send_message`, `send_photo`, `send_animation`,
`send_video`, `send_document`, `send_sticker`, `send_audio`, `send_voice`,
`edit_message_text` and `edit_message_caption`. Operator holds
(`uncertain:operator_observed_delivery`), interrupted attempts (`in_flight`)
and every other hold are left as they are. A row is decided once its attempt
is 11 minutes old. Each bot makes at most one call every 15 seconds and waits
out a flood wait, so a large first pass takes a while; sends are not slowed by
it.

A send is looked for among the message ids between the last message logged for
its source chat before the attempt and the first one logged after it, reading
on until a message dated after the window is seen. A message matches when it
is from the same bot, not a forward, not already in the message log, dated from
5 seconds before to 10 minutes after the attempt, of a compatible type, in the
same topic, and carries the same text or caption (a caption Telegram cut, by
its start). Sends that look the same are matched to one message each. An edit
was applied when the message it changes shows its text.

| The chat shows | What the bot does |
|---|---|
| a matching message | Records it as the delivery, as `/confirm_send` does, and never sends the row again. The log entry has the message's id, author and media type but no Bot API file id. |
| no such message, the attempt is less than a day old and every media file it names is still there | Clears the hold, so the send goes out again, after the messages sent since. An edit is dropped instead when a newer edit of the same message is queued, or the message was edited after the attempt. |
| no such message, otherwise | Discards the row, with the container edits that need it (as `needs_removed_row` below). A queue-owned file is deleted only when no remaining row names it; external media files are never deleted. |
| nothing that decides it (several candidates, a near miss with other text, a span it could not close) | Nothing: the row stays held. It is read again after a restart. |

A discarded or confirmed row never needs `/confirm_send`; the command still works
for every row the check leaves held. Each pass logs one line with counts only,
for example `Held-send check pass: ambiguous=2, calls=14, flood_waits=0,
found=5, not_found=1, too_early=3`; message text, chat titles, tokens and the API values
are never logged.

## An already repeating file from an older version

1. Stop EFB and its automatic restarts. Keep the queue, media files and MsgLog.
2. Identify the **queue row ID** from the matching EFB failure log. A repeated
   filename alone does not distinguish one retrying row from several source rows.
3. Install this revision while EFB is stopped. Inspect the row using:

   ```sh
   python -m efb_telegram_master.hold_send --data-dir /channel/data --row-id ROW_ID
   ```

   After checking the reported operation and chat, add `--apply`. This records
   an operator hold before restarting, so the old queued row is not sent once
   more on startup. It changes only queue metadata, never media or MsgLog. The
   helper refuses to run while the data-directory lock is held by EFB.

   When the queue is in PostgreSQL, add `--config` with the private YAML file that
   holds the same `database` settings the bot uses (only its `database` mapping is read):

   ```sh
   python -m efb_telegram_master.hold_send --data-dir /channel/data --row-id ROW_ID \
     --config /private/path/postgresql.yaml
   ```

   It then holds the row in PostgreSQL and also refuses while a running ETM owns the
   queue, even one started from another data directory or host. After the queue moved
   into PostgreSQL, running it without `--config` is refused, because the retained
   `outbound-queue.sqlite3` is a frozen copy that no longer sends.
4. Start EFB. As a configured EFB administrator, **reply to the original, already
   delivered bot message** with `/confirm_send ROW_ID`. A forward is not accepted.
   The chat and sending bot must match the recorded attempt. An operator hold
   from an old version accepts only one of the currently configured bots.
5. The reply supplies the actual Telegram Message and file ID. The handler keeps
   the actual author separate from the bot that issued the observed file ID.
   It saves both in the receipt and invokes MsgLog reconciliation without
   resending the confirmed message. The queue row/media are removed only after
   successful MsgLog reconciliation. On a database failure the receipt stays
   `sent_pending` and is retried as a log write, never as another upload.

The command deliberately does not delete any duplicate messages already on
Telegram and does not fabricate a message ID. Confirming the wrong queued item
would create a wrong mapping; choose the actual row and its actual delivered
message. If no delivered message can be established, keep the row held and
investigate. Only the [chat check](#checking-held-sends-against-the-chat) sends a
held row again, and only after reading the chat and not finding it.

Do not downgrade to a revision that ignores `delivery_hold`: it can replay held
rows. The current decoder accepts older author-only receipts and new receipts
with a separate file-ID issuer; older code cannot decode the new three-element
receipts, even though their format prefix is unchanged. This is not rollback
compatibility. The migration backup includes the queue columns and receipts.
Historically misrecorded file owners are not retroactively repaired.

## Primary messages and supplemental work

For long text or captions, the primary response receipt and a separate durable
full-content attachment request commit together. MsgLog reconciles the primary
message independently of the attachment. A failed attachment does not cause the
primary message to be resent. Permanent supplemental rejection retains the work
with a `supplement_failed:` hold; an ambiguous supplemental send stays uncertain
and needs confirmation of that supplemental row. Confirming a held primary
message also preserves any required supplemental work.

History media acquisition has a different retry boundary: after an explicit copy
rejection, a transient `get_file` or download failure can retry before any
fallback upload is attempted. Acquisition backoff persists across restart,
caps its exponential component at 60 seconds and respects a longer RetryAfter.
Permanent acquisition rejection or an unavailable issuer retains failed work.
A lost copy or upload response still needs confirmation; it never authorizes an
acquisition retry that would duplicate an uncertain send.

The file-ID issuer is used only for acquisition. Normal sends, supplemental sends
and history text batching use ordinary sender selection, without pinning to the
original bot. Edit/delete routing continues to use the actual author.

## Diagnostics and streaming

`etm_outbound_failures_total{stage="uncertain"}` counts newly detected ambiguous
results of sends. A retried edit counts as a retry with reason `unconfirmed_edit`,
and a dropped edit as a `terminal` failure. A healthy worker can coexist with held
records; queue depth alone does not mean that those records are awaiting a safe
automatic retry.

Aggregation recovery and source lookups read `log_context` and ordering only;
`aggregation_rows()` returns no payload bytes, so a large legacy inline media
row cannot be pulled into memory by a recovery pass.

A dispatch pass that leaves queued rows with no retry deadline and nothing in
flight logs `Outbound scheduler left N dispatchable head(s) undispatched ... while
queued rows remain` and rescans 30 seconds later instead of sleeping until the
next message or restart. That covers a row waiting behind another row and a row
whose readiness filter excludes it from every head. Worker permits held without
a submitted call cannot be released by a completion, so they are reset and
reported the same way. All of these mean delivery is still being attempted, not
that rows were dropped. [Outbound worker](#outbound-worker) lists every way a
pass can end.

A dependent row is deferred only while the row it links to is itself still
waiting: queued and unheld, in flight, or delivered and awaiting its MsgLog write.
A predecessor that is gone, permanently held, or linked into a cycle is not an
ordering barrier — waiting for it either already happened or can never happen —
so it cannot strand everything queued behind it. Held rows themselves are kept
until the chat check or an operator resolves them. When every waiting row defers to another waiting
row there is no valid order at all; the scheduler then dispatches the oldest row
per destination and logs `deferred by predecessor links that never resolve`:

```sql
SELECT q.id, q.predecessor_id, p.id IS NOT NULL AS predecessor_exists
FROM outbound_queue q LEFT JOIN outbound_queue p ON p.id = q.predecessor_id
WHERE q.delivery_state = 'queued' AND q.delivery_hold IS NULL
  AND NOT EXISTS (SELECT 1 FROM outbound_queue p2 WHERE p2.id = q.predecessor_id)
LIMIT 20;
```

```sql
SELECT id, operation, telegram_chat_id, delivery_state, delivery_hold, hold_reason,
       attempt_sender_bot_id, attempt_started_at,
       completion_receipt IS NOT NULL AS has_receipt
FROM outbound_queue
WHERE delivery_hold IS NOT NULL;
```

`python -m efb_telegram_master.queue_report` lists held rows grouped by hold and
Telegram's error text (`held rows: Telegram's error text`) and prints
`hold_reason` in a single row's details.

Startup sweeps unreferenced media sidecars only for a queue of at most
`MAX_ORPHAN_SCAN_ROWS` rows, because the sweep opens one blob reader per
queued row. A larger queue logs `Skipping the outbound media sweep` and keeps
every sidecar; the sweep resumes as the queue drains. Delivery never depends
on it.

Sidecar uploads are wrapped with PTB `InputFile(read_file_handle=False)`, including
nested input media. Passing a normal file object to PTB would otherwise read the
whole attachment before the HTTP request, defeating the sidecar memory benefit.
Recovered legacy media retains its original filename and MIME metadata through
sidecar storage and multipart upload. Failed preparation removes only newly
created sidecars; failed decoding closes handles opened before the failure.
Undecodable queued payloads remain held as `invalid_payload`, preserving the row
and existing media while allowing later runnable rows to continue. This hold
indicates a payload problem, not evidence that Telegram accepted the message.
Configured request timeouts remain unchanged; expiry never authorizes an
unconfirmed duplicate upload.

## Queue row states

The queue's state machines are described in this guide, and text aggregation's
in the [text aggregation guide](text-aggregation.md).

Every row of the outbound queue (the `outbound_queue` table, in `outbound-queue.sqlite3`
on SQLite and in the same PostgreSQL schema as the message tables on PostgreSQL) is in
exactly one state, read from its `delivery_state` and `delivery_hold` columns (and `reconcile_after` for a
deferred row). `hold_reason` only explains a hold with the error's text, and
`unconfirmed_at` only marks an edit whose earlier try Telegram did not confirm;
neither decides a state. A queue written by an earlier version gains both
columns when this version opens it (SQLite) or at database startup
(PostgreSQL); rows from before then have them empty. An earlier version still
runs on the upgraded queue and ignores the columns, but its `migrate_db` refuses to move an upgraded
SQLite queue (`Unsupported columns`), so move a queue with the version that last
ran the bot. Verify or finish a move with the version that started it: this
version cannot verify a move made by a version without these columns. On a queued row `reconcile_attempts` counts its failed attempts
in a row; from 3 on (`FAILED_ATTEMPTS_LIMIT`) it no longer holds back the rows
ordered after it (see [Ordering links](#ordering-links)). The definition lives
in `efb_telegram_master/delivery_states.py`: `RowState` (states), `HoldKind` (stored hold spellings), `Event`, `TRANSITIONS`
(the allowed-transition table), `classify`, `parse_hold` and `format_hold`.
Stored spellings never change between versions.

| State | `delivery_state` / `delivery_hold` | Meaning | Operator action |
|---|---|---|---|
| waiting | `queued` / NULL | Will be sent when its destination's turn comes. | None. |
| deferred | `queued` / NULL, `reconcile_after` ahead | Media acquisition failed before any send, or a send kept failing with a retryable error (a connection that never reached Telegram, a chat migration) for 3 attempts; retried after a backoff. Earlier failed sends are retried after a backoff the scheduler keeps in memory, and the row stays waiting. | None. |
| in_flight | `queued` / `in_flight` | Attempt recorded before submission. After a restart it is an interrupted attempt that may have been delivered; opening the queue counts it as 3 failed attempts, so later rows no longer wait for it. | If delivered, `/confirm_send`; otherwise investigate. |
| uncertain | `queued` / `uncertain:<Class>[/<Cause>]` | Ambiguous result of a send: Telegram may have accepted it. An edit is retried once or dropped instead, unless it appends new messages (see the top of this guide). | With the chat check on, none: it confirms, sends again or discards the row. Otherwise, or for a row it leaves held, `/confirm_send` on the delivered message. |
| uncertain (operator) | `queued` / `uncertain:operator_observed_delivery` | Held offline by `hold_send` for a send already seen on Telegram. | `/confirm_send`; any configured bot is accepted. |
| invalid_payload | `queued` / `invalid_payload` | The stored request cannot be decoded. Not evidence of delivery. | Investigate the row. |
| history_failed | `queued` / `history_failed:<Class>` | History replay was rejected. Startup re-queues `RequiredSenderUnavailableError` rows that never started an attempt. | Investigate, or `/confirm_send` if it was delivered. |
| supplement_failed | `queued` / `supplement_failed:<Class>` | The full-content attachment of a long message was rejected; the primary message is unaffected. | Investigate the row. |
| aggregation_failed | `queued` / `aggregation_failed:<Class>` | An aggregated text batch, or a row others are ordered behind, failed for good. A batch that was never sent as one Telegram message cannot be confirmed with `/confirm_send`. | Investigate the row. |
| unrecognized hold | `queued` / any other text | A hold written by another version. Kept and never sent. | Use the version that wrote it, or `/confirm_send`. |
| sent_pending | `sent_pending` / NULL | Telegram receipt saved; the MsgLog write is retried with backoff. | None. |
| absent | no row | Not yet enqueued, or removed. | None. |

While the bot runs, a hold ends by `/confirm_send`, by the
[chat check](#checking-held-sends-against-the-chat) for an `uncertain` row, and
at startup for the `history_failed` rows named above (when the queue opens, the
schema migration clears only `history_failed:RequiredSenderUnavailableError` on
a history row that never started an attempt). Apart from the chat check, the bot
never removes a held row or its media by itself. A held row that can never be
sent is cleared offline. It is removed; or, for a row found not delivered, its
caption is sent as text when its media file is gone, or the row is sent again
when it was held after a timeout and its files are still there:

```sh
python -m efb_telegram_master.clear_unsendable --data-dir /channel/data
python -m efb_telegram_master.clear_unsendable --data-dir /channel/data \
    --not-delivered not-delivered.txt --apply
```

Without `--apply` it only prints counts. With a PostgreSQL queue, add `--config`
with the bot's `database` settings, as for `hold_send`. It refuses while a bot
owns the queue (the data directory's lock files, or the PostgreSQL queue's
session lock), so stop EFB first. It never sends anything itself and decides
from the rows and files present when it runs. These rows are removed:

| Reason | Row |
|---|---|
| `missing_media` | Held; an external media file it names (a local path or `file://` URI, such as a file staged for a local Bot API server) is gone. Web URLs are fetched again on replay and do not count. A listed row with a caption may send its caption instead (below). |
| `missing_sidecar` | Held; a queue-owned file it names in `outbound-media/` is gone. A listed row with a caption may send its caption instead (below). |
| `invalid_payload` | Held as `invalid_payload`. |
| `rejected_batch` | Held as `aggregation_failed:BadRequest`: Telegram rejected the batch. It is removed even when listed, never sent again. |
| `needs_removed_row` | Held or waiting edit of a container that does not exist yet (a `member_update` without `container_id`, see [Ordering links](#ordering-links)): the removed row would have created it. Repeated for edits that wait on such an edit in turn. |

`--not-delivered` names a text file of queue row ids, one per line: the held
rows found not delivered, checked against the chat just before the clear. The
tool itself does not look in the chat. Only listed rows are sent, in two ways:

* **Caption as text.** A held send of one media file (`send_photo`,
  `send_video`, `send_document` and the like) whose file is gone and whose
  caption has text from the sender becomes a text send of its caption
  (`caption_sent_as_text`). When the chat shows a sender header, a stored
  caption starts with it: a line ending in `:` followed by a newline. For a
  message without text the header is followed by nothing or by the bot's own
  placeholder for the media (the `default_media_prompt` emoji or text, in any
  of the bot's languages). Such a caption, the header (ignoring formatting)
  with only that placeholder or nothing after it, is not sent: the row is
  removed and counted as `caption_header_only`. A caption without a header
  line, such as a one-line text of the sender that ends in `:`, is the
  sender's text and is sent.
* **Sent again.** A send held as uncertain after a timeout or a cut connection
  (`uncertain:TimedOut...` or `uncertain:NetworkError...`) whose media files
  are all still there is released unchanged (`resend`): its hold, attempt and
  failure count are cleared and the bot sends it on its next start. An
  operator hold and a row without media are not released.

Every other row whose file is gone is removed, including a delivered one, whose
caption is already in the chat; such an unlisted removed row with a caption is
counted as `caption_not_sent`. Every other held row stays held. Without
`--not-delivered`, nothing is sent, every row whose file is gone is removed, and
the output says so. Ids in the file that do not name such a row change nothing.
A member of a text batch (an aggregation context other than `boundary`) is
neither converted nor released: it is built again from its batch when sent. A
listed member that would otherwise be sent again stays held and is counted as
`resend_batch_member`.
Take the list after the bot's last stop and do not reuse it once the bot has run: a
send released once that timed out again would be released again by the same list.
A full-content attachment's caption only announces the file, so that row is
removed.

A row that sends its caption keeps its id, chat, topic, position and MsgLog
context; its request becomes `send_message` with the caption as `text` and the
caption's formatting, reply and buttons, without the file or the options only
media take. Its hold is cleared and it must be sent by the bot that attempted it
(`required_sender_bot_id`; the main bot when no auxiliary bot did). A row sent
again keeps its stored request and sender. Both are sent through the normal
queue and mapped in MsgLog like any sent row; rows ordered behind them wait for
them again.

It never clears an `in_flight` or `sent_pending` row or an unrecognized hold.
If a removed row had in fact been delivered, that message keeps no MsgLog
mapping, as for a send nobody confirmed; if a listed row had in fact been
delivered, its caption, or the whole message, is sent a second time. Every
other row keeps its stored bytes; a link to a removed row becomes void, so a
row that was only ordered behind it is ready. A queue-owned file of a removed
row, or of a row that now sends only its caption, is deleted when no remaining
row names it; while any remaining row's media cannot be read, all files are
kept. External media files are never deleted. The output has counts only: all
rows, removed rows by reason and by operation, `caption_sent_as_text`,
`caption_header_only`, `caption_not_sent`, `resend`, `resend_batch_member`,
kept rows (including those sending a caption or sent again), kept rows that
were ordered behind a removed row (`released`), and deleted files. To undo
`--apply`, restore the queue (`outbound-queue.sqlite3` and `outbound-media/`, or
the PostgreSQL queue tables) from a backup taken before it.

To list the held sends that the chats show were not delivered, for the clear
tool's `--not-delivered` file, run the chat check once, read-only, while the bot
can still run:

```sh
python -m efb_telegram_master.delivery_check --data-dir /channel/data \
    --not-delivered-out not-delivered.txt
```

It reads the bot tokens, the `database` settings and the `api_id` and `api_hash`
keys from the channel's `config.yaml` (no option carries a value), reads every
held send back by the bot that attempted it as the check above does, and writes
the ids of the rows found not delivered, one per line. It opens the queue file
and the message log read-only (SQLite `mode=ro`, or a read-only PostgreSQL
transaction), never through the queue class, so no schema change is made to a
file an older version owns, and it sends, edits and deletes nothing. A row the
chat does not decide (several candidates, a near miss, an attempt less than 11
minutes old, an unreadable chat or a bot that is not configured) is left out of
the file. The output is counts only. If a bot cannot log in, it prints that and
exits 1 without writing the file. At one call per bot every 15 seconds, a large
queue takes about an hour.

A new row is written as waiting by `OutboundQueue.enqueue_many`, the aggregation
handoff and the supplement insert; it has no earlier state to check, so the table
below lists only events on an existing row.

| Event | From | To | Code |
|---|---|---|---|
| begin_attempt | waiting, deferred | in_flight | `begin_delivery_attempt` (an aggregation row that `materialize` already moved to in_flight is left unchanged) |
| materialize | waiting, deferred | in_flight | `materialize_aggregation` |
| release_attempt | in_flight | waiting | `release_delivery_attempt` (submission failed or Telegram definitely rejected; an aggregate rewinds to its logical batch) |
| hold_uncertain | waiting, deferred, in_flight | uncertain | `hold_uncertain_delivery` |
| operator_hold | waiting, deferred, in_flight, uncertain | uncertain | `python -m efb_telegram_master.hold_send --apply` |
| hold_invalid_payload | waiting, deferred, in_flight | invalid_payload | `hold_invalid_payload` |
| hold_failed_history / hold_failed_supplement | waiting, deferred, in_flight | history_failed / supplement_failed | `hold_failed_history` |
| hold_failed_aggregation | waiting, deferred, in_flight | aggregation_failed | `hold_failed_aggregation` |
| defer_acquisition | waiting, deferred, in_flight | deferred | `defer_acquisition` (counts a failed attempt unless a flood wait caused it) |
| defer_failed_send | waiting, deferred | deferred | `defer_failed_send`, after the attempt was released (counts a failed attempt and, from the 3rd, stores the backoff; earlier ones leave the state as it was; a flood wait is retried without it) |
| record_completion | waiting, deferred, in_flight | sent_pending | `record_telegram_completion` |
| operator_confirm | in_flight, every held state | sent_pending | `/confirm_send` and the chat check for a found message (`confirm_queued_delivery`); refused for an aggregation row whose context takes no receipt (an unsent batch or deferred member update) |
| defer_reconciliation | sent_pending | sent_pending | `defer_reconciliation` |
| remove | waiting, deferred, in_flight, sent_pending | absent | `delete` (success, or a failure that is discarded instead of held); `clear_unsendable --apply` and the chat check's `discard_held` for a waiting container edit that has no container yet (`needs_removed_row`) |
| withdraw | waiting, deferred | absent | `cancel_logical_aggregation`, `cancel_pending_source`, and `enqueue_many` when the rows of a split replace their batch; only before any attempt started |
| discard | every held state except unrecognized hold | absent | `python -m efb_telegram_master.clear_unsendable --apply`, for a row that can never be sent (above); `discard_held`, when the chat check does not find an uncertain send it does not send again, with its container edits |
| replace_request | every held state except unrecognized hold | waiting | `python -m efb_telegram_master.clear_unsendable --apply`, for a listed send whose file is gone and whose caption has text from the sender (above) |
| resend | uncertain | waiting | `python -m efb_telegram_master.clear_unsendable --apply`, for a listed send held after a timeout or a cut connection whose files are all there (above); `resend_uncertain`, when the chat check does not find a recent uncertain send whose media is all there; only the hold is cleared |

An event the row's state does not accept is never applied. The queue raises
`IllegalDeliveryTransitionError` with a message such as `Queue row 12 is
invalid_payload; hold_uncertain is not allowed there, so the row was left
unchanged.` It is handled like any other queue persistence error, and the row
keeps its bytes. In particular a hold never replaces another hold, and an
acquisition retry never clears one. Raising the failed-attempt count of an
interrupted `in_flight` row when the queue opens changes no state and is not an
event. Three events are reported instead of stopping: releasing a row that is
not in flight logs `Send attempt not released: ...`, counting a failed send on
a row that is not waiting logs `Failed send not counted: ...`, and deferring
the reconciliation of a row that is not `sent_pending` logs `... its receipt
reconciliation was not deferred.`
`/confirm_send` answers `Only a held, unconfirmed send can be confirmed.` for a
row it cannot resolve, `This batch was never sent to Telegram as one message, so
no reply can confirm it. It stays held and no data was changed.` for an unsent
aggregation batch, and `hold_send` exits with `Hold failed: This row is
already held as <hold>; no data changed` for a row held for another reason.

## Ordering links

A row may name a predecessor in `predecessor_id`: aggregated text, a long
message's attachment and history replay use it to keep their order. From the
dependent's side a link is:

* **free**: no predecessor; the row is ready when it is waiting.
* **barrier**: the predecessor exists, is not the row itself, and is
  `sent_pending`, or is waiting, deferred or in flight with fewer than 3 failed
  attempts in a row. The dependent waits.
* **failing**: the predecessor is waiting, deferred or in flight after 3 failed
  attempts in a row, or after a restart interrupted its send. The dependent is
  ready unless it needs the predecessor's Telegram message (below).
* **void**: the predecessor is gone, is the row itself, or is held. The
  dependent is ready: waiting for it either already happened or never ends.
* **cycle**: every waiting row defers to another waiting row that has not
  failed 3 attempts in a row. The scheduler then sends the oldest row per
  destination and logs `deferred by predecessor links that never resolve`. A
  chain that ends at a failing row is not a cycle: that row is retried.

A failed attempt is a send that failed with a retryable error (a connection
that never reached Telegram, a chat migration) or a failed media acquisition.
Telegram flood waits (`RetryAfter`) are not counted, because they hold back the
whole chat, and neither are waits for a bot, a rate limit or a worker slot,
since nothing was attempted. The count resets when the row is
delivered. When a row reaches 3 failed attempts the queue logs `Queue row N for
chat C failed 3 attempts in a row; later rows no longer wait for it. It keeps
retrying and is delivered late if a retry succeeds.` From then on a failed
send stores its backoff in `reconcile_after`, as a failed acquisition always does. While that deadline is ahead the row is not its
destination's head, so later rows of the same chat are sent. Once the deadline
passes, the row is the oldest again and is retried. If it succeeds it appears in
the chat after the messages that overtook it; nothing is dropped.

An attempt interrupted by a restart stays `in_flight` and is never sent again
by itself. Opening the queue raises its count to 3 and logs `Queue row N for
chat C was interrupted by a restart while sending; counted as 3 failed
attempts, later rows no longer wait for it. It stays held for /confirm_send.`
`/confirm_send` resolves it as before.

Some dependents of a failing row need its Telegram message, not only its turn:
an edit of a container (`member_update`, including a removal or routing hint),
an append into the predecessor's container (`logical` with `inherit`), and
another output of a member the predecessor carries (`source`, `redirect`).
`needs_delivered_predecessor` in `aggregation_states.py` names them, and the
aggregation readiness check (`LiveTextAggregation.deadline`) reports them as
waiting with no deadline. The scheduler then leaves such a row out of head
election, re-elects its destination and wakes, so the rows after it in that
destination are sent while it waits. Each pass checks the rows set aside this
way (`OutboundQueue.still_waiting_for_failing`); once a row's predecessor is
delivered, confirmed, held or gone, or the row itself stopped waiting, the row
takes part in head election again and its destination is re-elected. The set
lives in memory only; after a restart the first pass finds such rows again.
`queue_report.py` (on a PostgreSQL queue it takes `--dsn`) cannot read aggregation
contexts, so its `head per destination` still shows a set-aside row as its destination's head. A row
ordered behind such a waiting row also waits, because the waiting row itself
has no failed attempts.

Links are set at enqueue. A successful removal or an aggregation handoff clears
the links that point at the row. Cancelling a batch or a pending source
re-points them at the removed row's own predecessor, splitting a batch at its
last part, and a long message's attachment at that attachment. A failed row
that is discarded leaves them void.

The rule is defined once in `efb_telegram_master/delivery_states.py`:
`waiting_sql`, `barrier_sql`, `failing_sql`, `backing_off_sql` and `ready_sql`
build the SQL. `OutboundQueue.heads`, `head_for`, `has_waiting_rows` and
`has_unresolvable_dependency` use it. `queue_report.py` carries a copy so it can
run without the package (a unit test keeps the copies of `waiting_sql`,
`barrier_sql`, `backing_off_sql` and `ready_sql` identical; `behind_barrier_sql`
exists only there, for the report): its `dispatchable` column counts ready rows,
`head per destination` elects among ready rows like the scheduler, passing over
a failing row whose retry deadline is ahead, and `--lane` shows `behind
predecessor` for a row behind a barrier and `sendable` for a ready one. A
deferred head is ready but waits for `retry_in_s`.

## Outbound worker

One worker thread delivers the queue. Each pass harvests finished calls,
reconciles receipts, retries blocked media edits and then offers each
destination's head a worker slot. A pass ends in one of the ways below, and the
way it ends decides how long the worker sleeps before the next pass. Enqueues and
completions always wake it early. `OutboundQueueScheduler.dispatch_once` sets
`next_deadline` and leaves calls in flight accordingly; when several apply, the
first one in this table wins.

| Pass ends as | When | Worker then waits | Log |
|---|---|---|---|
| stopped | The scheduler is stopping or hit a queue persistence error. | Does not wait: the worker exits and durable rows stay queued. | `Outbound recovery stopped; durable rows retained: ...` after a persistence error |
| stalled | Rows are still waiting, none has a retry deadline, nothing is in flight. | 30 seconds (`STALL_RESCAN_SECONDS`), then rescans. | `Outbound scheduler left N dispatchable head(s) undispatched ... while queued rows remain`, at most once a minute |
| cycle | No head was ready and every waiting row defers to another waiting row that is not failing; the oldest row per destination was sent anyway. | For that call to complete, or for a retry deadline. The next pass re-elects every head. | `deferred by predecessor links that never resolve` |
| permit_leak | Every worker slot was taken while nothing was in flight, so no completion could free one; the slots were reset. | For the calls this pass started, or for a retry deadline. | `Outbound worker permits were held without a submitted call; resetting them. Queued rows are retained.` |
| busy | A call is in flight. | For a completion, or for a retry deadline. | None. |
| deferred | Nothing is in flight; a row, receipt or blocked media edit has a retry deadline. | Until the earliest deadline. | None. |
| idle | Nothing is waiting and nothing is in flight. | For the next enqueue, with no timeout. | None. |

`N` in the stall line counts the heads the pass looked at. It is 0 when every
waiting row sits behind an ordering link, so `left 0 dispatchable head(s)`
means rows are waiting for a predecessor (see [Ordering links](#ordering-links)),
not that the queue is empty. A stall, a cycle and a slot reset all mean
delivery is still being attempted; no row was dropped.

### Worker slots

`send_worker_count` sets how many calls may run at once. `WorkerSlots` in
`outbound.py` holds them; each slot is:

* **free**: available to the next head;
* **reserved**: `WorkerSlots.reserve` handed it to the row a pass is preparing
  (sender choice, rate limits, payload read, submission);
* **owned**: `ReservedSlot.own(row_id)` gave it to that row once its call was
  submitted. `WorkerSlots.release(row_id)` frees it when the call leaves
  `in_flight` (completion, shutdown).

A reservation is held in a `with` block, so a row that is deferred, held,
discarded or fails to submit frees its slot on every path, including when
choosing a sender or taking its rate limit raises. Such a row stays queued and
is retried 30 seconds later; the worker carries on with the next head and logs
`Choosing a sender for queue row N failed; the row stays queued and is retried
in 30 seconds.` with the traceback. Releasing a slot no row owns, or owning one
without a reservation, is never applied: it logs `Outbound worker slot release
for queue row N has no matching reservation; ignored.` The permit_leak reset
above is kept as a backstop for slots that nothing can free.

### Head cache

The scheduler keeps the last elected head of every destination instead of
reading every waiting row on each pass. The cache is:

* **fresh**: a full scan (`OutboundQueue.heads`) just elected every head;
* **incremental**: only the destinations a queue change marked dirty
  (`mark_destination_dirty`, `take_dirty_destinations`) are re-elected with
  `head_for`;
* **heal required**: the next pass does a full scan. That happens at start,
  when the cache is empty, after a cycle was broken, after a row was
  quarantined for its replay size, and at least every 30 seconds
  (`HEADS_FULL_RESCAN_SECONDS`).

A failing row whose retry deadline is ahead is not elected, so no head carries
that deadline. Each pass schedules a wake-up for it and, once it is due, marks
its destination dirty unless it is already that destination's head.

The periodic full scan bounds how long a queue change that was not marked dirty
can go unseen. The cache lives in memory only; a restart starts fresh.
