from __future__ import annotations

import threading

import pytest
from telegram.error import RetryAfter

import efb_telegram_master.sender_policy as sender_policy_module
from efb_telegram_master.outbound_types import SenderSelection
from tests.unit.sender_policy_support import auxiliary, call, policy


def test_retry_after_cooldown_is_scoped_to_exact_sender_and_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    auxiliary_bot = auxiliary(10)
    sender_policy, _pool, _main_bot, _limiter = policy(auxiliary_bot)
    sender = SenderSelection(auxiliary_bot.bot, "10")
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: 1_000.0)

    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(20), sender)

    delayed = sender_policy.select(call(required_sender_bot_id="10", chat_id=100), now=1_000.0)
    other_chat = sender_policy.select(call(required_sender_bot_id="10", chat_id=101), now=1_000.0)

    assert delayed.selection is None
    assert delayed.retry_at == 1_020.0
    assert other_chat.selection == sender


def test_retry_after_keeps_the_latest_deadline_for_one_sender_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    auxiliary_bot = auxiliary(10)
    sender_policy, _pool, _main_bot, _limiter = policy(auxiliary_bot)
    sender = SenderSelection(auxiliary_bot.bot, "10")
    clock = [1_000.0]
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: clock[0])

    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(20), sender)
    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(5), sender)
    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(1_000), sender)

    assert sender_policy.select(call(required_sender_bot_id="10", chat_id=100), now=1_000.0).retry_at == 2_000.0
    assert len(sender_policy._cooldown_expiry_heap) == 1
    assert len(sender_policy._cooldown_max_heaps["auxiliary"]) == 1


def test_expired_cooldowns_are_reclaimed_before_new_retry_after_state(monkeypatch: pytest.MonkeyPatch) -> None:
    auxiliary_bot = auxiliary(10)
    sender_policy, _pool, _main_bot, _limiter = policy(auxiliary_bot)
    sender = SenderSelection(auxiliary_bot.bot, "10")
    clock = [1_000.0]
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: clock[0])

    for chat_id in range(2_000):
        sender_policy.record_retry_after(call(chat_id=chat_id), RetryAfter(1), sender)

    clock[0] = 1_002.0
    sender_policy.record_retry_after(call(chat_id=2_000), RetryAfter(5), sender)

    assert len(sender_policy._cooldowns) == 1
    assert len(sender_policy._cooldown_expiry_heap) == 1
    assert len(sender_policy._cooldown_max_heaps["auxiliary"]) == 1
    assert sender_policy.cooldown_snapshot() == {"main": 0.0, "auxiliary": 5.0}


def test_cooldown_snapshot_reports_exact_remaining_maximum_after_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    auxiliary_bot = auxiliary(10)
    sender_policy, _pool, main_bot, _limiter = policy(auxiliary_bot)
    auxiliary_sender = SenderSelection(auxiliary_bot.bot, "10")
    main_sender = SenderSelection(main_bot, None)
    clock = [1_000.0]
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: clock[0])

    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(5), auxiliary_sender)
    sender_policy.record_retry_after(call(chat_id=101), RetryAfter(12), auxiliary_sender)
    sender_policy.record_retry_after(call(chat_id=100), RetryAfter(20), main_sender)

    assert sender_policy.cooldown_snapshot() == {"main": 20.0, "auxiliary": 12.0}

    clock[0] = 1_006.0

    assert sender_policy.cooldown_snapshot() == {"main": 14.0, "auxiliary": 6.0}


def test_cooldown_snapshot_is_safe_while_retry_after_updates_arrive(monkeypatch: pytest.MonkeyPatch) -> None:
    auxiliary_bot = auxiliary(10)
    sender_policy, _pool, _main_bot, _limiter = policy(auxiliary_bot)
    sender = SenderSelection(auxiliary_bot.bot, "10")
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: 1_000.0)
    start = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        start.wait()
        for chat_id in range(2_000):
            sender_policy.record_retry_after(call(chat_id=chat_id), RetryAfter(1), sender)

    thread = threading.Thread(target=writer)
    thread.start()
    start.set()
    try:
        while thread.is_alive():
            try:
                snapshot = sender_policy.cooldown_snapshot()
                assert snapshot["auxiliary"] >= 0.0
            except BaseException as error:
                errors.append(error)
    finally:
        thread.join()

    assert errors == []


def test_send_failure_records_the_triggering_slave_and_sender_for_membership_confirmation() -> None:
    first = auxiliary(10)
    second = auxiliary(20)
    sender_policy, pool, _main_bot, _limiter = policy(first, second)
    assert pool is not None
    pool.record_successful_auxiliary_send("slave-a", 10)
    pool.record_successful_auxiliary_send("slave-b", 10)

    sender_policy.record_send_failure(call(slave_id="slave-a", chat_id=100), SenderSelection(first.bot, "10"))
    first._membership_changed_callback(first, 100, False)

    assert pool.preferred_sender("slave-a") is None
    assert pool.preferred_sender("slave-b") is first


def test_retry_after_cooldown_leaves_another_sender_for_the_same_chat_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    first = auxiliary(10)
    second = auxiliary(20)
    sender_policy, _pool, _main_bot, _limiter = policy(first, second, main_delay=30.0)
    monkeypatch.setattr(sender_policy_module.time, "monotonic", lambda: 1_000.0)
    queued_call = call()

    sender_policy.record_retry_after(queued_call, RetryAfter(20), SenderSelection(first.bot, "10"))
    decision = sender_policy.select(queued_call, now=1_000.0)

    assert decision.selection is not None
    assert decision.selection.sender_bot_id == "20"
