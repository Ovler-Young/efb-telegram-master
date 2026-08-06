from __future__ import annotations

from unittest.mock import Mock

from efb_telegram_master.bot_pool import BotPool


def bot(bot_id: int, *, disabled: bool = False, membership: bool | None = True) -> Mock:
    result = Mock()
    result.bot_id = bot_id
    result.disabled = disabled
    result.check_membership_tri.return_value = membership
    result.has_pending_probes.return_value = False
    return result


def test_candidate_bots_exclude_disabled_and_preserve_unknown_membership() -> None:
    disabled = bot(1, disabled=True)
    unknown = bot(20, membership=None)
    member = bot(3)

    candidates = BotPool([disabled, unknown, member]).candidate_bots(100)

    assert candidates == [(unknown, None), (member, True)]
    disabled.check_membership_tri.assert_not_called()


def test_successful_main_and_null_slave_do_not_change_affinity() -> None:
    auxiliary = bot(10)
    pool = BotPool([auxiliary])
    pool.record_successful_auxiliary_send("slave-a", 10)

    pool.record_successful_auxiliary_send(None, 10)

    assert pool.preferred_sender("slave-a") is auxiliary
    assert pool.preferred_sender(None) is None


def test_disabling_bot_removes_every_affinity_to_that_bot() -> None:
    first = bot(10)
    second = bot(20)
    pool = BotPool([first, second])
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)
    pool.record_successful_auxiliary_send("slave-c", 20)

    pool.disable_bot(10)

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-b") is None
    assert pool.preferred_sender("slave-c") is second


def test_membership_failure_isolated_to_the_affected_chat() -> None:
    first = bot(10)
    second = bot(20)
    pool = BotPool([first, second])

    pool.on_bot_left_chat(10, 100)

    first.update_membership.assert_called_once_with(100, False)
    second.update_membership.assert_not_called()
    assert pool.preferred_sender("unrelated-slave") is None


def test_remove_affinity_for_bot_keeps_other_bot_affinities() -> None:
    first = bot(10)
    second = bot(20)
    pool = BotPool([first, second])
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 20)

    pool.remove_affinity_for_bot(10)

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-b") is second


def test_confirmed_membership_failure_removes_only_the_failed_sender_affinity() -> None:
    first = bot(10)
    second = bot(20)
    pool = BotPool([first, second])
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)
    pool.record_successful_auxiliary_send("slave-c", 20)
    pool.record_possible_membership_failure("slave-a", 10, 100)

    first._membership_changed_callback(first, 100, False)

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-b") is first
    assert pool.preferred_sender("slave-c") is second


def test_confirmed_membership_failure_preserves_a_newer_affinity() -> None:
    first = bot(10)
    second = bot(20)
    pool = BotPool([first, second])
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_possible_membership_failure("slave-a", 10, 100)
    pool.record_successful_auxiliary_send("slave-a", 20)

    first._membership_changed_callback(first, 100, False)

    assert pool.preferred_sender("slave-a") is second
