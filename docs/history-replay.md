# Relink history replay

Link controls are unchanged: auto backfills a first link and sends a history
link on relink; explicit `true` requests full backfill, and `false` disables it.
Upgrading does not automatically replay previously completed history.

## Text batching and sending

Concatenate consecutive formatted texts up to the existing `4096 - 20`
character boundary, flushing before media and at the end. Original bot identity
is irrelevant: texts from different original bots may share one batch. Author
and timestamp formatting is unchanged. A single oversized text uses the existing
full-text attachment path.

Read history in keyset pages of 32. Page boundaries do not split batches, and
formatted history is spooled to a temporary disk file from a finite read snapshot.
Formatting holds no main-database write lock. Staging inserts commit in batches
of 32, then one target pointer publishes the complete generation. Before that
publication, readers continue to see the previous generation. Obsolete staging
rows are deleted in bounded batches; startup reclaims interrupted, unpublished
generations after the schema transaction. History formatting does not load media
bytes. Oversized legacy queue media is streamed into sidecars before dispatch,
with filenames preserved and non-media data bounded by the queue budget. This
encoded-data budget does not bound total process memory.

All replay sends use the existing outbound sender selection, affinity and rate
limits. There is no separate historical sender policy. For example, the original
bot may be auxiliary A, while the ordinary sender chosen for replay is main B.

## Media acquisition

Try copying the original Telegram message with the ordinarily selected sender.
If MsgLog has an alternate message ID, copy that newer ID. Successful copying
requires no file-ID lookup or download.

After an explicit source-copy rejection, recover media using the stored file ID.
Only `get_file` is routed to the bot that owns that ID; it must not fall back to
another bot. Membership in the destination chat is not required for acquisition.
The resulting local file or downloaded byte stream is uploaded by the already
selected ordinary sender. Do not pass bot A's file ID (or token-bearing download
URL) as bot B's upload argument.

This applies to photo, video, animation, document, audio, voice and sticker
recovery. Captions, media types, target topics and silent delivery are preserved.
Local Bot API paths are reused without downloading or buffering a large file;
remote downloads are streamed to a temporary file, and uploads use
`InputFile(read_file_handle=False)`. Temporary handles close on success or error.
No content conversion or new sender-selection algorithm is introduced.

The same owner-aware `get_file` entry point is used for ordinary ETMMsg media
acquisition. An auxiliary bot's failed file lookup is not retried with main.
`sender_bot_id` identifies the actual author for edit/delete routing. A separate
`file_bot_id` in MsgLog metadata identifies the file-ID issuer when needed. For
example, a reply observed by main can contain main-issued file IDs for a message
authored by an auxiliary bot. Acquisition uses the issuer; sending and batching
remain independent of both identities. Legacy rows without an issuer fall back
to the recorded author, with `sender_bot_id = NULL` meaning main. Historically
misrecorded owners and unrecorded past main-bot changes are not retroactively
repaired.

A timeout or lost copy/send response is not a negative acknowledgment. It does
not trigger fallback/re-upload; the existing uncertain-delivery hold still
prevents automatic duplicates. Transient `get_file` or download failures after
an explicit copy rejection retry acquisition with persisted capped backoff,
respecting RetryAfter. They do not represent an uncertain upload because the
fallback send has not begun. Permanent acquisition rejections or an unavailable
owner retain failed history work for repair. A lost fallback send response still
requires delivery confirmation.

## Persistent state and compatibility

New history queue metadata records the file-ID issuer in `source_sender_bot_id`
for media acquisition, not in `required_sender_bot_id`. Despite its name, this
field need not identify the original author. Text needs no original-bot metadata
or source-log lookup after its formatted history entry has been prepared.

Older queued replays may still have an original bot in `required_sender_bot_id`.
It is used only as legacy media-acquisition metadata, not a send constraint.
At queue startup, only `history_failed:RequiredSenderUnavailableError` records
that have never begun a delivery attempt are released from the obsolete hold.
Attempted, uncertain and other failed records remain untouched. Original MsgLog
records are neither deleted nor rewritten.

The outbound enqueue and generation-qualified ownership keys commit in the same
queue transaction. Only then are staging entries deleted, followed by their
ownership keys. Keys survive queue-row completion until staging cleanup finishes.
On restart, entries already owned by the queue are removed without being enqueued
again, including after an exit between enqueue and staging deletion. This makes
the ownership handoff idempotent across the two stores.

Rejected historical sends stay in the durable queue. Held rows do not block later
sends, and preparation failures retain staging entries. This does not guarantee
exactly-once Telegram delivery: uncertain remote responses require confirmation.
Explicitly requesting a new full backfill can replay completed history again.

The current receipt decoder reads both older author-only receipts and receipts
with a separate file-ID issuer. Older code cannot read the latter, so this
one-way format compatibility does not support rollback.

## Tests

`test_history_replay.py` covers bounded cross-page text batching, including
mixed original senders, media ordering, alternate IDs, failure retention and
uncertain responses.

`test_history_sender.py` covers all supported saved-media types, owner-aware
acquisition with independent main/auxiliary sending, unavailable acquisition
bots, local/streamed files and legacy queue restart behavior. It deliberately
does not assert that replay must be sent by the original bot.

`test_history_runtime_recovery.py` covers finite snapshots with concurrent live
writes, atomic generation publication, indexed seeks, and interrupted ownership
handoff and staging cleanup.

The credential-dependent live Telegram tests are designed to perform
`/start <token> true`, verify merged texts and actual recovered photo/video
content, and retain the original MsgLog records.
The multi-bot case deletes media originally sent by an auxiliary bot, observes
that only that bot receives `get_file`, and verifies the normal main sender sends
the recovered media and merged texts, including texts originally sent by other
bots. Duplicate-response-loss tests remain enabled.
