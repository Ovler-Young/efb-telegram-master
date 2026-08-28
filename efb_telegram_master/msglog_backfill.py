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

from .db import DatabaseManager, MsgLog, SYNTHETIC_MSGLOG_PREFIX, TopicAssoc, database
from .paths import get_config_path, get_data_path
from .utils import EFBChannelChatIDStr, chat_id_str_to_id, chat_id_to_str

CHANNEL_ID = ModuleID("blueset.telegram")
GAP_THRESHOLD = 20


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


class HistorySource(Protocol):
    def iter_gap(self, gap: MsgLogGap) -> AsyncIterator[object]: ...


class TelethonHistorySource:
    def __init__(self, client: object) -> None:
        self.client = client

    async def iter_gap(self, gap: MsgLogGap) -> AsyncIterator[object]:
        iterator = self.client.iter_messages(  # type: ignore[attr-defined]
            gap.chat_id,
            min_id=gap.left,
            max_id=gap.right,
            reverse=True,
        )
        async for message in iterator:
            yield message


class MsgLogBackfillStore:
    """Read MsgLog anchors and atomically insert one completed gap."""

    INSERT_CHUNK_SIZE = 500

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

    def insert_gap(self, rows: Sequence[BackfillRow]) -> int:
        if not rows:
            return 0
        payloads = [row.__dict__ for row in rows]
        inserted = 0
        with database.atomic():
            for batch in chunked(payloads, self.INSERT_CHUNK_SIZE):
                inserted += self._insert_batch(batch)
        return inserted

    @staticmethod
    def _insert_batch(rows: Sequence[dict[str, object]]) -> int:
        return int(MsgLog.insert_many(rows).on_conflict_ignore().as_rowcount().execute())


class MsgLogGapBackfiller:
    def __init__(self, store: MsgLogBackfillStore, history: HistorySource, *, main_bot_id: int) -> None:
        self.store = store
        self.history = history
        self.main_bot_id = main_bot_id

    async def run(self) -> list[GapResult]:
        results: list[GapResult] = []
        for gap in self.store.find_gaps():
            rows, skipped = await self._fetch_gap(gap)
            inserted = self.store.insert_gap(rows)
            results.append(GapResult(gap, inserted, dict(skipped)))
        return results

    async def _fetch_gap(self, gap: MsgLogGap) -> tuple[list[BackfillRow], Counter[str]]:
        associations = self.store.topic_associations(gap.chat_id)
        rows: list[BackfillRow] = []
        skipped: Counter[str] = Counter()
        previous_id = gap.left
        seen = 0
        async for message in self.history.iter_gap(gap):
            message_id = getattr(message, "id", None)
            if isinstance(message_id, bool) or not isinstance(message_id, int):
                raise ValueError(f"Telegram history for {gap.chat_id} contains a message without an integer ID")
            if not gap.left < message_id < gap.right or message_id <= previous_id:
                raise ValueError(f"Telegram history for {gap.chat_id} is outside the requested ascending gap")
            previous_id = message_id
            seen += 1

            skip_reason, row = self._build_row(message, gap.chat_id, associations)
            if row is None:
                skipped[skip_reason] += 1
            else:
                rows.append(row)
        skipped["deleted"] += gap.missing_count - seen
        return rows, skipped

    def _build_row(
        self,
        message: object,
        chat_id: int,
        associations: Mapping[int, EFBChannelChatIDStr],
    ) -> tuple[str, Optional[BackfillRow]]:
        if type(message).__name__ == "MessageEmpty":
            return "deleted", None
        if getattr(message, "action", None) is not None:
            return "service", None
        reply_to = getattr(message, "reply_to", None)
        topic_id = getattr(reply_to, "reply_to_top_id", None)
        if topic_id is None:
            topic_id = getattr(reply_to, "reply_to_msg_id", None)
        if isinstance(topic_id, bool) or not isinstance(topic_id, int) or topic_id not in associations:
            return "unmapped", None

        sender_id = getattr(message, "sender_id", None)
        if isinstance(sender_id, bool) or not isinstance(sender_id, int):
            raise ValueError(f"Telegram message {chat_id}.{getattr(message, 'id', '?')} has no sender identity")
        slave_uid = associations[topic_id]
        slave_channel_id, _, _ = chat_id_str_to_id(slave_uid)
        media_type, msg_type, mime = self._content_type(message)
        message_id = int(getattr(message, "id"))
        master_msg_id = f"{chat_id}.{message_id}"
        source_time = getattr(message, "date", None)
        if not isinstance(source_time, datetime.datetime):
            source_time = None
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
    parser.add_argument("--session", type=Path, help="Telethon user session path")
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

    manager = DatabaseManager(SimpleNamespace(channel_id=CHANNEL_ID, config=config))  # type: ignore[arg-type]
    session = args.session or get_data_path(CHANNEL_ID) / "msglog-backfill"
    try:
        from telethon import TelegramClient

        client = TelegramClient(str(session), args.api_id, args.api_hash, receive_updates=False)
        await client.start()
        try:
            return await MsgLogGapBackfiller(
                MsgLogBackfillStore(), TelethonHistorySource(client), main_bot_id=main_bot_id
            ).run()
        finally:
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
