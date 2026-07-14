"""Tests for the outbound sender limiter contract."""

from __future__ import annotations

from dataclasses import dataclass

from efb_telegram_master.rate_limiter import SlidingWindowRateLimiter


@dataclass
class MonotonicClock:
    value: float = 0.0

    def now(self) -> float:
        return self.value


def _make_limiter(clock: MonotonicClock) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(clock=clock.now)


def test_global_capacity_is_28_acquisitions_per_bot_per_second() -> None:
    clock = MonotonicClock()
    limiter = _make_limiter(clock)

    for _ in range(28):
        assert limiter.try_acquire_global()

    assert limiter.global_delay() > 0.0
    assert not limiter.try_acquire_global()

    clock.value = 1.001
    assert limiter.try_acquire_global()


def test_chat_capacity_is_18_acquisitions_per_bot_chat_per_60_seconds() -> None:
    clock = MonotonicClock()
    limiter = _make_limiter(clock)

    for _ in range(18):
        assert limiter.try_acquire_chat(100)

    assert limiter.chat_delay(100) > 0.0
    assert not limiter.try_acquire_chat(100)
    assert limiter.try_acquire_chat(200)


def test_each_limiter_instance_has_independent_bot_global_key() -> None:
    clock = MonotonicClock()
    first_bot = _make_limiter(clock)
    second_bot = _make_limiter(clock)

    for _ in range(28):
        assert first_bot.try_acquire_global()

    assert first_bot.global_delay() > 0.0
    assert second_bot.global_delay() == 0.0
    assert second_bot.try_acquire_global()


def test_acquisition_uses_monotonic_clock_not_wall_clock(monkeypatch) -> None:
    clock = MonotonicClock()
    limiter = _make_limiter(clock)

    monkeypatch.setattr(
        "efb_telegram_master.rate_limiter.time.time",
        lambda: (_ for _ in ()).throw(AssertionError("wall clock used")),
    )
    assert limiter.try_acquire_global()
    assert limiter.global_delay() == 0.0


def test_global_acquisition_precedes_chat_and_is_not_returned_after_chat_failure() -> None:
    clock = MonotonicClock()
    limiter = _make_limiter(clock)

    for _ in range(18):
        assert limiter.try_acquire_chat(100)

    assert not limiter.try_acquire(100)
    assert limiter.get_counts(100) == (18, 1)


def test_acquire_runs_global_before_bot_chat() -> None:
    clock = MonotonicClock()
    limiter = _make_limiter(clock)
    calls: list[str] = []
    global_acquire = limiter.try_acquire_global
    chat_acquire = limiter.try_acquire_chat

    def acquire_global() -> bool:
        calls.append("global")
        return global_acquire()

    def acquire_chat(chat_id: int) -> bool:
        calls.append("chat")
        return chat_acquire(chat_id)

    setattr(limiter, "try_acquire_global", acquire_global)
    setattr(limiter, "try_acquire_chat", acquire_chat)

    assert limiter.try_acquire(100)
    assert calls == ["global", "chat"]


def test_failed_or_cancelled_send_has_no_limiter_release_path() -> None:
    clock = MonotonicClock()
    limiter = _make_limiter(clock)

    assert limiter.try_acquire(100)
    assert limiter.get_counts(100) == (1, 1)
    assert not hasattr(limiter, "release_slot")
    assert limiter.get_counts(100) == (1, 1)


def test_limiter_state_resets_on_process_restart() -> None:
    clock = MonotonicClock()
    first_process = _make_limiter(clock)

    for _ in range(28):
        assert first_process.try_acquire_global()

    restarted_process = _make_limiter(clock)
    assert restarted_process.global_delay() == 0.0
    assert restarted_process.try_acquire_global()
