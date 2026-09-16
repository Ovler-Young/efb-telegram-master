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

```sql
SELECT id, operation, telegram_chat_id, delivery_state, delivery_hold,
       attempt_sender_bot_id, attempt_started_at,
       completion_receipt IS NOT NULL AS has_receipt
FROM outbound_queue
WHERE delivery_hold IS NOT NULL;
```

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
