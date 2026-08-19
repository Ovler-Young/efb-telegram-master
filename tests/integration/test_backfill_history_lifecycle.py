import asyncio
import time
from typing import List, Set

from efb_telegram_master import utils as etm_utils
from efb_telegram_master.models import HistoryMigrationEntry

from .test_backfill_history_ingestion import POLL_INTERVAL_SECONDS, expected_stream_indices, extract_stream_indices, logs_with_prefix, messages_since_id

BACKFILL_WAIT_TIMEOUT = 6 * 60.0


def migration_activity_completed(*, activity_observed: bool, expected: Set[int], db_indices: List[int], telegram_indices: List[int], target_entry_count: int) -> bool:
    expected_count = len(expected)
    return (
        activity_observed
        and target_entry_count == 0
        and set(db_indices) == expected
        and len(db_indices) == expected_count
        and set(telegram_indices) == expected
        and len(telegram_indices) == expected_count
    )


def target_migration_entry_count(slave_chat_id: str, target_chat_id: int) -> int:
    return int(HistoryMigrationEntry.select().where((HistoryMigrationEntry.slave_chat_id == slave_chat_id) & (HistoryMigrationEntry.target_chat_id == str(target_chat_id))).count())


async def wait_for_migrated_stream_terminal(channel_with_auxiliary_bots, client, chat, chat_id: int, prefix: str, expected_count: int, *, min_message_id: int, timeout: float = BACKFILL_WAIT_TIMEOUT):
    expected = expected_stream_indices(expected_count)
    slave_chat_id = etm_utils.chat_id_to_str(chat=chat)
    deadline = time.time() + timeout
    replay_or_entry_observed = False

    last_debug = ""
    while time.time() < deadline:
        recent = await messages_since_id(client, chat_id, min_message_id)
        telegram_indices = [idx for message in recent for idx in extract_stream_indices(message.raw_text or "", prefix)]
        db_logs = logs_with_prefix(channel_with_auxiliary_bots, chat, prefix)
        db_indices = [idx for log in db_logs for idx in extract_stream_indices(log.text or "", prefix)]
        target_entry_count = target_migration_entry_count(slave_chat_id, chat_id)
        replay_or_entry_observed = replay_or_entry_observed or bool(telegram_indices) or target_entry_count > 0
        last_debug = (
            f"replay_or_entry_observed={replay_or_entry_observed}, db_idx={len(db_indices)}/{expected_count}, "
            f"tg_idx={len(telegram_indices)}/{expected_count}, target_entries={target_entry_count}, "
            f"expected_migration_sends={expected_count}"
        )
        if migration_activity_completed(
            activity_observed=replay_or_entry_observed,
            expected=expected,
            db_indices=db_indices,
            telegram_indices=telegram_indices,
            target_entry_count=target_entry_count,
        ):
            return recent
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

    raise AssertionError(f"Timed out waiting for migrated stream terminal state: {last_debug}")
