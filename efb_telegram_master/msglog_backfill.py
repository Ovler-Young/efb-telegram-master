"""Run-once repair of large gaps in Telegram MsgLog history."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import os
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Protocol, cast

from ehforwarderbot import coordinator
from ehforwarderbot.types import ChatID, ModuleID
from peewee import chunked
from ruamel.yaml import YAML

from .db import (
    DatabaseManager,
    MsgLog,
    MsgLogBackfillCheckpoint,
    SYNTHETIC_MSGLOG_PREFIX,
    TopicAssoc,
    database,
)
from .paths import get_config_path, get_data_path
from .utils import EFBChannelChatIDStr, chat_id_str_to_id, chat_id_to_str

CHANNEL_ID = ModuleID("blueset.telegram")
GAP_THRESHOLD = 20
LOSS_INTRODUCED_AT = datetime.datetime(2026, 7, 14, 18, 22, 3, tzinfo=datetime.timezone.utc)
RECOVERY_SCAN_START = datetime.datetime(2026, 7, 13, tzinfo=datetime.timezone.utc)


@dataclass(frozen=True, order=True)
class MsgLogGap:
    chat_id: int
    left: int
    right: int

    @property
    def missing_count(self) -> int:
        return self.right - self.left - 1


@dataclass(frozen=True)
class BackfillRow:
    master_msg_id: str
    slave_message_id: str
    text: str
    slave_origin_uid: str
    slave_member_uid: str
    media_type: str
    mime: Optional[str]
    msg_type: str
    sent_to: str
    sender_bot_id: Optional[str]
    time: Optional[datetime.datetime]


@dataclass(frozen=True)
class GapResult:
    gap: MsgLogGap
    inserted: int
    skipped: Mapping[str, int]


@dataclass(frozen=True, order=True)
class PendingGap:
    gap: MsgLogGap
    cursor: int


class HistorySource(Protocol):
    def iter_gap(self, gap: MsgLogGap) -> AsyncIterator[object]: ...


class TelethonHistorySource:
    def __init__(self, client: object) -> None:
        self.client = client

    async def iter_gap(self, gap: MsgLogGap) -> AsyncIterator[object]:
        lower_bound = gap.left
        cutoff_probe = self.client.iter_messages(  # type: ignore[attr-defined]
            gap.chat_id,
            limit=1,
            offset_date=RECOVERY_SCAN_START,
        )
        async for cutoff_anchor in cutoff_probe:
            cutoff_anchor_id = getattr(cutoff_anchor, "id", None)
            if isinstance(cutoff_anchor_id, bool) or not isinstance(cutoff_anchor_id, int):
                raise ValueError(f"Telegram history cutoff probe for {gap.chat_id} has no integer message ID")
            lower_bound = max(lower_bound, cutoff_anchor_id)
            break
        if lower_bound >= gap.right - 1:
            return
        iterator = self.client.iter_messages(  # type: ignore[attr-defined]
            gap.chat_id,
            min_id=lower_bound,
            max_id=gap.right,
            reverse=True,
        )
        async for message in iterator:
            yield message


class MsgLogBackfillStore:
    """Read MsgLog anchors and persist short, restartable recovery writes."""

    INSERT_CHUNK_SIZE = 400
    CHUNK_MESSAGE_SPAN = 1000

    def find_gaps(self) -> list[MsgLogGap]:
        message_ids: dict[int, list[int]] = defaultdict(list)
        for value in MsgLog.select(MsgLog.master_msg_id).tuples().iterator():
            try:
                chat_id_text, message_id_text = str(value[0]).split(".", 1)
                chat_id = int(chat_id_text)
                message_id = int(message_id_text)
            except (TypeError, ValueError):
                continue
            message_ids[chat_id].append(message_id)

        gaps: list[MsgLogGap] = []
        for chat_id in sorted(message_ids):
            ordered = sorted(set(message_ids[chat_id]))
            if ordered and ordered[0] - 1 > GAP_THRESHOLD:
                gaps.append(MsgLogGap(chat_id, 0, ordered[0]))
            gaps.extend(
                MsgLogGap(chat_id, left, right)
                for left, right in zip(ordered, ordered[1:])
                if right - left - 1 > GAP_THRESHOLD
            )
        return gaps

    def topic_associations(self, chat_id: int) -> dict[int, EFBChannelChatIDStr]:
        query = TopicAssoc.select(TopicAssoc.message_thread_id, TopicAssoc.slave_uid).where(
            TopicAssoc.topic_chat_id == str(chat_id)
        )
        associations: dict[int, EFBChannelChatIDStr] = {}
        rows = cast(Iterable[tuple[object, object]], query.tuples())
        for thread_id, slave_uid in rows:
            try:
                associations[int(str(thread_id))] = EFBChannelChatIDStr(str(slave_uid))
            except (TypeError, ValueError):
                continue
        return associations

    def snapshot_gaps(self) -> list[PendingGap]:
        with database.atomic():
            checkpoints = self._pending_gaps()
            if checkpoints:
                for pending_gap in checkpoints:
                    if self._is_legacy_leading_checkpoint(pending_gap):
                        MsgLogBackfillCheckpoint.update(left=0, cursor=1).where(
                            (MsgLogBackfillCheckpoint.chat_id == pending_gap.gap.chat_id)
                            & (MsgLogBackfillCheckpoint.left == pending_gap.gap.left)
                            & (MsgLogBackfillCheckpoint.right == pending_gap.gap.right)
                            & (MsgLogBackfillCheckpoint.cursor == pending_gap.cursor)
                        ).execute()
                    elif pending_gap.cursor <= pending_gap.gap.left:
                        MsgLogBackfillCheckpoint.update(cursor=pending_gap.gap.left + 1).where(
                            (MsgLogBackfillCheckpoint.chat_id == pending_gap.gap.chat_id)
                            & (MsgLogBackfillCheckpoint.left == pending_gap.gap.left)
                            & (MsgLogBackfillCheckpoint.right == pending_gap.gap.right)
                            & (MsgLogBackfillCheckpoint.cursor == pending_gap.cursor)
                        ).execute()
                return self._pending_gaps()
            gaps = self.find_gaps()
            if gaps:
                MsgLogBackfillCheckpoint.insert_many([
                    {
                        "chat_id": gap.chat_id,
                        "left": gap.left,
                        "right": gap.right,
                        "cursor": gap.left + 1,
                    }
                    for gap in gaps
                ]).execute()
            return self._pending_gaps()

    @staticmethod
    def _is_legacy_leading_checkpoint(pending_gap: PendingGap) -> bool:
        if pending_gap.gap.left != 1 or pending_gap.cursor != 1:
            return False
        master_msg_id = f"{pending_gap.gap.chat_id}.1"
        return not MsgLog.select().where(MsgLog.master_msg_id == master_msg_id).exists()

    @staticmethod
    def _pending_gaps() -> list[PendingGap]:
        return [
            PendingGap(MsgLogGap(row.chat_id, row.left, row.right), row.cursor)
            for row in MsgLogBackfillCheckpoint.select().order_by(
                MsgLogBackfillCheckpoint.chat_id,
                MsgLogBackfillCheckpoint.left,
                MsgLogBackfillCheckpoint.right,
            )
        ]

    def insert_chunk_and_advance(
        self,
        pending_gap: PendingGap,
        next_cursor: int,
        rows: Sequence[BackfillRow],
    ) -> int:
        if not pending_gap.gap.left < pending_gap.cursor < next_cursor <= pending_gap.gap.right:
            raise ValueError("Backfill checkpoint cursor is outside its gap")
        with database.atomic():
            inserted = self._insert_rows(rows)
            where = (
                (MsgLogBackfillCheckpoint.chat_id == pending_gap.gap.chat_id)
                & (MsgLogBackfillCheckpoint.left == pending_gap.gap.left)
                & (MsgLogBackfillCheckpoint.right == pending_gap.gap.right)
                & (MsgLogBackfillCheckpoint.cursor == pending_gap.cursor)
            )
            if next_cursor == pending_gap.gap.right:
                changed = MsgLogBackfillCheckpoint.delete().where(where).execute()
            else:
                changed = MsgLogBackfillCheckpoint.update(cursor=next_cursor).where(where).execute()
            if changed != 1:
                raise RuntimeError("MsgLog backfill checkpoint changed concurrently")
            return inserted

    def _insert_rows(self, rows: Sequence[BackfillRow]) -> int:
        if not rows:
            return 0
        payloads = [row.__dict__ for row in rows]
        inserted = 0
        for batch in chunked(payloads, self.INSERT_CHUNK_SIZE):
            message_ids = [str(row["master_msg_id"]) for row in batch]
            query = MsgLog.select(MsgLog.master_msg_id, MsgLog.master_msg_id_alt).where(
                (MsgLog.master_msg_id.in_(message_ids)) |
                (MsgLog.master_msg_id_alt.in_(message_ids))
            ).tuples()
            existing = {
                str(message_id)
                for row in cast(Iterable[tuple[object, object]], query)
                for message_id in row
                if message_id is not None
            }
            rows_to_insert = [row for row in batch if row["master_msg_id"] not in existing]
            if rows_to_insert:
                inserted += self._insert_batch(rows_to_insert)
        return inserted

    @staticmethod
    def _insert_batch(rows: Sequence[dict[str, object]]) -> int:
        return int(MsgLog.insert_many(rows).on_conflict_ignore().as_rowcount().execute())


class MsgLogGapBackfiller:
    def __init__(
        self,
        store: MsgLogBackfillStore,
        history: HistorySource | Sequence[HistorySource],
        *,
        main_bot_id: int,
        auxiliary_bot_ids: Iterable[int] = (),
    ) -> None:
        self.store = store
        self.histories = tuple(history) if isinstance(history, Sequence) else (history,)
        if not self.histories:
            raise ValueError("At least one Telegram history source is required")
        self.main_bot_id = main_bot_id
        self.auxiliary_bot_ids = frozenset(auxiliary_bot_ids)

    async def run(self) -> list[GapResult]:
        results: list[GapResult] = []
        for pending_gap in self.store.snapshot_gaps():
            inserted = 0
            skipped: Counter[str] = Counter()
            cursor = pending_gap.cursor
            while cursor < pending_gap.gap.right:
                next_cursor = min(cursor + self.store.CHUNK_MESSAGE_SPAN, pending_gap.gap.right)
                chunk_gap = MsgLogGap(pending_gap.gap.chat_id, cursor - 1, next_cursor)
                rows, chunk_skipped = await self._fetch_gap(chunk_gap)
                inserted += self.store.insert_chunk_and_advance(
                    PendingGap(pending_gap.gap, cursor), next_cursor, rows
                )
                skipped.update(chunk_skipped)
                cursor = next_cursor
            results.append(GapResult(pending_gap.gap, inserted, dict(skipped)))
        return results

    async def _fetch_gap(self, gap: MsgLogGap) -> tuple[list[BackfillRow], Counter[str]]:
        associations = self.store.topic_associations(gap.chat_id)
        rows: list[BackfillRow] = []
        skipped: Counter[str] = Counter()
        messages_by_id: dict[int, object] = {}
        for history in self.histories:
            messages = await self._read_gap(history, gap)
            for message in messages:
                messages_by_id.setdefault(int(getattr(message, "id")), message)

        for message_id in sorted(messages_by_id):
            message = messages_by_id[message_id]
            skip_reason, row = await self._build_row(message, gap.chat_id, associations)
            if row is None:
                skipped[skip_reason] += 1
            else:
                rows.append(row)
        skipped["deleted"] += gap.missing_count - len(messages_by_id)
        return rows, skipped

    @staticmethod
    async def _read_gap(history: HistorySource, gap: MsgLogGap) -> list[object]:
        messages: list[object] = []
        previous_id = gap.left
        async for message in history.iter_gap(gap):
            message_id = getattr(message, "id", None)
            if isinstance(message_id, bool) or not isinstance(message_id, int):
                raise ValueError(f"Telegram history for {gap.chat_id} contains a message without an integer ID")
            if not gap.left < message_id < gap.right or message_id <= previous_id:
                raise ValueError(f"Telegram history for {gap.chat_id} is outside the requested ascending gap")
            previous_id = message_id
            messages.append(message)
        return messages

    async def _build_row(
        self,
        message: object,
        chat_id: int,
        associations: Mapping[int, EFBChannelChatIDStr],
    ) -> tuple[str, Optional[BackfillRow]]:
        if type(message).__name__ == "MessageEmpty":
            return "deleted", None
        source_time = self._message_time(message)
        if source_time is None:
            return "unknown_time", None
        if source_time < LOSS_INTRODUCED_AT:
            return "before_loss", None
        if getattr(message, "action", None) is not None:
            return "service", None
        reply_to = getattr(message, "reply_to", None)
        topic_id = getattr(reply_to, "reply_to_top_id", None)
        if topic_id is None:
            topic_id = getattr(reply_to, "reply_to_msg_id", None)
        if isinstance(topic_id, bool) or not isinstance(topic_id, int) or topic_id not in associations:
            return "unmapped", None

        sender = getattr(message, "sender", None)
        if sender is None:
            get_sender = getattr(message, "get_sender", None)
            if not callable(get_sender):
                raise ValueError(f"Telegram message {chat_id}.{getattr(message, 'id', '?')} has no sender entity")
            sender = await get_sender()
        if sender is None:
            raise ValueError(f"Telegram message {chat_id}.{getattr(message, 'id', '?')} has no sender entity")
        if not bool(getattr(sender, "bot", False)):
            return "human", None
        sender_id = getattr(sender, "id", None)
        if isinstance(sender_id, bool) or not isinstance(sender_id, int):
            raise ValueError(f"Telegram message {chat_id}.{getattr(message, 'id', '?')} has no bot identity")
        if sender_id != self.main_bot_id and sender_id not in self.auxiliary_bot_ids:
            return "unconfigured_bot", None
        slave_uid = associations[topic_id]
        slave_channel_id, _, _ = chat_id_str_to_id(slave_uid)
        media_type, msg_type, mime = self._content_type(message)
        message_id = int(getattr(message, "id"))
        master_msg_id = f"{chat_id}.{message_id}"
        return "eligible", BackfillRow(
            master_msg_id=master_msg_id,
            slave_message_id=f"{SYNTHETIC_MSGLOG_PREFIX}{master_msg_id}",
            text=str(getattr(message, "message", "") or ""),
            slave_origin_uid=str(slave_uid),
            slave_member_uid=str(chat_id_to_str(slave_channel_id, ChatID("__self__"))),
            media_type=media_type,
            mime=mime,
            msg_type=msg_type,
            sent_to=str(CHANNEL_ID),
            sender_bot_id=None if sender_id == self.main_bot_id else str(sender_id),
            time=source_time,
        )

    @staticmethod
    def _message_time(message: object) -> Optional[datetime.datetime]:
        source_time = getattr(message, "date", None)
        if not isinstance(source_time, datetime.datetime) or source_time.tzinfo is None:
            return None
        return source_time.astimezone(datetime.timezone.utc)

    @staticmethod
    def _content_type(message: object) -> tuple[str, str, Optional[str]]:
        media = getattr(message, "media", None)
        if media is None:
            return "Text", "Text", None
        if type(media).__name__ == "MessageMediaPhoto" or getattr(media, "photo", None) is not None:
            return "Photo", "Image", "image/jpeg"
        if type(media).__name__ == "MessageMediaGeo" or getattr(media, "geo", None) is not None:
            return "Location", "Location", None
        document = getattr(media, "document", None)
        mime = getattr(document, "mime_type", None) if document is not None else None
        return "Document", "File", str(mime) if mime else None


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill MsgLog gaps larger than 20 Telegram messages")
    parser.add_argument("--profile", default="default", help="EFB profile containing the Telegram Master config")
    parser.add_argument("--api-id", type=int, default=os.environ.get("TELEGRAM_API_ID"))
    parser.add_argument("--api-hash", default=os.environ.get("TELEGRAM_API_HASH"))
    parser.add_argument("--session", type=Path, help="Base path for per-bot Telethon sessions")
    args = parser.parse_args(argv)
    if not args.api_id or not args.api_hash:
        parser.error("--api-id and --api-hash (or TELEGRAM_API_ID and TELEGRAM_API_HASH) are required")
    return args


async def _run_command(args: argparse.Namespace) -> list[GapResult]:
    coordinator.profile = args.profile
    config_path = get_config_path(CHANNEL_ID)
    with config_path.open() as config_file:
        config = YAML(typ="safe").load(config_file) or {}
    token = config.get("token")
    if not isinstance(token, str) or not token.partition(":")[0].isdigit():
        raise ValueError(f"Telegram bot token is missing from {config_path}")
    main_bot_id = int(token.partition(":")[0])
    bot_tokens = [token]
    auxiliary_bots = config.get("auxiliary_bots", [])
    if not isinstance(auxiliary_bots, list):
        raise ValueError(f"auxiliary_bots must be a list in {config_path}")
    for index, auxiliary_bot in enumerate(auxiliary_bots):
        auxiliary_token = auxiliary_bot.get("token") if isinstance(auxiliary_bot, dict) else None
        if not isinstance(auxiliary_token, str) or not auxiliary_token.partition(":")[0].isdigit():
            raise ValueError(f"auxiliary_bots[{index}] has an invalid Telegram bot token in {config_path}")
        bot_tokens.append(auxiliary_token)

    manager = DatabaseManager(SimpleNamespace(channel_id=CHANNEL_ID, config=config))  # type: ignore[arg-type]
    session = args.session or get_data_path(CHANNEL_ID) / "msglog-backfill"
    try:
        from telethon import TelegramClient

        clients = []
        histories = []
        try:
            for bot_token in bot_tokens:
                bot_id = bot_token.partition(":")[0]
                client = TelegramClient(
                    str(session.with_name(f"{session.name}-{bot_id}")),
                    args.api_id,
                    args.api_hash,
                    receive_updates=False,
                )
                try:
                    await client.start(bot_token=bot_token)
                    bot = await client.get_me()
                    if (
                        not bool(getattr(bot, "bot", False))
                        or getattr(bot, "id", None) != int(bot_id)
                    ):
                        raise ValueError(f"Telethon session does not belong to configured bot {bot_id}")
                except Exception as error:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"Could not initialize configured Telegram bot {bot_id} for MsgLog backfill"
                    ) from error
                clients.append(client)
                histories.append(TelethonHistorySource(client))
            return await MsgLogGapBackfiller(
                MsgLogBackfillStore(), histories, main_bot_id=main_bot_id,
                auxiliary_bot_ids=(int(bot_token.partition(":")[0]) for bot_token in bot_tokens[1:]),
            ).run()
        finally:
            for client in clients:
                await client.disconnect()
    finally:
        manager.stop_worker()


def main(argv: Optional[Sequence[str]] = None) -> int:
    results = asyncio.run(_run_command(_parse_args(argv)))
    for result in results:
        skipped = ", ".join(f"{name}={count}" for name, count in sorted(result.skipped.items()) if count)
        print(
            f"{result.gap.chat_id} ({result.gap.left}, {result.gap.right}): "
            f"inserted={result.inserted}" + (f", skipped {skipped}" if skipped else "")
        )
    print(f"Completed {len(results)} gap(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
