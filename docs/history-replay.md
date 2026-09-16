# Relink history replay

The existing link controls are unchanged: automatic mode backfills a first link
and sends a history link on relink; an explicit `true` requests full backfill,
and `false` disables it. No new automatic replay is triggered by an upgrade.

## Text batching

Replay restores the earlier consecutive-text algorithm: concatenate formatted
text entries until the next entry would exceed `4096 - 20` characters. Flush
before a media entry and at the end. Author and timestamp formatting stays the
same. A single oversized text still uses the existing full-text attachment path.

Read historical entries in keyset pages of 32. A page boundary does not flush a
text batch. Only a page plus the current text batch is retained in memory; the
14-million-row MsgLog table is not materialized or rewritten.

For `text A, text B, video, text C, text D`, the resulting calls are one combined
text for A+B, one media replay, then one combined text for C+D. This is not a
media-album regrouping algorithm.

## Media replay

Copy the source Telegram message first. If MsgLog has a newer alternate message
ID, copy that ID. Prefer its recorded sender bot, because saved Telegram file
IDs are bot-specific. A removed/non-member auxiliary sender does not prevent a
main-bot copy of a still-accessible original, but its file ID must not be reused
by the main bot.

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

`tests/integration/test_relink_history_media.py` uses the connected CI Telegram
bot and user session. It sends two texts, a real small MP4, and two more texts,
then issues `/start <token> true`. It checks the three resulting messages and
original MsgLog records. A second case deletes only the test's original video
from Telegram before relink, exercising actual saved-file-ID recovery.
