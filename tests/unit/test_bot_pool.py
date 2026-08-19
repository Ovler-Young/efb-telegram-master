from __future__ import annotations

import time
from collections import OrderedDict
from unittest.mock import Mock

import pytest

from efb_telegram_master.auxiliary_bot import MembershipProbeShutdownTimeout
from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.channel_commands import MAX_AUXILIARY_BOTS, load_channel_config
from efb_telegram_master.outbound import DEFAULT_MAX_PENDING


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


def test_null_slave_does_not_consume_affinity_capacity(monkeypatch) -> None:
    auxiliary = bot(10)
    pool = BotPool([auxiliary])
    monkeypatch.setattr(BotPool, "MAX_AFFINITY_ENTRIES", 1)

    pool.record_successful_auxiliary_send(None, 10)
    pool.record_successful_auxiliary_send("slave-a", 10)

    assert pool.preferred_sender("slave-a") is auxiliary


def test_disabling_bot_removes_every_affinity_to_that_bot() -> None:
    first = bot(10)
    second = bot(20)
    pool = BotPool([first, second])
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)
    pool.record_successful_auxiliary_send("slave-c", 20)

    pool.disable_bot(10)
    first.disabled = False

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


def test_confirmed_membership_failure_removes_only_the_failed_sender_affinity() -> None:
    first = bot(10)
    second = bot(20)
    pool = BotPool([first, second])
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)
    pool.record_successful_auxiliary_send("slave-c", 20)
    pool.record_possible_membership_failure("slave-a", 10, 100)

    first.recheck_membership.assert_called_once_with(100)
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


def test_shutdown_uses_one_deadline_for_all_bots_and_clears_affinity_state(monkeypatch) -> None:
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
    pool._membership_failure_slaves[(10, 100)] = OrderedDict({"slave-a": 10.0})
    pool.shutdown()

    assert observed_deadlines == [15.0, 15.0]
    assert pool.preferred_sender("slave-a") is None
    assert pool._membership_failure_slaves == {}


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


def test_affinity_and_membership_failure_state_are_bounded_without_evicting_live_entries(monkeypatch) -> None:
    first = bot(10)
    pool = BotPool([first])
    now = [100.0]
    monkeypatch.setattr("efb_telegram_master.bot_pool.time.monotonic", lambda: now[0])
    monkeypatch.setattr(BotPool, "MAX_AFFINITY_ENTRIES", 2)
    monkeypatch.setattr(BotPool, "MAX_MEMBERSHIP_FAILURE_ENTRIES", 1)
    monkeypatch.setattr(BotPool, "MAX_FAILURE_SLAVES_PER_MEMBERSHIP_PROBE", 2)

    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)
    pool.record_successful_auxiliary_send("slave-c", 10)

    assert pool.preferred_sender("slave-a") is first
    assert pool.preferred_sender("slave-b") is first
    assert pool.preferred_sender("slave-c") is None

    pool.record_possible_membership_failure("slave-a", 10, 100)
    pool.record_possible_membership_failure("slave-b", 10, 100)
    pool.record_possible_membership_failure("slave-c", 10, 100)
    pool.record_possible_membership_failure("slave-a", 10, 200)

    assert list(pool._membership_failure_slaves) == [(10, 100)]
    assert list(pool._membership_failure_slaves[(10, 100)]) == ["slave-a", "slave-b"]

    now[0] += BotPool.AFFINITY_TTL + 1
    pool.record_successful_auxiliary_send("slave-c", 10)

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-c") is first
    assert pool._membership_failure_slaves == {}


def test_load_channel_config_rejects_non_mapping_yaml_root(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("null\n")
    monkeypatch.setattr("efb_telegram_master.channel_commands.get_config_path", lambda _channel_id: config_path)

    with pytest.raises(ValueError, match="Config file must contain a mapping"):
        load_channel_config("tests.channel", str)


def test_load_channel_config_rejects_too_many_auxiliary_bots(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    auxiliary_bots = "".join(f'  - token: "auxiliary-{index}"\n' for index in range(MAX_AUXILIARY_BOTS + 1))
    config_path.write_text(f'token: "main"\nadmins: [1]\nauxiliary_bots:\n{auxiliary_bots}')
    monkeypatch.setattr("efb_telegram_master.channel_commands.get_config_path", lambda _channel_id: config_path)

    with pytest.raises(ValueError, match=f"at most {MAX_AUXILIARY_BOTS} entries"):
        load_channel_config("tests.channel", str)


def test_load_channel_config_rejects_boolean_admin_id(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text('token: "main"\nadmins: [true]\n')
    monkeypatch.setattr("efb_telegram_master.channel_commands.get_config_path", lambda _channel_id: config_path)

    with pytest.raises(ValueError, match="Admin ID is expected to be an int"):
        load_channel_config("tests.channel", str)


def test_load_channel_config_rejects_non_mapping_runtime_section(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text('token: "main"\nadmins: [1]\ndatabase: []\n')
    monkeypatch.setattr("efb_telegram_master.channel_commands.get_config_path", lambda _channel_id: config_path)

    with pytest.raises(ValueError, match="database must be a mapping"):
        load_channel_config("tests.channel", str)


@pytest.mark.parametrize(
    ("outbound_config", "expected_max_pending"),
    [("", DEFAULT_MAX_PENDING), ("outbound:\n  max_pending: 17\n", 17)],
    ids=["default", "configured"],
)
def test_load_channel_config_sets_outbound_pending_limit(tmp_path, monkeypatch, outbound_config: str, expected_max_pending: int) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f'token: "main"\nadmins: [1]\n{outbound_config}')
    monkeypatch.setattr("efb_telegram_master.channel_commands.get_config_path", lambda _channel_id: config_path)

    config, _mtproto_config = load_channel_config("tests.channel", str)

    assert config["outbound"]["max_pending"] == expected_max_pending


@pytest.mark.parametrize(
    ("outbound_config", "message"),
    [
        ("outbound: []\n", "outbound must be a mapping"),
        ("outbound:\n  max_pending: 0\n", "outbound.max_pending must be a positive integer"),
        ("outbound:\n  max_pending: true\n", "outbound.max_pending must be a positive integer"),
    ],
)
def test_load_channel_config_rejects_invalid_outbound_pending_limit(tmp_path, monkeypatch, outbound_config: str, message: str) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f'token: "main"\nadmins: [1]\n{outbound_config}')
    monkeypatch.setattr("efb_telegram_master.channel_commands.get_config_path", lambda _channel_id: config_path)

    with pytest.raises(ValueError, match=message):
        load_channel_config("tests.channel", str)


def test_shutdown_attempts_every_bot_after_a_partial_begin_failure() -> None:
    first = bot(10)
    second = bot(20)
    error = RuntimeError("second probe shutdown failed")
    second.begin_membership_shutdown.side_effect = error
    first.wait_for_membership_shutdown.return_value = False
    second.wait_for_membership_shutdown.return_value = True
    pool = BotPool([first, second])

    assert pool.begin_shutdown() == (error,)
    assert pool.wait_for_shutdown(time.monotonic() + 0.1) == (10,)

    first.begin_membership_shutdown.assert_called_once_with()
    second.begin_membership_shutdown.assert_called_once_with()
