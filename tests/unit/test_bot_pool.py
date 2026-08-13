from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from efb_telegram_master.auxiliary_bot import AuxiliaryBot, MembershipProbeShutdownTimeout
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


def test_successful_membership_recheck_discards_stale_affinities_and_deduplicates_later_probes() -> None:
    first_probe_finished = threading.Event()
    second_probe_started = threading.Event()
    release_second_probe = threading.Event()
    probe_count = 0

    def get_chat_member(_chat_id: int, _bot_id: int) -> SimpleNamespace:
        nonlocal probe_count
        probe_count += 1
        if probe_count == 1:
            first_probe_finished.set()
            return SimpleNamespace(status="member")
        second_probe_started.set()
        assert release_second_probe.wait(1)
        return SimpleNamespace(status="left")

    with patch("efb_telegram_master.auxiliary_bot.telegram.Bot"):
        auxiliary = AuxiliaryBot("123:token")
    auxiliary.bot_id = 10
    auxiliary.async_bot.get_chat_member.side_effect = get_chat_member
    auxiliary.update_membership(100, True)
    pool = BotPool([auxiliary])
    try:
        pool.record_successful_auxiliary_send("slave-a", 10)
        pool.record_possible_membership_failure("slave-a", 10, 100)
        assert first_probe_finished.wait(1)

        deadline = time.monotonic() + 1
        while auxiliary.check_membership_tri(100) is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert auxiliary.check_membership_tri(100) is True

        pool.record_successful_auxiliary_send("slave-b", 10)
        pool.record_possible_membership_failure("slave-b", 10, 100)
        assert second_probe_started.wait(1)
        pool.record_possible_membership_failure("slave-b", 10, 100)
        assert probe_count == 2
        release_second_probe.set()

        deadline = time.monotonic() + 1
        while pool.preferred_sender("slave-b") is auxiliary and time.monotonic() < deadline:
            time.sleep(0.01)

        assert pool.preferred_sender("slave-a") is auxiliary
        assert pool.preferred_sender("slave-b") is None
    finally:
        release_second_probe.set()
        pool.shutdown()


def test_shutdown_uses_one_deadline_for_all_bots_and_disables_affinity_callbacks(monkeypatch) -> None:
    first = bot(10)
    second = bot(20)
    pool = BotPool([first, second])
    observed_deadlines: list[float] = []
    now = [10.0]

    def begin_shutdown() -> None:
        return None

    def wait_for_membership_shutdown(deadline: float) -> bool:
        observed_deadlines.append(deadline)
        now[0] += 3.0
        return True

    first.begin_membership_shutdown.side_effect = begin_shutdown
    second.begin_membership_shutdown.side_effect = begin_shutdown
    first.wait_for_membership_shutdown.side_effect = wait_for_membership_shutdown
    second.wait_for_membership_shutdown.side_effect = wait_for_membership_shutdown
    monkeypatch.setattr("efb_telegram_master.bot_pool.time.monotonic", lambda: now[0])

    pool.record_successful_auxiliary_send("slave-a", 10)
    pool._membership_failure_slaves[(10, 100)] = {"slave-a"}
    pool.shutdown()
    first._membership_changed_callback(first, 100, False)

    assert observed_deadlines == [15.0, 15.0]
    assert pool.preferred_sender("slave-a") is first
    assert pool._membership_failure_slaves == {(10, 100): {"slave-a"}}


def test_shutdown_reports_unjoined_membership_workers_after_stopping_every_bot() -> None:
    first = bot(10)
    second = bot(20)
    first.wait_for_membership_shutdown.return_value = False
    second.wait_for_membership_shutdown.return_value = True
    pool = BotPool([first, second])

    with pytest.raises(MembershipProbeShutdownTimeout, match="10"):
        pool.shutdown()

    first.begin_membership_shutdown.assert_called_once_with()
    second.begin_membership_shutdown.assert_called_once_with()
    first.wait_for_membership_shutdown.assert_called_once()
    second.wait_for_membership_shutdown.assert_called_once()


def test_shutdown_attempts_every_bot_when_one_begin_fails_and_another_probe_is_blocked() -> None:
    first = bot(10)
    second = bot(20)
    begin_error = RuntimeError("second probe shutdown failed")
    second.begin_membership_shutdown.side_effect = begin_error
    first.wait_for_membership_shutdown.return_value = False
    second.wait_for_membership_shutdown.return_value = True
    pool = BotPool([first, second])

    assert pool.begin_shutdown() == (begin_error,)
    assert pool.wait_for_shutdown(time.monotonic() + 0.1) == (10,)

    first.begin_membership_shutdown.assert_called_once_with()
    second.begin_membership_shutdown.assert_called_once_with()
    first.wait_for_membership_shutdown.assert_called_once()
    second.wait_for_membership_shutdown.assert_called_once()
