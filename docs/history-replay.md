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
neither the complete history nor its media is buffered in memory.

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
`sender_bot_id = NULL` retains the existing MsgLog convention of main bot;
unrecorded past main-bot changes cannot be reconstructed from that field.

A timeout or lost copy/send response is not a negative acknowledgment. It does
not trigger fallback/re-upload; the existing uncertain-delivery hold still
prevents automatic duplicates.

## Persistent state and compatibility

New history queue metadata records `source_sender_bot_id` for media acquisition,
not `required_sender_bot_id`. Text needs no original-bot metadata or source-log
lookup after its formatted history entry has been prepared.

Older queued replays may still have an original bot in `required_sender_bot_id`.
It is used only as legacy media-acquisition metadata, not a send constraint.
At queue startup, only `history_failed:RequiredSenderUnavailableError` records
that have never begun a delivery attempt are released from the obsolete hold.
Attempted, uncertain and other failed records remain untouched. Original MsgLog
records are neither deleted nor rewritten.

Once an outbound enqueue commits, it owns the batch; only then are its staging
history entries removed. Rejected historical sends stay in the durable queue
rather than being silently discarded. Held rows do not block later sends.
Preparation failures retain staging entries. This is not a cross-database
exactly-once handoff: an abrupt exit between outbound commit and staging deletion
remains a recovery boundary. Explicitly requesting a new full backfill can replay
already completed history again.

## Tests

`test_history_replay.py` covers bounded cross-page text batching, including
mixed original senders, media ordering, alternate IDs, failure retention and
uncertain responses.

`test_history_sender.py` covers all supported saved-media types, owner-aware
acquisition with independent main/auxiliary sending, unavailable acquisition
bots, local/streamed files and legacy queue restart behavior. It deliberately
does not assert that replay must be sent by the original bot.

The live Telegram tests perform `/start <token> true`, verify merged texts and
actual recovered photo/video content, and retain the original MsgLog records.
The multi-bot case deletes media originally sent by an auxiliary bot, observes
that only that bot receives `get_file`, and verifies the normal main sender sends
the recovered media and merged texts, including texts originally sent by other
bots. Duplicate-response-loss tests remain enabled.
