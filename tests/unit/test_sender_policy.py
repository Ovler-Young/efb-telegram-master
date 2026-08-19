from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from efb_telegram_master.outbound.outbound_types import SenderSelection
from efb_telegram_master.transport.telegram_calls import TelegramCallAdapter
from tests.unit.sender_policy_support import auxiliary, call, policy


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


def test_auxiliary_reservation_falls_back_to_main_until_the_auxiliary_becomes_available() -> None:
    auxiliary_bot = auxiliary(10)
    reserved = {"value": False}

    def peek_delay(_chat_id: int) -> float:
        return 30.0 if reserved["value"] else 0.0

    def acquire(_chat_id: int) -> bool:
        if reserved["value"]:
            return False
        reserved["value"] = True
        return True

    auxiliary_bot.peek_delay.side_effect = peek_delay
    auxiliary_bot.try_acquire_limits.side_effect = acquire
    sender_policy, _pool, main_bot, _limiter = policy(auxiliary_bot)

    first = sender_policy.select(call(chat_id=100), now=1_000.0)
    assert first.selection == SenderSelection(auxiliary_bot.bot, "10")
    assert sender_policy.acquire(first.selection, 100)

    while_reserved = sender_policy.select(call(chat_id=100), now=1_000.0)
    assert while_reserved.selection == SenderSelection(main_bot, None)

    reserved["value"] = False
    after_quiescence = sender_policy.select(call(chat_id=100), now=1_000.0)
    assert after_quiescence.selection == SenderSelection(auxiliary_bot.bot, "10")


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


@pytest.mark.parametrize(
    "auxiliaries",
    [
        (auxiliary(10, disabled=True),),
        (auxiliary(10, membership=False), auxiliary(20, membership=False)),
    ],
)
def test_main_bot_falls_back_when_auxiliaries_are_unavailable(auxiliaries: tuple[Mock, ...]) -> None:
    sender_policy, _pool, main_bot, _limiter = policy(*auxiliaries)

    decision = sender_policy.select(call(), now=1_000.0)

    assert decision.selection == SenderSelection(main_bot, None)
