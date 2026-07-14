from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master.bot_pool import BotPool


def _bot(
    bot_id: int,
    *,
    disabled: bool = False,
    membership: bool | None = True,
    limiter_delay: float = 0.0,
) -> Mock:
    bot = Mock()
    bot.bot_id = bot_id
    bot.disabled = disabled
    bot.check_membership_tri.return_value = membership
    bot.peek_delay.return_value = limiter_delay
    bot.has_pending_probes.return_value = False
    return bot


def _pool(*bots: Mock) -> BotPool:
    return BotPool(list(bots), SimpleNamespace())


def test_candidate_bots_exclude_disabled_and_preserve_unknown_membership() -> None:
    disabled = _bot(1, disabled=True)
    unknown = _bot(20, membership=None)
    member = _bot(3)

    candidates = _pool(disabled, unknown, member).candidate_bots(100)

    assert candidates == [(unknown, None), (member, True)]
    disabled.check_membership_tri.assert_not_called()


def test_affinity_is_created_only_after_successful_auxiliary_send() -> None:
    bot = _bot(10)
    pool = _pool(bot)

    assert pool.preferred_sender("slave-a") is None
    pool.record_successful_auxiliary_send("slave-a", 10)

    assert pool.preferred_sender("slave-a") is bot


def test_successful_main_and_null_slave_do_not_change_affinity() -> None:
    bot = _bot(10)
    pool = _pool(bot)
    pool.record_successful_auxiliary_send("slave-a", 10)

    pool.record_successful_auxiliary_send(None, 10)

    assert pool.preferred_sender("slave-a") is bot
    assert pool.preferred_sender(None) is None


def test_disabling_bot_removes_every_affinity_to_that_bot() -> None:
    first = _bot(10)
    second = _bot(20)
    pool = _pool(first, second)
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)
    pool.record_successful_auxiliary_send("slave-c", 20)

    pool.disable_bot(10)

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-b") is None
    assert pool.preferred_sender("slave-c") is second


def test_confirmed_membership_failure_removes_only_matching_task_affinity() -> None:
    first = _bot(10)
    second = _bot(20)
    pool = _pool(first, second)
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)
    pool.record_successful_auxiliary_send("slave-c", 20)

    pool.remove_failed_membership_affinity("slave-a", 10)

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-b") is first
    assert pool.preferred_sender("slave-c") is second


def test_required_sender_state_reports_terminal_and_waiting_conditions() -> None:
    enabled = _bot(10, limiter_delay=3.0)
    disabled = _bot(20, disabled=True)
    unknown = _bot(30, membership=None)
    non_member = _bot(40, membership=False)
    pool = _pool(enabled, disabled, unknown, non_member)

    available = pool.required_sender_state(10, 100, cooldown_until=12.0, now=10.0)
    assert available.bot is enabled
    assert available.is_terminal is False
    assert available.is_selectable(10.0) is False
    assert available.next_deadline(10.0) == 13.0

    assert pool.required_sender_state(99, 100, cooldown_until=0.0, now=10.0).is_terminal
    assert pool.required_sender_state(20, 100, cooldown_until=0.0, now=10.0).is_terminal
    assert pool.required_sender_state(40, 100, cooldown_until=0.0, now=10.0).is_terminal
    disabled.peek_delay.assert_not_called()

    pending = pool.required_sender_state(30, 100, cooldown_until=0.0, now=10.0)
    assert pending.is_terminal is False
    assert pending.is_selectable(10.0) is False
    assert pending.next_deadline(10.0) == 10.25


def test_affinity_sender_states_expose_deadlines_and_deterministic_tie_inputs() -> None:
    first = _bot(10, limiter_delay=2.0)
    preferred = _bot(20)
    unknown = _bot(30, membership=None)
    non_member = _bot(40, membership=False)
    pool = _pool(first, preferred, unknown, non_member)
    pool.record_successful_auxiliary_send("slave-a", 20)

    states = pool.affinity_sender_states(
        100,
        "slave-a",
        cooldown_until_for_bot=lambda bot_id, _chat_id: 15.0 if bot_id == "10" else 0.0,
        now=10.0,
    )

    assert [state.bot_id for state in states] == ["10", "20", "30"]
    assert [state.tie_key for state in states] == [(2, "10"), (0, "20"), (2, "30")]
    assert states[0].next_deadline(10.0) == 15.0
    assert states[1].is_selectable(10.0)
    assert states[2].next_deadline(10.0) == 10.25
