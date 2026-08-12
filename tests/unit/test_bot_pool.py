from unittest.mock import Mock

from efb_telegram_master.bot_pool import BotPool


def _bot(bot_id: int, *, disabled: bool = False, membership: bool | None = True) -> Mock:
    bot = Mock()
    bot.bot_id = bot_id
    bot.disabled = disabled
    bot.check_membership_tri.return_value = membership
    return bot


def test_candidate_bots_exclude_disabled_and_preserve_unknown_membership() -> None:
    disabled = _bot(1, disabled=True)
    unknown = _bot(20, membership=None)
    member = _bot(3)

    candidates = BotPool([disabled, unknown, member]).candidate_bots(100)

    assert candidates == [(unknown, None), (member, True)]
    disabled.check_membership_tri.assert_not_called()


def test_successful_main_and_null_slave_do_not_change_affinity() -> None:
    bot = _bot(10)
    pool = BotPool([bot])
    pool.record_successful_auxiliary_send("slave-a", 10)

    pool.record_successful_auxiliary_send(None, 10)

    assert pool.preferred_sender("slave-a") is bot
    assert pool.preferred_sender(None) is None


def test_disabling_bot_removes_every_affinity_to_that_bot() -> None:
    first = _bot(10)
    second = _bot(20)
    pool = BotPool([first, second])
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)
    pool.record_successful_auxiliary_send("slave-c", 20)

    pool.disable_bot(10)

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-b") is None
    assert pool.preferred_sender("slave-c") is second
