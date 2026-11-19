# Recovering an unconfirmed send without sending it again

A timeout or disconnected HTTP response is not proof that Telegram rejected a
message. The remote side may have accepted it. Message-creating queue operations
now persist `delivery_hold='in_flight'` before executor submission. The hold is
cleared only for a definite rejection/pre-send connection failure, or by saving
an actual completion receipt. An interrupted attempt stays held across restarts.
An ambiguous failure is stored as `uncertain:<exception-class>/<cause-class>`.
No request objects, tokens or exception tracebacks are persisted in this field.

Held rows keep their payload, sidecar and original MsgLog context. They are not
silently marked sent and are not deleted. Other runnable rows, including newer
messages to the same chat, continue. Definite RetryAfter rejections still retry
at the requested deadline. ConnectError/ConnectTimeout/PoolTimeout causes are
also retryable because the HTTP request was not sent. Read/write failures and
unknown network outcomes are not blindly retried for message-creating calls.

This is not an exactly-once transport guarantee: after loss of the response,
remote success requires evidence. No increasing retry count, arbitrary file-size
limit, or longer timeout can supply that missing evidence.

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
investigate. There is no automatic "assume failure and resend" timer.

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
results. A healthy worker can coexist with held records; queue depth alone does
not mean that those records are awaiting a safe automatic retry.

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
that rows were dropped.

A dependent row is deferred only while the row it links to is itself still
waiting: queued and unheld, in flight, or delivered and awaiting its MsgLog write.
A predecessor that is gone, permanently held, or linked into a cycle is not an
ordering barrier — waiting for it either already happened or can never happen —
so it cannot strand everything queued behind it. Held rows themselves are kept
and still require an operator. When every waiting row defers to another waiting
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
SELECT id, operation, telegram_chat_id, delivery_state, delivery_hold,
       attempt_sender_bot_id, attempt_started_at,
       completion_receipt IS NOT NULL AS has_receipt
FROM outbound_queue
WHERE delivery_hold IS NOT NULL;
```

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

Every row of `outbound-queue.sqlite3` is in exactly one state, read from its
`delivery_state` and `delivery_hold` columns (and `reconcile_after` for a
deferred row). The definition lives in `efb_telegram_master/delivery_states.py`:
`RowState` (states), `HoldKind` (stored hold spellings), `Event`, `TRANSITIONS`
(the allowed-transition table), `classify`, `parse_hold` and `format_hold`.
Stored spellings never change between versions.

| State | `delivery_state` / `delivery_hold` | Meaning | Operator action |
|---|---|---|---|
| waiting | `queued` / NULL | Will be sent when its destination's turn comes. | None. |
| deferred | `queued` / NULL, `reconcile_after` ahead | Media acquisition failed before any send; retried after a capped backoff. | None. |
| in_flight | `queued` / `in_flight` | Attempt recorded before submission. After a restart it is an interrupted attempt that may have been delivered. | If delivered, `/confirm_send`; otherwise investigate. |
| uncertain | `queued` / `uncertain:<Class>[/<Cause>]` | Ambiguous result: Telegram may have accepted it. | `/confirm_send` on the delivered message. |
| uncertain (operator) | `queued` / `uncertain:operator_observed_delivery` | Held offline by `hold_send` for a send already seen on Telegram. | `/confirm_send`; any configured bot is accepted. |
| invalid_payload | `queued` / `invalid_payload` | The stored request cannot be decoded. Not evidence of delivery. | Investigate the row. |
| history_failed | `queued` / `history_failed:<Class>` | History replay was rejected. Startup re-queues `RequiredSenderUnavailableError` rows that never started an attempt. | Investigate, or `/confirm_send` if it was delivered. |
| supplement_failed | `queued` / `supplement_failed:<Class>` | The full-content attachment of a long message was rejected; the primary message is unaffected. | Investigate the row. |
| aggregation_failed | `queued` / `aggregation_failed:<Class>` | An aggregated text batch, or a row others are ordered behind, failed for good. | Investigate the row. |
| unrecognized hold | `queued` / any other text | A hold written by another version. Kept and never sent. | Use the version that wrote it, or `/confirm_send`. |
| sent_pending | `sent_pending` / NULL | Telegram receipt saved; the MsgLog write is retried with backoff. | None. |
| absent | no row | Not yet enqueued, or removed. | None. |

| Event | From | To | Code |
|---|---|---|---|
| enqueue | absent | waiting | `OutboundQueue.enqueue_many`, aggregation handoff, supplement insert (a new row has no earlier state to check) |
| begin_attempt | waiting, deferred | in_flight | `begin_delivery_attempt` (an aggregation row that `materialize` already moved to in_flight is left unchanged) |
| materialize | waiting, deferred | in_flight | `materialize_aggregation` |
| release_attempt | in_flight | waiting | `release_delivery_attempt` (submission failed or Telegram definitely rejected; an aggregate rewinds to its logical batch) |
| hold_uncertain | waiting, deferred, in_flight | uncertain | `hold_uncertain_delivery` |
| operator_hold | waiting, deferred, in_flight, uncertain | uncertain | `python -m efb_telegram_master.hold_send --apply` |
| hold_invalid_payload | waiting, deferred, in_flight | invalid_payload | `hold_invalid_payload` |
| hold_failed_history / hold_failed_supplement | waiting, deferred, in_flight | history_failed / supplement_failed | `hold_failed_history` |
| hold_failed_aggregation | waiting, deferred, in_flight | aggregation_failed | `hold_failed_aggregation` |
| defer_acquisition | waiting, deferred, in_flight | deferred | `defer_acquisition` |
| record_completion | waiting, deferred, in_flight | sent_pending | `record_telegram_completion` |
| operator_confirm | in_flight, every held state | sent_pending | `/confirm_send` (`confirm_queued_delivery`) |
| defer_reconciliation | sent_pending | sent_pending | `defer_reconciliation` |
| remove | waiting, deferred, in_flight, sent_pending | absent | `delete` (success, or a failure that is discarded instead of held) |
| withdraw | waiting, deferred | absent | `cancel_logical_aggregation`, `cancel_pending_source`, and `enqueue_many` when the rows of a split replace their batch; only before any attempt started |
| repair_history_sender | history_failed | waiting | queue open, schema migration; only `history_failed:RequiredSenderUnavailableError` on a history row that never started an attempt |

An event the row's state does not accept is never applied. The queue raises
`IllegalDeliveryTransitionError` with a message such as `Queue row 12 is
invalid_payload; hold_uncertain is not allowed there, so the row was left
unchanged.` It is handled like any other queue persistence error, and the row
keeps its bytes. In particular a hold never replaces another hold, and an
acquisition retry never clears one. Two events are reported instead of
stopping: releasing a row that is not in flight logs `Send attempt not
released: ...`, and deferring the reconciliation of a row that is not
`sent_pending` logs `... its receipt reconciliation was not deferred.`
`/confirm_send` answers `Only a held, unconfirmed send can be confirmed.` for a
row it cannot resolve, and `hold_send` exits with `Hold failed: This row is
already held as <hold>; no data changed` for a row held for another reason.

## Ordering links

A row may name a predecessor in `predecessor_id`: aggregated text, a long
message's attachment and history replay use it to keep their order. From the
dependent's side a link is:

* **free**: no predecessor; the row is ready when it is waiting.
* **barrier**: the predecessor exists, is not the row itself, and is waiting,
  deferred, in flight or `sent_pending`. The dependent waits.
* **void**: the predecessor is gone, is the row itself, or is held. The
  dependent is ready: waiting for it either already happened or never ends.
* **cycle**: every waiting row defers to another waiting row. The scheduler
  then sends the oldest row per destination and logs `deferred by predecessor
  links that never resolve`.

An attempt interrupted by a restart stays `in_flight`, so rows linked behind it
keep waiting until `/confirm_send` resolves it.

Links are set at enqueue. A successful removal or an aggregation handoff clears
the links that point at the row. Cancelling a batch or a pending source
re-points them at the removed row's own predecessor, splitting a batch at its
last part, and a long message's attachment at that attachment. A failed row
that is discarded leaves them void.

The rule is defined once in `efb_telegram_master/delivery_states.py`:
`waiting_sql`, `barrier_sql`, `behind_barrier_sql` and `ready_sql` build the SQL,
and `is_waiting`, `is_barrier` and `is_ready` mirror it in Python.
`OutboundQueue.heads`, `head_for`, `has_waiting_rows` and
`has_unresolvable_dependency` use it. `queue_report.py` carries a copy so it can
run without the package (a unit test keeps the copy identical): its
`dispatchable` column counts ready rows, `head per destination` elects among
ready rows like the scheduler, and `--lane` shows `behind predecessor` for a
row behind a barrier and `sendable` for a ready one. A deferred head is ready
but waits for `retry_in_s`.
