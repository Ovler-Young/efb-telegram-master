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
that rows were dropped. [Outbound worker](#outbound-worker) lists every way a
pass can end.

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

Every state machine in the queue and in text aggregation is listed in
[State machines](state-machines.md).

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
| aggregation_failed | `queued` / `aggregation_failed:<Class>` | An aggregated text batch, or a row others are ordered behind, failed for good. A batch that was never sent as one Telegram message cannot be confirmed with `/confirm_send`. | Investigate the row. |
| unrecognized hold | `queued` / any other text | A hold written by another version. Kept and never sent. | Use the version that wrote it, or `/confirm_send`. |
| sent_pending | `sent_pending` / NULL | Telegram receipt saved; the MsgLog write is retried with backoff. | None. |
| absent | no row | Not yet enqueued, or removed. | None. |

Only `/confirm_send` ends a hold (and startup, for the `history_failed` rows
named above). The bot never removes a held row or its media by itself: a row
that was not delivered stays in the queue until an operator removes it.

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
| operator_confirm | in_flight, every held state | sent_pending | `/confirm_send` (`confirm_queued_delivery`); refused for an aggregation row whose context takes no receipt (an unsent batch or deferred member update) |
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
row it cannot resolve, `This batch was never sent to Telegram as one message, so
no reply can confirm it. It stays held and no data was changed.` for an unsent
aggregation batch, and `hold_send` exits with `Hold failed: This row is
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

## Outbound worker

One worker thread delivers the queue. Each pass harvests finished calls,
reconciles receipts, retries blocked media edits and then offers each
destination's head a worker slot. The pass ends in exactly one outcome, and the
outcome decides how long the worker sleeps before the next pass. Enqueues and
completions always wake it early. The definition is `PassOutcome` in
`efb_telegram_master/outbound.py`, returned by
`OutboundQueueScheduler.dispatch_once`; when several apply, the first one in
this table wins.

| Outcome | When | Worker then waits | Log |
|---|---|---|---|
| stopped | The scheduler is stopping or hit a queue persistence error. | Does not wait: the worker exits and durable rows stay queued. | `Outbound recovery stopped; durable rows retained: ...` after a persistence error |
| stalled | Rows are still waiting, none has a retry deadline, nothing is in flight. | 30 seconds (`STALL_RESCAN_SECONDS`), then rescans. | `Outbound scheduler left N dispatchable head(s) undispatched ... while queued rows remain`, at most once a minute |
| cycle | No head was ready and every waiting row defers to another waiting row; the oldest row per destination was sent anyway. | For that call to complete, or for a retry deadline. The next pass re-elects every head. | `deferred by predecessor links that never resolve` |
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

The periodic full scan bounds how long a queue change that was not marked dirty
can go unseen. The cache lives in memory only; a restart starts fresh.
