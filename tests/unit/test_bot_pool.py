from types import SimpleNamespace
from unittest.mock import Mock, patch

from efb_telegram_master.bot_pool import BotPool


def _make_aux_bot(bot_id, *, disabled=False, membership=True, delay=0.0, username=None):
    aux_bot = Mock()
    aux_bot.bot_id = bot_id
    aux_bot.username = username or f"bot{bot_id}"
    aux_bot.disabled = disabled
    aux_bot.check_membership_tri.return_value = membership
    aux_bot.check_membership_sync.return_value = membership
    aux_bot.check_membership.return_value = bool(membership)
    aux_bot.peek_delay.return_value = delay
    aux_bot.reserve_slot.return_value = delay
    aux_bot.has_pending_probes.return_value = False
    return aux_bot


def test_acquire_send_slot_picks_lowest_delay_bot():
    bot_a = _make_aux_bot(1, delay=1.5)
    bot_b = _make_aux_bot(2, delay=0.25)
    pool = BotPool([bot_a, bot_b], SimpleNamespace(admins=[1], send_message=Mock()))

    selected = pool.acquire_send_slot(100, max_delay=2.0)

    assert selected == (bot_b, 0.25)
    bot_b.reserve_slot.assert_called_once_with(100)


def test_acquire_send_slot_skips_disabled_and_respects_max_delay():
    disabled_bot = _make_aux_bot(1, disabled=True, delay=0.0)
    slow_bot = _make_aux_bot(2, delay=5.0)
    pool = BotPool([disabled_bot, slow_bot], SimpleNamespace(admins=[1], send_message=Mock()))

    assert pool.acquire_send_slot(100, max_delay=1.0) is None
    disabled_bot.reserve_slot.assert_not_called()
    slow_bot.reserve_slot.assert_not_called()


def test_send_blocking_waits_until_slot_is_available():
    aux_bot = _make_aux_bot(1, delay=1.0)
    aux_bot.peek_delay.side_effect = [1.0, 0.0]
    manager = SimpleNamespace(admins=[1], send_message=Mock())
    pool = BotPool([aux_bot], manager)

    time_values = iter([0.0, 0.1, 0.2, 0.3])
    with patch("efb_telegram_master.bot_pool.time.time", side_effect=lambda: next(time_values)), \
         patch("efb_telegram_master.bot_pool.time.sleep") as sleep:
        selected = pool.send_blocking(100, timeout=1.0)

    assert selected is aux_bot
    assert sleep.called
    aux_bot.reserve_slot.assert_called_once_with(100)


def test_membership_updates_are_forwarded_to_bots():
    aux_bot = _make_aux_bot(10)
    pool = BotPool([aux_bot], SimpleNamespace(admins=[1], send_message=Mock()))

    pool.on_bots_joined_chat([10], 1000)
    pool.on_bot_left_chat(10, 1000)

    aux_bot.update_membership.assert_any_call(1000, True)
    aux_bot.update_membership.assert_any_call(1000, False)


def test_notify_admin_only_fires_once_per_chat():
    aux_bot = _make_aux_bot(10)
    pool = BotPool([aux_bot], SimpleNamespace(admins=[1], send_message=Mock()))

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
    pool = BotPool([aux_bot], SimpleNamespace(admins=[1], send_message=Mock()))

    stats = pool.get_pool_stats()

    assert stats["total_bots"] == 1
    assert stats["active_bots"] == 1
    assert stats["bots"][0]["bot_id"] == 10
    assert stats["bots"][0]["membership_cache_size"] == 2
