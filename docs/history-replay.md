# Relink history replay

The existing link controls are unchanged: automatic mode backfills a first link
and sends a history link on relink; an explicit `true` requests full backfill,
and `false` disables it. No new automatic replay is triggered by an upgrade.

## Text batching

Replay restores the earlier consecutive-text algorithm: concatenate formatted
text entries until the next entry would exceed `4096 - 20` characters. Flush
before a media entry, when the original sender bot changes, and at the end.
Only consecutive texts with the same original bot may be combined. Author and
timestamp formatting stays the same. A single oversized text still uses the
existing full-text attachment path, with the same original sender.

Read historical entries in keyset pages of 32. A page boundary does not flush a
text batch. Only a page plus the current text batch is retained in memory; the
14-million-row MsgLog table is not materialized or rewritten.

For `text A, text B, video, text C, text D`, the resulting calls are one combined
text for A+B, one media replay, then one combined text for C+D, provided each
text pair has the same original sender. For `bot A: text 1, bot B: text 2,
bot A: text 3`, send three separate batches in that order; never regroup by bot
across intervening messages. This is not a media-album regrouping algorithm.

## Media replay

Copy the source Telegram message first. If MsgLog has a newer alternate message
ID, copy that ID. The recorded sender is mandatory for both source copying and
saved-file-ID recovery, not merely a pool preference. Never switch an unavailable
auxiliary sender to the main bot, even when a main-bot copy could succeed.

After an explicit source-copy rejection (message missing, not copyable, or chat
not found), reuse the saved file ID with the same owning bot and original media
type. This supports video, photo, animation, document, audio, voice and sticker
records. It does not download, convert, or re-upload the stored media bytes.
Target topic, silent delivery, and the stored caption are retained. The fallback
uses the usual long-caption handling when needed.

A timeout or lost response is not proof of rejection. It never triggers the
copy-to-file-ID fallback, and the existing persistent delivery uncertainty
handling still prevents automatic duplicate sends. If the fallback itself has
an ambiguous response, the same outbound row is held for confirmation.

## Ownership and failures

A committed outbound request carries the source chat and all history-entry IDs
for its batch. Only then are those staging entries removed. Original MsgLog
records remain unchanged and are never deleted by history replay.

Every prepared replay, including text, reads its original sender from MsgLog and
persists that requirement with the outbound row. Cooldown, retries, restarts,
changed pool affinity and media fallback do not change it. An explicitly recorded
numeric main-bot ID is supported too. The existing NULL `sender_bot_id` convention
means the main bot; missing MsgLog records are not equivalent to NULL. Old rows
that never recorded an identity cannot identify a previously replaced main bot.

Preparation errors (including a failed or missing MsgLog lookup) retain the staging
entries and stop that replay pass instead of guessing a sender. Previously queued,
tagged history rows with no required sender are retained as `history_failed:*`, not
automatically assigned to a bot. Old merged batches without per-source identity
cannot be safely relabeled in place. This upgrade does not rewrite the queue or
trigger a new full replay.

Rejected history deliveries remain in `outbound_queue` with a
`history_failed:<exception class>` hold rather than being silently discarded.
The failed Future logs the entry IDs and error. Held rows do not block later
replay/live sends, and are not automatically retried after restart. Uncertain
sends retain their separate `uncertain:*` handling and confirmation procedure.

This is not a cross-database exactly-once handoff: an abrupt exit between the
outbound commit and deletion of staging entries remains a separate recovery
boundary. Re-requesting full backfill is an explicit new replay and can copy
previously replayed messages again; do not use repeated relinks to probe a send
whose delivery result is still uncertain.

## Validation

`tests/unit/test_history_replay.py` exercises real SQLite history staging,
text/media ordering, cross-page batching, sender-owned file IDs, alternate IDs,
rejected replay retention, and ambiguous-response duplicate prevention.

`tests/unit/test_history_sender.py` covers all supported media types, strict
unavailable-owner handling, mixed-bot batch boundaries, restart/retry identity,
and the pre-RPC sender check. `tests/integration/test_relink_original_sender.py`
uses real main/auxiliary bots and the user session to check actual sender IDs for
text and deleted-source photo/video recovery, while current affinity favors a
different sender. It also verifies that source MsgLog records remain unchanged.

`tests/integration/test_relink_history_media.py` uses the connected CI Telegram
bot and user session. It sends two texts, a real small MP4, and two more texts,
then issues `/start <token> true`. It checks the three resulting messages and
original MsgLog records. A second case deletes only the test's original video
from Telegram before relink, exercising actual saved-file-ID recovery.
