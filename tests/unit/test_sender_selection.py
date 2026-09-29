from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import telegram.error

from efb_telegram_master.bot_manager import TelegramBotManager
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.outbound import SenderSelection


class Limiter:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.acquisitions = 0

    def peek_delay(self, _chat_id: int) -> float:
        return self.delay

    def try_acquire(self, _chat_id: int) -> bool:
        self.acquisitions += 1
        return True


def _auxiliary(bot_id: int, *, disabled: bool = False, membership: bool | None = True, delay: float = 0.0) -> Mock:
    auxiliary = Mock()
    auxiliary.bot_id = bot_id
    auxiliary.bot = object()
    auxiliary.disabled = disabled
    auxiliary.check_membership_tri.return_value = membership
    auxiliary.peek_delay.return_value = delay
    auxiliary.try_acquire_limits.return_value = True
    return auxiliary


def _manager(*auxiliaries: Mock) -> TelegramBotManager:
    manager = object.__new__(TelegramBotManager)
    manager._bot = object()
    manager._rate_limiter = Limiter()
    manager._bot_chat_disabled_until = {}
    manager._bot_chat_retry_failures = {}
    manager.bot_pool = BotPool(list(auxiliaries), manager) if auxiliaries else None
    return manager


def _task(
    *,
    required_sender_bot_id: str | None = None,
    slave_id: str | None = None,
    chat_id: int = 100,
    priority: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        telegram_chat_id=chat_id,
        required_sender_bot_id=required_sender_bot_id,
        slave_id=slave_id,
        priority=priority,
    )


def _now() -> float:
    return 1_000.0


@pytest.mark.parametrize("required_sender_bot_id", ["9", "10"])
def test_required_sender_missing_or_disabled_is_terminal(required_sender_bot_id: str) -> None:
    manager = _manager(_auxiliary(10, disabled=True))

    result = manager.select_sender(_task(required_sender_bot_id=required_sender_bot_id), _now())

    assert result.selection is None
    assert result.terminal_error_class == "required_sender_unavailable"
    assert manager._rate_limiter.acquisitions == 0


def test_required_sender_confirmed_non_member_is_terminal_without_acquisition() -> None:
    required = _auxiliary(10, membership=False)
    manager = _manager(required)

    result = manager.select_sender(_task(required_sender_bot_id="10"), _now())

    assert result.selection is None
    assert result.terminal_error_class == "required_sender_unavailable"
    required.try_acquire_limits.assert_not_called()


def test_required_sender_unknown_membership_rechecks_after_250ms() -> None:
    required = _auxiliary(10, membership=None)
    manager = _manager(required)
    now = _now()

    result = manager.select_sender(_task(required_sender_bot_id="10"), now)

    assert result.selection is None
    assert result.retry_at == now + 0.25


def test_main_bot_sentinel_selects_main_bot_without_sender_id() -> None:
    manager = _manager()

    result = manager.select_sender(_task(required_sender_bot_id="__main__"), _now())

    assert result.selection is not None
    assert result.selection.sender is manager._bot
    assert result.selection.sender_bot_id is None


def test_unknown_auxiliary_membership_does_not_block_selectable_main_sender() -> None:
    unknown = _auxiliary(10, membership=None)
    manager = _manager(unknown)
    now = _now()

    result = manager.select_sender(_task(), now)

    assert result.selection is not None
    assert result.selection.sender is manager._bot


def test_unknown_auxiliary_membership_does_not_block_selectable_auxiliary_sender() -> None:
    unknown = _auxiliary(10, membership=None)
    selectable = _auxiliary(20, membership=True)
    manager = _manager(unknown, selectable)
    manager._rate_limiter.delay = 1.0

    result = manager.select_sender(_task(), _now())

    assert result.selection is not None
    assert result.selection.sender_bot_id == "20"


def test_affinity_wins_ties_then_main_wins_without_affinity() -> None:
    preferred = _auxiliary(10)
    other = _auxiliary(20)
    manager = _manager(preferred, other)
    assert manager.bot_pool is not None
    manager.bot_pool.record_successful_auxiliary_send("slave-a", 10)

    preferred_result = manager.select_sender(_task(slave_id="slave-a"), _now())
    main_result = manager.select_sender(_task(slave_id="slave-b"), _now())

    assert preferred_result.selection.sender_bot_id == "10"
    assert main_result.selection.sender_bot_id is None


def test_affinity_mapping_changes_only_after_successful_auxiliary_completion() -> None:
    auxiliary = _auxiliary(10)
    manager = _manager(auxiliary)
    task = _task(slave_id="slave-a")

    selection_result = manager.select_sender(task, _now())
    assert manager.bot_pool is not None
    assert manager.bot_pool.preferred_sender("slave-a") is None

    assert selection_result.selection is not None
    manager.record_queued_success(task, object(), selection_result.selection)
    assert manager.bot_pool.preferred_sender("slave-a") is None

    auxiliary_selection = SenderSelection(auxiliary.bot, "10")
    manager.record_queued_success(task, object(), auxiliary_selection)
    assert manager.bot_pool.preferred_sender("slave-a") is auxiliary


def test_confirmed_non_member_removes_only_the_triggering_affinity() -> None:
    first = _auxiliary(10)
    second = _auxiliary(20)
    manager = _manager(first, second)
    assert manager.bot_pool is not None
    manager.bot_pool.record_successful_auxiliary_send("slave-a", 10)
    manager.bot_pool.record_successful_auxiliary_send("slave-b", 10)

    task = _task(slave_id="slave-a")
    manager.record_queued_failure(task, Exception("membership probe pending"), SenderSelection(first.bot, "10"))
    manager.remove_confirmed_non_member_affinity_for_sender_chat("10", task.telegram_chat_id)

    assert manager.bot_pool.preferred_sender("slave-a") is None
    assert manager.bot_pool.preferred_sender("slave-b") is first


def test_eventual_retry_after_uses_exact_sender_chat_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager()
    task = _task()
    selection = SenderSelection(object(), "10")
    monkeypatch.setattr("efb_telegram_master.bot_manager.time.monotonic", _now)

    first = manager.record_queued_failure(task, telegram.error.RetryAfter(20), selection)
    second = manager.record_queued_failure(task, telegram.error.RetryAfter(20), selection)
    third = manager.record_queued_failure(task, telegram.error.RetryAfter(20), selection)
    capped = manager.record_queued_failure(task, telegram.error.RetryAfter(1_000), selection)

    assert [decision.retry_at for decision in (first, second, third, capped)] == [
        1_025.0,
        1_060.0,
        1_120.0,
        1_900.0,
    ]
    assert manager._bot_chat_retry_failures == {("10", 100): 4}


def test_retry_after_cooldown_leaves_another_sender_for_the_same_chat_selectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _auxiliary(10)
    second = _auxiliary(20)
    manager = _manager(first, second)
    manager._rate_limiter.delay = 30.0
    task = _task()
    monkeypatch.setattr("efb_telegram_master.bot_manager.time.monotonic", _now)

    manager.record_queued_failure(task, telegram.error.RetryAfter(20), SenderSelection(first.bot, "10"))
    selection = manager.select_sender(task, _now())

    assert manager._bot_chat_retry_failures == {("10", 100): 1}
    assert selection.selection is not None
    assert selection.selection.sender_bot_id == "20"


def test_eventual_success_resets_only_its_sender_chat_retry_streak() -> None:
    manager = _manager()
    first = _task(chat_id=100)
    second = _task(chat_id=200)
    manager._bot_chat_retry_failures = {("10", 100): 3, ("10", 200): 2}

    decision = manager.record_queued_success(first, object(), SenderSelection(object(), "10"))

    assert decision.kind.name == "SUCCESS"
    assert manager._bot_chat_retry_failures == {("10", 200): 2}
