from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram.error import RetryAfter

import efb_telegram_master.sender_policy as sender_policy_module
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.outbound_types import QueuedCall, SenderSelection
from efb_telegram_master.sender_policy import SenderPolicy
from efb_telegram_master.telegram_calls import TelegramCallAdapter


class Limiter:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.acquisitions = 0

    def peek_delay(self, _chat_id: int) -> float:
        return self.delay

    def try_acquire(self, _chat_id: int) -> bool:
        self.acquisitions += 1
        return True

    def occupancy_snapshot(self) -> dict[str, float]:
        return {"global": 0.0, "chat": 0.0}


def auxiliary(bot_id: int, *, disabled: bool = False, membership: bool | None = True, delay: float = 0.0) -> Mock:
    result = Mock()
    result.bot_id = bot_id
    result.bot = object()
    result.disabled = disabled
    result.check_membership_tri.return_value = membership
    result.peek_delay.return_value = delay
    result.try_acquire_limits.return_value = True
    return result


def policy(*auxiliaries: Mock, main_delay: float = 0.0, main_bot: object | None = None) -> tuple[SenderPolicy, BotPool | None, object, Limiter]:
    main_bot = main_bot or object()
    limiter = Limiter(main_delay)
    pool = BotPool(list(auxiliaries)) if auxiliaries else None
    return SenderPolicy(main_bot, pool, limiter), pool, main_bot, limiter


def call(*, required_sender_bot_id: str | None = None, slave_id: str | None = None, chat_id: int = 100) -> QueuedCall:
    return QueuedCall("send_message", (), {}, chat_id, slave_id, required_sender_bot_id)


@pytest.mark.parametrize(
    ("auxiliaries", "required_sender_bot_id"),
    [([], "9"), ([auxiliary(10, disabled=True)], "10")],
)
def test_required_missing_or_disabled_sender_is_terminal(auxiliaries: list[Mock], required_sender_bot_id: str) -> None:
    sender_policy, _pool, _main_bot, limiter = policy(*auxiliaries)

    decision = sender_policy.select(call(required_sender_bot_id=required_sender_bot_id), now=1_000.0)

    assert decision.error == "required_sender_unavailable"
    assert limiter.acquisitions == 0


def test_required_sender_confirmed_non_member_is_terminal_without_acquisition() -> None:
    required = auxiliary(10, membership=False)
    sender_policy, _pool, _main_bot, _limiter = policy(required)

    decision = sender_policy.select(call(required_sender_bot_id="10"), now=1_000.0)

    assert decision.error == "required_sender_unavailable"
    required.try_acquire_limits.assert_not_called()


def test_required_sender_unknown_membership_rechecks_after_250ms() -> None:
    sender_policy, _pool, _main_bot, _limiter = policy(auxiliary(10, membership=None))

    decision = sender_policy.select(call(required_sender_bot_id="10"), now=1_000.0)

    assert decision.selection is None
    assert decision.retry_at == 1_000.25


def test_main_bot_sentinel_selects_main_bot_without_sender_id() -> None:
    sender_policy, _pool, main_bot, _limiter = policy()

    decision = sender_policy.select(call(required_sender_bot_id="__main__"), now=1_000.0)

    assert decision.selection == SenderSelection(main_bot, None)


def test_unknown_auxiliary_membership_does_not_block_selectable_main_sender() -> None:
    sender_policy, _pool, main_bot, _limiter = policy(auxiliary(10, membership=None))

    decision = sender_policy.select(call(), now=1_000.0)

    assert decision.selection == SenderSelection(main_bot, None)


def test_unknown_auxiliary_membership_does_not_block_selectable_auxiliary_sender() -> None:
    sender_policy, _pool, _main_bot, _limiter = policy(auxiliary(10, membership=None), auxiliary(20), main_delay=1.0)

    decision = sender_policy.select(call(), now=1_000.0)

    assert decision.selection is not None
    assert decision.selection.sender_bot_id == "20"


def test_affinity_wins_ties_then_confirmed_auxiliary_bootstraps_affinity() -> None:
    preferred = auxiliary(10)
    other = auxiliary(20)
    sender_policy, pool, _main_bot, _limiter = policy(preferred, other)
    assert pool is not None
    pool.record_successful_auxiliary_send("slave-a", 10)

    preferred_decision = sender_policy.select(call(slave_id="slave-a"), now=1_000.0)
    unbound_decision = sender_policy.select(call(slave_id="slave-b"), now=1_000.0)

    assert preferred_decision.selection is not None
    assert preferred_decision.selection.sender_bot_id == "10"
    assert unbound_decision.selection is not None
    assert unbound_decision.selection.sender_bot_id == "10"


def test_affinity_is_recorded_only_after_successful_auxiliary_execution() -> None:
    sender = SimpleNamespace(send_message=Mock(return_value=object()))
    auxiliary_bot = auxiliary(10)
    auxiliary_bot.bot = sender
    main_bot = SimpleNamespace(send_message=Mock(return_value=object()))
    _sender_policy, pool, main_bot, _limiter = policy(auxiliary_bot, main_bot=main_bot)
    assert pool is not None
    queued_call = call(slave_id="slave-a")

    main_adapter = TelegramCallAdapter(pool)
    main_adapter.execute_primary(queued_call, SenderSelection(main_bot, None))
    main_adapter.record_successful_send(queued_call, SenderSelection(main_bot, None))
    assert pool.preferred_sender("slave-a") is None

    main_adapter.execute_primary(queued_call, SenderSelection(sender, "10"))
    main_adapter.record_successful_send(queued_call, SenderSelection(sender, "10"))

    assert pool.preferred_sender("slave-a") is auxiliary_bot


def test_confirmed_non_member_excludes_affinity_sender_and_uses_another_confirmed_auxiliary() -> None:
    first = auxiliary(10, membership=False)
    second = auxiliary(20)
    sender_policy, pool, _main_bot, _limiter = policy(first, second)
    assert pool is not None
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 20)

    decision = sender_policy.select(call(slave_id="slave-a"), now=1_000.0)

    assert decision.selection == SenderSelection(second.bot, "20")
    assert pool.preferred_sender("slave-b") is second


def test_retry_after_cooldown_is_scoped_to_exact_sender_and_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    auxiliary_bot = auxiliary(10)
    sender_policy, _pool, _main_bot, _limiter = policy(auxiliary_bot)
    sender = SenderSelection(auxiliary_bot.bot, "10")
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: 1_000.0)

    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(20), sender)

    delayed = sender_policy.select(call(required_sender_bot_id="10", chat_id=100), now=1_000.0)
    other_chat = sender_policy.select(call(required_sender_bot_id="10", chat_id=101), now=1_000.0)

    assert delayed.selection is None
    assert delayed.retry_at == 1_020.0
    assert other_chat.selection == sender


def test_retry_after_keeps_the_latest_deadline_for_one_sender_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    auxiliary_bot = auxiliary(10)
    sender_policy, _pool, _main_bot, _limiter = policy(auxiliary_bot)
    sender = SenderSelection(auxiliary_bot.bot, "10")
    clock = [1_000.0]
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: clock[0])

    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(20), sender)
    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(5), sender)
    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(1_000), sender)

    assert sender_policy.select(call(required_sender_bot_id="10", chat_id=100), now=1_000.0).retry_at == 2_000.0


def test_cooldown_snapshot_is_safe_while_retry_after_updates_arrive(monkeypatch: pytest.MonkeyPatch) -> None:
    auxiliary_bot = auxiliary(10)
    sender_policy, _pool, _main_bot, _limiter = policy(auxiliary_bot)
    sender = SenderSelection(auxiliary_bot.bot, "10")
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: 1_000.0)
    start = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        start.wait()
        for chat_id in range(2_000):
            sender_policy.record_retry_after(call(chat_id=chat_id), RetryAfter(1), sender)

    thread = threading.Thread(target=writer)
    thread.start()
    start.set()
    try:
        while thread.is_alive():
            try:
                snapshot = sender_policy.cooldown_snapshot()
                assert snapshot["auxiliary"] >= 0.0
            except BaseException as error:
                errors.append(error)
    finally:
        thread.join()

    assert errors == []


def test_send_failure_records_the_triggering_slave_and_sender_for_membership_confirmation() -> None:
    first = auxiliary(10)
    second = auxiliary(20)
    sender_policy, pool, _main_bot, _limiter = policy(first, second)
    assert pool is not None
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)

    sender_policy.record_send_failure(call(slave_id="slave-a", chat_id=100), SenderSelection(first.bot, "10"))
    first._membership_changed_callback(first, 100, False)

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-b") is first


def test_retry_after_cooldown_leaves_another_sender_for_the_same_chat_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    first = auxiliary(10)
    second = auxiliary(20)
    sender_policy, _pool, _main_bot, _limiter = policy(first, second, main_delay=30.0)
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: 1_000.0)
    queued_call = call()

    sender_policy.record_retry_after(queued_call, RetryAfter(20), SenderSelection(first.bot, "10"))
    decision = sender_policy.select(queued_call, now=1_000.0)

    assert decision.selection is not None
    assert decision.selection.sender_bot_id == "20"


def test_main_bot_remains_selectable_when_no_auxiliary_is_available() -> None:
    sender_policy, _pool, main_bot, _limiter = policy(auxiliary(10, disabled=True))

    decision = sender_policy.select(call(), now=1_000.0)

    assert decision.selection == SenderSelection(main_bot, None)


def test_main_bot_falls_back_when_all_confirmed_auxiliaries_are_unavailable() -> None:
    sender_policy, _pool, main_bot, _limiter = policy(auxiliary(10, membership=False), auxiliary(20, membership=False))

    decision = sender_policy.select(call(), now=1_000.0)

    assert decision.selection == SenderSelection(main_bot, None)
