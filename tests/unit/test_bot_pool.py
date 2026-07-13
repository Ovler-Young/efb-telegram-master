from types import SimpleNamespace
from unittest.mock import Mock, patch

from efb_telegram_master.bot_pool import BotPool
from efb_telegram_master.rate_limiter import ReservationOutcome, SlotReservation


def _make_aux_bot(bot_id, *, disabled=False, membership=True, delay=0.0, username=None):
    aux_bot = Mock()
    aux_bot.bot_id = bot_id
    aux_bot.username = username or f"bot{bot_id}"
    aux_bot.disabled = disabled
    aux_bot.check_membership_tri.return_value = membership
    aux_bot.check_membership_sync.return_value = membership
    aux_bot.check_membership.return_value = bool(membership)
    aux_bot.peek_delay.return_value = delay
    aux_bot.reservation = SlotReservation(bot_id, str(bot_id), 100, 100.0)
    aux_bot.reserve_slot.return_value = ReservationOutcome(delay, aux_bot.reservation)
    aux_bot.get_chat_send_count.return_value = 0
    aux_bot.has_pending_probes.return_value = False
    return aux_bot


def _make_manager():
    return SimpleNamespace(admins=[1], send_message=Mock(), CHAT_LIMIT=20)


def test_acquire_send_slot_picks_lowest_delay_bot():
    bot_a = _make_aux_bot(1, delay=1.5)
    bot_b = _make_aux_bot(2, delay=0.25)
    pool = BotPool([bot_a, bot_b], _make_manager())

    selected = pool.acquire_send_slot(100, max_delay=2.0)

    assert selected == (bot_b, bot_b.reservation)
    bot_b.reserve_slot.assert_called_once_with(100)


def test_acquire_send_slot_rotates_equal_delay_bots_per_chat():
    bot_a = _make_aux_bot(1, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())

    first = pool.acquire_send_slot(100, max_delay=1.0)
    second = pool.acquire_send_slot(100, max_delay=1.0)
    third = pool.acquire_send_slot(100, max_delay=1.0)

    assert first == (bot_a, bot_a.reservation)
    assert second == (bot_b, bot_b.reservation)
    assert third == (bot_a, bot_a.reservation)
    bot_a.reserve_slot.assert_called_with(100)
    bot_b.reserve_slot.assert_called_once_with(100)


def test_acquire_send_slot_reuses_affinity_bot_below_half_capacity():
    bot_a = _make_aux_bot(1, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())

    first = pool.acquire_send_slot(100, max_delay=1.0, affinity_key=(100, 10))
    second = pool.acquire_send_slot(100, max_delay=1.0, affinity_key=(100, 10))

    assert first == (bot_a, bot_a.reservation)
    assert second == (bot_a, bot_a.reservation)
    assert bot_a.reserve_slot.call_count == 2
    bot_b.reserve_slot.assert_not_called()


def test_acquire_send_slot_keeps_affinity_per_slave_id():
    bot_a = _make_aux_bot(1, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())

    first = pool.acquire_send_slot(100, max_delay=1.0, affinity_key="slave.chat")
    second = pool.acquire_send_slot(200, max_delay=1.0, affinity_key="slave.chat")

    assert first == (bot_a, bot_a.reservation)
    assert second == (bot_a, bot_a.reservation)
    assert pool._affinity_bot_by_key["slave.chat"] == 1


def test_forget_affinity_allows_next_selection_to_rotate():
    bot_a = _make_aux_bot(1, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())

    first = pool.acquire_send_slot(100, max_delay=1.0, affinity_key="slave.chat")
    pool.forget_affinity("slave.chat")
    second = pool.acquire_send_slot(100, max_delay=1.0, affinity_key="slave.chat")

    assert first == (bot_a, bot_a.reservation)
    assert second == (bot_b, bot_b.reservation)
    assert pool._affinity_bot_by_key["slave.chat"] == 2


def test_acquire_send_slot_switches_affinity_bot_at_half_capacity():
    bot_a = _make_aux_bot(1, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())

    first = pool.acquire_send_slot(100, max_delay=1.0, affinity_key=(100, 10))
    bot_a.get_chat_send_count.return_value = 10
    second = pool.acquire_send_slot(100, max_delay=1.0, affinity_key=(100, 10))

    assert first == (bot_a, bot_a.reservation)
    assert second == (bot_b, bot_b.reservation)
    assert pool._affinity_bot_by_key[(100, 10)] == 2
    bot_b.reserve_slot.assert_called_once_with(100)


def test_acquire_send_slot_keeps_affinity_per_topic():
    bot_a = _make_aux_bot(1, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())

    first_topic = pool.acquire_send_slot(100, max_delay=1.0, affinity_key=(100, 10))
    second_topic = pool.acquire_send_slot(100, max_delay=1.0, affinity_key=(100, 20))
    first_topic_again = pool.acquire_send_slot(100, max_delay=1.0, affinity_key=(100, 10))

    assert first_topic == (bot_a, bot_a.reservation)
    assert second_topic == (bot_b, bot_b.reservation)
    assert first_topic_again == (bot_a, bot_a.reservation)


def test_acquire_send_slot_falls_back_when_affinity_bot_unavailable():
    bot_a = _make_aux_bot(1, disabled=True, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())
    pool._affinity_bot_by_key[(100, 10)] = 1

    selected = pool.acquire_send_slot(100, max_delay=1.0, affinity_key=(100, 10))

    assert selected == (bot_b, bot_b.reservation)
    assert pool._affinity_bot_by_key[(100, 10)] == 2
    bot_a.reserve_slot.assert_not_called()
    bot_b.reserve_slot.assert_called_once_with(100)


def test_acquire_send_slot_falls_back_when_affinity_bot_is_not_member():
    bot_a = _make_aux_bot(1, membership=False, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())
    pool._affinity_bot_by_key[(100, 10)] = 1

    selected = pool.acquire_send_slot(100, max_delay=1.0, affinity_key=(100, 10))

    assert selected == (bot_b, bot_b.reservation)
    assert pool._affinity_bot_by_key[(100, 10)] == 2
    bot_a.reserve_slot.assert_not_called()
    bot_b.reserve_slot.assert_called_once_with(100)


def test_acquire_send_slot_falls_back_when_affinity_bot_disabled_by_skip():
    bot_a = _make_aux_bot(1, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())
    pool._affinity_bot_by_key[(100, 10)] = 1

    selected = pool.acquire_send_slot(
        100,
        max_delay=1.0,
        affinity_key=(100, 10),
        skip_bot=lambda bot: bot.bot_id == 1,
    )

    assert selected == (bot_b, bot_b.reservation)
    assert pool._affinity_bot_by_key[(100, 10)] == 2
    bot_a.reserve_slot.assert_not_called()
    bot_b.reserve_slot.assert_called_once_with(100)


def test_acquire_send_slot_falls_back_when_affinity_bot_has_local_delay():
    bot_a = _make_aux_bot(1, delay=1.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())
    pool._affinity_bot_by_key[(100, 10)] = 1

    selected = pool.acquire_send_slot(100, max_delay=2.0, affinity_key=(100, 10))

    assert selected == (bot_b, bot_b.reservation)
    assert pool._affinity_bot_by_key[(100, 10)] == 2
    bot_a.reserve_slot.assert_not_called()
    bot_b.reserve_slot.assert_called_once_with(100)


def test_acquire_send_slot_skips_disabled_bots_before_selecting():
    bot_a = _make_aux_bot(1, delay=0.0)
    bot_b = _make_aux_bot(2, delay=0.0)
    pool = BotPool([bot_a, bot_b], _make_manager())

    selected = pool.acquire_send_slot(100, max_delay=1.0, skip_bot=lambda bot: bot.bot_id == 1)

    assert selected == (bot_b, bot_b.reservation)
    bot_a.reserve_slot.assert_not_called()
    bot_b.reserve_slot.assert_called_once_with(100)


def test_acquire_send_slot_skips_disabled_and_respects_max_delay():
    disabled_bot = _make_aux_bot(1, disabled=True, delay=0.0)
    slow_bot = _make_aux_bot(2, delay=5.0)
    pool = BotPool([disabled_bot, slow_bot], _make_manager())

    assert pool.acquire_send_slot(100, max_delay=1.0) is None
    disabled_bot.reserve_slot.assert_not_called()
    slow_bot.reserve_slot.assert_not_called()


def test_explain_send_slot_unavailable_returns_bounded_reason_labels():
    assert BotPool([], _make_manager()).explain_send_slot_unavailable(100) == "not_configured"

    non_member = _make_aux_bot(1, membership=False)
    pool = BotPool([non_member], _make_manager())
    assert pool.explain_send_slot_unavailable(100) == "no_aux_member"

    unknown = _make_aux_bot(2, membership=None)
    pool = BotPool([unknown], _make_manager())
    assert pool.explain_send_slot_unavailable(100) == "membership_unknown"

    delayed = _make_aux_bot(3, membership=True, delay=1.0)
    pool = BotPool([delayed], _make_manager())
    assert pool.explain_send_slot_unavailable(100) == "local_rate_limit"

    skipped = _make_aux_bot(4, membership=True, delay=0.0)
    pool = BotPool([skipped], _make_manager())
    assert pool.explain_send_slot_unavailable(100, skip_bot=lambda bot: True) == "bot_chat_cooldown"


def test_membership_updates_are_forwarded_to_bots():
    aux_bot = _make_aux_bot(10)
    pool = BotPool([aux_bot], _make_manager())

    pool.on_bots_joined_chat([10], 1000)
    pool.on_bot_left_chat(10, 1000)

    aux_bot.update_membership.assert_any_call(1000, True)
    aux_bot.update_membership.assert_any_call(1000, False)


def test_notify_admin_only_fires_once_per_chat():
    aux_bot = _make_aux_bot(10)
    pool = BotPool([aux_bot], _make_manager())

    started_targets = []

    class ImmediateThread:
        def __init__(self, target, args, daemon, name):
            self.target = target
            self.args = args

        def start(self):
            started_targets.append(self.args)
            self.target(*self.args)

    with patch("efb_telegram_master.bot_pool.threading.Thread", ImmediateThread), \
         patch.object(pool, "_send_admin_notification") as notify:
        pool._maybe_notify_admin(100, [aux_bot])
        pool._maybe_notify_admin(100, [aux_bot])

    assert notify.call_count == 1
    assert len(started_targets) == 1


def test_get_pool_stats_reports_disabled_and_cache_size():
    aux_bot = _make_aux_bot(10, username="aux")
    aux_bot._membership_cache = {1: (True, 0.0), 2: (False, 1.0)}
    pool = BotPool([aux_bot], _make_manager())

    stats = pool.get_pool_stats()

    assert stats["total_bots"] == 1
    assert stats["active_bots"] == 1
    assert stats["bots"][0]["bot_id"] == 10
    assert stats["bots"][0]["membership_cache_size"] == 2
