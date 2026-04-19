# ETM Migration Plan: PTB 13.15 to PTB 22.7 with a Hybrid Async Send Runtime

## Summary

- Target `python-telegram-bot~=22.7` and raise `python_requires` to `>=3.10`.
- Keep the EFB boundary synchronous:
  - `Channel.send_message()` stays sync
  - `Channel.send_status()` stays sync
  - `Channel.poll()` stays blocking
- Rebuild the send path first, because it is the core architectural change and will shape the rest of the migration.
- Use one async runtime owned by ETM, with synchronous wrappers for the rest of the codebase.
- Adopt a **hybrid send model by call site**:
  - slave-originated outbound content may use eventual completion semantics
  - control/UI flows that need a real Telegram `message_id` immediately stay blocking

## Hard Constraints

- EFB framework and docs confirm ETM cannot push async into the framework surface.
- PTB `22.7` is async-only and requires Python `3.10+`.
- PTB v20+ makes Telegram objects effectively immutable for ETM’s purposes, so `_sender_bot_id` must no longer be injected into PTB message objects.
- Existing ETM code has immediate-ID dependencies:
  - [commands.py](/abs/path/efb-telegram-master/efb_telegram_master/commands.py:72) stores command state by `(chat.id, message_id)`
  - [chat_binding.py](/abs/path/efb-telegram-master/efb_telegram_master/chat_binding.py:399) and [chat_binding.py](/abs/path/efb-telegram-master/efb_telegram_master/chat_binding.py:790) create conversation state keyed by the returned Telegram `message_id`
  - [slave_message.py](/abs/path/efb-telegram-master/efb_telegram_master/slave_message.py:120) resolves edits by looking up the DB mapping; if missing, it sends a new message instead
- Because of that, **universal placeholders are not in scope for this migration**.

## Architecture Decisions

- `TelegramBotManager.polling()` will create and run the PTB `Application` inside the blocking EFB master poll thread.
- All Telegram Bot API I/O will run inside one asyncio runtime managed by ETM.
- Outbound sending is centralized in a new internal runtime, not in the current decorator stack.
- Sender identity becomes explicit runtime metadata and durable DB state, not PTB object mutation.
- Queue durability is in-process only: once ETM accepts a send request, it must not be lost because of rate limits or temporary sender unavailability; restart recovery is out of scope.
- Small cross-sender reordering is acceptable.

## Send Runtime Design

- Introduce `AsyncTelegramRuntime` in [bot_manager.py](/abs/path/efb-telegram-master/efb_telegram_master/bot_manager.py).
- Introduce internal types:
  - `SenderState`: bot handle, identity, disabled state, membership state, per-sender sliding window, retry-after state
  - `OutboundRequest`: method, args/kwargs, chat_id, thread_id, forced sender ID, callback-keyboard flag, cleanup files, send mode
  - `SendReceipt`: `message`, `sender_bot_id`, `queued`, `task_id`
  - `QueuedSendPlaceholder`: local placeholder for eventual sends
- Each sender has its own sliding window:
  - main bot
  - each auxiliary bot
- Scheduling policy:
  - compute eligible senders
  - compute earliest legal send time per sender
  - if any sender can send now, use the earliest-ready one
  - otherwise enqueue
  - queued tasks are not pre-bound to a sender; sender choice is recomputed at dispatch time
- Messages with callback keyboards remain main-bot-only.
- `send_chat_action` stays best-effort and non-reliable.

## Hybrid Send Modes

- **Blocking mode**:
  - used for control/UI flows and any flow that needs the real Telegram `message_id` immediately
  - examples: command registration, chat-link/chat-head conversations, flows that immediately edit or reply to the sent message
- **Eventual mode**:
  - used for slave-originated outbound content sent to the user
  - returns immediately with a placeholder/handle even if a sender is currently available
  - real Telegram result and DB mapping are completed asynchronously
- This hybrid model gives most of the latency benefit without forcing a full temp-ID-to-real-ID rebinding system across the whole codebase.

## Persistence and Routing

- Keep `MsgLog.sender_bot_id` and `ETMMsg.sender_bot_id` as the durable routing source.
- Remove `_sender_bot_id` injection into PTB messages entirely.
- Immediate sends log with the runtime-provided `sender_bot_id`.
- Eventual sends register pending DB completion by `task_id`; on completion ETM writes:
  - real Telegram message ID
  - file/media metadata
  - final `sender_bot_id`
- `edit_*`, `delete_message`, and `get_file` route by persisted `sender_bot_id`, not by runtime guesswork.

## PTB 22.7 Migration Work

- In [setup.py](/abs/path/efb-telegram-master/setup.py):
  - bump to `python-telegram-bot~=22.7`
  - bump Python to `>=3.10`
  - remove `urllib3<2`, `standard-imghdr`, `audioop-lts`
- Replace:
  - `Updater` / `Dispatcher` with `Application` / `ApplicationBuilder`
  - `Filters` with `telegram.ext.filters`
  - old `Request` with `telegram.request.HTTPXRequest`
  - old proxy config semantics with PTB 22 proxy/request config
  - old custom `Handler` with `BaseHandler` in [locale_handler.py](/abs/path/efb-telegram-master/efb_telegram_master/locale_handler.py)
- Convert all Telegram handlers to `async def`.
- Migrate constants, exceptions, file download APIs, and request configuration to PTB 22 semantics.

## Subsystem Plan

- `bot_manager.py`: build runtime, send scheduler, sync wrappers, PTB Application lifecycle, shutdown.
- `auxiliary_bot.py` and `bot_pool.py`: keep identity, membership, and admin notification logic; remove old synchronous slot-allocation role.
- `slave_message.py`: consume `SendReceipt`, support eventual completion for slave-originated sends, preserve edit/delete routing.
- `commands.py` and `chat_binding.py`: stay on blocking send mode where immediate message IDs are required.
- `__init__.py`, `master_message.py`, `wizard.py`, `chat_binding.py`, `commands.py`: migrate handlers, filters, and startup model to PTB 22.
- `message.py` and `db.py`: keep sender persistence model, update file retrieval and completion plumbing.

## Test Plan

- Unit tests:
  - per-sender sliding window behavior
  - sender selection between main and aux bots
  - queue insertion and re-dispatch without message loss
  - accepted minor cross-sender reordering
  - callback-keyboard main-bot restriction
  - explicit sender persistence without PTB message mutation
  - forced routing for edit/delete/get_file
  - blocking vs eventual send-mode behavior
- Integration tests:
  - PTB `Application` startup/shutdown
  - `/start`, `/help`, `/info`, `/react`
  - command callbacks
  - chat linking and chat-head flows
  - auxiliary bot routing
  - delayed/eventual completion and DB repair
  - webhook and polling modes

## Explicitly Out Of Scope

- Universal placeholders for all sends.
- Restart-persistent outbound queue recovery.
- A global temp-message-ID rebinding layer for all Telegram interactions.
- A full “pending edit/delete merge engine” for every outbound path.
- If universal placeholders are desired later, that should be a second project after PTB 22 migration and hybrid send runtime stabilization.

## References

- Local repo:
  - [setup.py](/abs/path/efb-telegram-master/setup.py)
  - [bot_manager.py](/abs/path/efb-telegram-master/efb_telegram_master/bot_manager.py)
  - [auxiliary_bot.py](/abs/path/efb-telegram-master/efb_telegram_master/auxiliary_bot.py)
  - [bot_pool.py](/abs/path/efb-telegram-master/efb_telegram_master/bot_pool.py)
  - [slave_message.py](/abs/path/efb-telegram-master/efb_telegram_master/slave_message.py)
  - [commands.py](/abs/path/efb-telegram-master/efb_telegram_master/commands.py)
  - [chat_binding.py](/abs/path/efb-telegram-master/efb_telegram_master/chat_binding.py)
  - [db.py](/abs/path/efb-telegram-master/efb_telegram_master/db.py)
  - [message.py](/abs/path/efb-telegram-master/efb_telegram_master/message.py)
  - [__init__.py](/abs/path/efb-telegram-master/efb_telegram_master/__init__.py)
  - [locale_handler.py](/abs/path/efb-telegram-master/efb_telegram_master/locale_handler.py)
- EFB docs and source:
  - https://ehforwarderbot.readthedocs.io/
  - https://ehforwarderbot.readthedocs.io/en/latest/guide/index.html
  - https://ehforwarderbot.readthedocs.io/en/latest/guide/master.html
  - https://ehforwarderbot.readthedocs.io/en/latest/guide/misc.html
  - https://ehforwarderbot.readthedocs.io/en/latest/API/message.html
  - https://ehforwarderbot.readthedocs.io/en/latest/_modules/ehforwarderbot/channel.html
  - locally inspected: `ehforwarderbot.channel`, `ehforwarderbot.coordinator`, `ehforwarderbot.__main__`, `ehforwarderbot.message`
- PTB docs:
  - https://pypi.org/project/python-telegram-bot/
  - https://docs.python-telegram-bot.org/en/stable/changelog.html
  - https://docs.python-telegram-bot.org/en/stable/telegram.ext.filters.html
  - https://docs.python-telegram-bot.org/en/latest/telegram.request.httpxrequest.html
"