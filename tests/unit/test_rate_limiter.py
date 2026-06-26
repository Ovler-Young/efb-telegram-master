"""Tests for the shared SlidingWindowRateLimiter."""

from collections import deque
from unittest.mock import patch

from efb_telegram_master.rate_limiter import SlidingWindowRateLimiter


def _make_limiter(**kwargs):
    defaults = dict(global_limit=5, global_window=1.0, chat_limit=3, chat_window=10.0, safety_margin=0)
    defaults.update(kwargs)
    return SlidingWindowRateLimiter(**defaults)


# Basic delay / reserve


def test_first_request_has_zero_delay():
    limiter = _make_limiter()
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        assert limiter.peek_delay(1) == 0.0
        assert limiter.reserve_slot(1) == 0.0


def test_peek_does_not_consume_slot():
    limiter = _make_limiter(chat_limit=2)
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        assert limiter.peek_delay(1) == 0.0
        assert limiter.peek_delay(1) == 0.0  # still 0; nothing consumed
        limiter.reserve_slot(1)
        limiter.reserve_slot(1)
        assert limiter.peek_delay(1) > 0.0  # now full


def test_chat_limit_triggers_delay():
    limiter = _make_limiter(chat_limit=2, chat_window=10.0)
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        assert limiter.reserve_slot(1) == 0.0
        assert limiter.reserve_slot(1) == 0.0
        delay = limiter.peek_delay(1)
        assert delay > 0.0  # third request must wait


def test_global_limit_triggers_delay():
    limiter = _make_limiter(global_limit=2, global_window=1.0, chat_limit=100)
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        assert limiter.reserve_slot(1) == 0.0
        assert limiter.reserve_slot(2) == 0.0  # different chat
        delay = limiter.peek_delay(3)
        assert delay > 0.0  # global limit hit


def test_different_chats_are_independent():
    limiter = _make_limiter(chat_limit=1, chat_window=10.0, global_limit=100)
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        assert limiter.reserve_slot(1) == 0.0
        assert limiter.peek_delay(1) > 0.0  # chat 1 full
        assert limiter.peek_delay(2) == 0.0  # chat 2 still free


# Safety margin


def test_safety_margin_reduces_effective_limit():
    limiter = _make_limiter(chat_limit=3, safety_margin=1)
    # effective chat limit = 3 - 1 = 2
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        assert limiter.reserve_slot(1) == 0.0
        assert limiter.reserve_slot(1) == 0.0
        assert limiter.peek_delay(1) > 0.0  # 2 consumed, margin=1; full


# Timestamp cleanup


def test_old_timestamps_are_cleaned_up():
    limiter = _make_limiter(chat_limit=1, chat_window=5.0, global_limit=100)

    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        limiter.reserve_slot(1)
        assert limiter.peek_delay(1) > 0.0

    # Advance time past the window
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=106.0):
        assert limiter.peek_delay(1) == 0.0  # old timestamp cleaned up

    with patch("efb_telegram_master.rate_limiter.time.time", return_value=106.0):
        limiter.reserve_slot(2)
        assert 1 not in limiter._chat_timestamps


def test_release_slot_removes_latest_reservation():
    limiter = _make_limiter()
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        limiter.reserve_slot(1)
        limiter.release_slot(1)
        assert limiter.get_counts(1) == (0, 0)
        assert 1 not in limiter._chat_timestamps


def test_release_slot_is_idempotent_without_reservation():
    limiter = _make_limiter()
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        limiter.release_slot(1)
        assert limiter.get_counts(1) == (0, 0)
        assert 1 not in limiter._chat_timestamps

        limiter._chat_timestamps[1] = deque()
        limiter.release_slot(1)
        assert limiter.get_counts(1) == (0, 0)
        assert 1 not in limiter._chat_timestamps


def test_peek_and_counts_do_not_create_empty_chat_key():
    limiter = _make_limiter()
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        assert limiter.peek_delay(1) == 0.0
        assert limiter.get_counts(1) == (0, 0)
        assert 1 not in limiter._chat_timestamps


# get_counts


def test_get_counts_returns_correct_values():
    limiter = _make_limiter()
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        limiter.reserve_slot(1)
        limiter.reserve_slot(1)
        limiter.reserve_slot(2)

    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        chat_count, global_count = limiter.get_counts(1)
        assert chat_count == 2
        assert global_count == 3


def test_chat_count_snapshot_returns_active_counts_and_effective_limit():
    limiter = _make_limiter(chat_limit=3, safety_margin=1)
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        limiter.reserve_slot(1)
        limiter.reserve_slot(1)
        limiter.reserve_slot(2)

        chat_counts, effective_limit = limiter.get_chat_count_snapshot()

    assert chat_counts == {1: 2, 2: 1}
    assert effective_limit == 2


def test_chat_count_snapshot_cleans_up_expired_chats():
    limiter = _make_limiter(chat_limit=3, chat_window=10.0)
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        limiter.reserve_slot(1)

    with patch("efb_telegram_master.rate_limiter.time.time", return_value=111.0):
        chat_counts, effective_limit = limiter.get_chat_count_snapshot()

    assert chat_counts == {}
    assert effective_limit == 3


def test_reserved_slot_count_uses_global_window_cleanup():
    limiter = _make_limiter(global_window=5.0, chat_window=100.0)
    with patch("efb_telegram_master.rate_limiter.time.time", return_value=100.0):
        limiter.reserve_slot(1)
        limiter.reserve_slot(2)
        assert limiter.get_reserved_slot_count() == 2

    with patch("efb_telegram_master.rate_limiter.time.time", return_value=106.0):
        assert limiter.get_reserved_slot_count() == 0


# Thread safety (smoke)


def test_concurrent_reserves_do_not_crash():
    """Smoke test: many threads reserving simultaneously should not raise."""
    import threading

    limiter = _make_limiter(global_limit=1000, chat_limit=1000)
    errors: list = []

    def worker():
        try:
            for _ in range(50):
                limiter.reserve_slot(1)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
