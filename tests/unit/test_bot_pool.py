from types import SimpleNamespace
from unittest.mock import Mock

from efb_telegram_master.bot_pool import BotPool


def _bot(bot_id: int, *, disabled: bool = False, membership: bool | None = True) -> Mock:
    bot = Mock()
    bot.bot_id = bot_id
    bot.disabled = disabled
    bot.check_membership_tri.return_value = membership
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
