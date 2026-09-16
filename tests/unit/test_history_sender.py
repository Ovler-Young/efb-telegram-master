"""All relink output belongs to the original bot, including merged text."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram.error import BadRequest, RetryAfter

from efb_telegram_master.db import MsgLog
from efb_telegram_master.db_runtime import connection_scope
from efb_telegram_master.outbound import (
    HISTORY_REPLAY_KEY, HISTORY_SOURCE_PREFIX, OutboundQueue, OutboundQueueScheduler,
    QueueEnqueueError, QueueRequest, RequiredSenderUnavailableError, SenderSelection,
)
from tests.unit.test_history_replay import (
    Sender, finish_attempt, history as history, populate, prepare_runtime,
)
from tests.unit.test_outbound_queue_runtime_evidence import ControlledExecutor


MEDIA = [
    ('Photo', 'send_photo', 'photo'), ('Video', 'send_video', 'video'),
    ('Animation', 'send_animation', 'animation'), ('Document', 'send_document', 'document'),
    ('Audio', 'send_audio', 'audio'), ('Voice', 'send_voice', 'voice'),
    ('Sticker', 'send_sticker', 'sticker'), ('AnimatedSticker', 'send_sticker', 'sticker'),
    ('VideoSticker', 'send_sticker', 'sticker'),
]


class MediaSender(Sender):
    def __getattr__(self, operation):
        if operation not in {item[1] for item in MEDIA}:
            raise AttributeError(operation)

        def send(**kwargs):
            self.calls.append((operation, kwargs))
            return SimpleNamespace(message_id=501)
        return send


def attach_owner(manager, owner, *, disabled=False, membership=True, delay=0.0):
    auxiliary = SimpleNamespace(
        disabled=disabled, bot_id='aux-7', bot=owner,
        check_membership_tri=lambda _: membership, peek_delay=lambda _: delay,
        try_acquire_limits=lambda _: True,
    )
    manager.bot_pool = SimpleNamespace(
        get_bot_by_id=lambda value: auxiliary if str(value) == 'aux-7' else None,
        record_successful_auxiliary_send=Mock(),
    )
    return auxiliary


def prepare_owned(history, text=None, *, owner='aux-7', media_type='Video'):
    target = populate(history, [text])
    with connection_scope(history.db._managed_database):
        MsgLog.update(sender_bot_id=owner, media_type='Text' if text is not None else media_type).execute()
    operation, kwargs = history.binding._prepare_history_migration_call(target, -1002, 42)
    return target, operation, kwargs


def enqueue_owned(manager, target, operation, kwargs):
    return manager.enqueue_history_operation(
        source_key='slave chat', target_chat_id=-1002, operation=operation,
        args=(), kwargs=kwargs, history_entry_ids=[target.id],
    )


@pytest.mark.parametrize('media_type,operation,argument', MEDIA)
@pytest.mark.parametrize('owner_id', [None, 'aux-7', '123'])
def test_every_saved_media_id_stays_with_its_owner(history, media_type, operation, argument, owner_id):
    target, copy_operation, kwargs = prepare_owned(history, owner=owner_id, media_type=media_type)
    main = MediaSender(copy_error=BadRequest('Message to copy not found'))
    owner = MediaSender(copy_error=BadRequest('Message to copy not found')) if owner_id == 'aux-7' else main
    manager, executor, scheduler = prepare_runtime(history, main)
    manager.me = SimpleNamespace(id=123)  # Also support the main bot stored by its numeric ID.
    attach_owner(manager, owner)
    queue = manager._outbound_queue
    try:
        waiter = enqueue_owned(manager, target, copy_operation, kwargs)
        assert queue.heads()[0].required_sender_bot_id == (owner_id or '__main__')
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        assert waiter.result().message_id == 501
        assert [kind for kind, _ in owner.calls] == ['copy_message', operation]
        assert owner.calls[1][1][argument] == 'saved-video-file-id'
        assert owner.calls[1][1]['message_thread_id'] == 42
        if owner_id == 'aux-7':
            assert main.calls == []
        assert queue.connection.execute('SELECT COUNT(*) FROM outbound_queue').fetchone()[0] == 0
        with connection_scope(history.db._managed_database):
            assert MsgLog.get().sender_bot_id == owner_id
            assert MsgLog.get().file_id == 'saved-video-file-id'
    finally:
        queue.close()


@pytest.mark.parametrize('text', ['historical text', None])
@pytest.mark.parametrize('unavailable', ['missing', 'disabled', 'not_member'])
def test_unavailable_owner_retains_text_and_media_without_any_other_bot_call(history, text, unavailable):
    target, operation, kwargs = prepare_owned(history, text)
    main, owner = Sender(), Sender()
    manager, executor, scheduler = prepare_runtime(history, main)
    if unavailable != 'missing':
        attach_owner(manager, owner, disabled=unavailable == 'disabled', membership=unavailable != 'not_member')
    queue = manager._outbound_queue
    try:
        waiter = enqueue_owned(manager, target, operation, kwargs)
        scheduler.dispatch_once()
        assert not executor.submissions and main.calls == owner.calls == []
        assert isinstance(waiter.exception(), RequiredSenderUnavailableError)
        assert queue.connection.execute('SELECT delivery_hold FROM outbound_queue').fetchone()[0].startswith('history_failed:')
        with connection_scope(history.db._managed_database):
            assert MsgLog.get().sender_bot_id == 'aux-7'
        assert not scheduler.stopping
    finally:
        queue.close()


@pytest.mark.parametrize('text', ['text', None])
def test_owner_cooldown_waits_instead_of_using_idle_main(history, text):
    target, operation, kwargs = prepare_owned(history, text)
    manager, executor, scheduler = prepare_runtime(history, Sender())
    attach_owner(manager, Sender(), delay=10.0)
    try:
        enqueue_owned(manager, target, operation, kwargs)
        scheduler.dispatch_once()
        assert executor.submissions == []
        assert scheduler.next_deadline is not None
        assert not manager._outbound_queue.connection.execute('SELECT delivery_hold FROM outbound_queue').fetchone()[0]
    finally:
        manager._outbound_queue.close()


def test_text_owner_survives_sqlite_restart_and_rate_limit_retry(history, monkeypatch):
    target, operation, kwargs = prepare_owned(history, 'text from auxiliary')
    main, owner = Sender(), Sender()
    manager, executor, scheduler = prepare_runtime(history, main)
    attach_owner(manager, owner)
    queue = manager._outbound_queue
    enqueue_owned(manager, target, operation, kwargs)
    queue.close()
    queue = manager._outbound_queue = OutboundQueue(history.path)
    executor = ControlledExecutor()
    scheduler = manager._outbound_scheduler = OutboundQueueScheduler(queue, manager, executor, worker_count=2)
    clock = {'now': 100.0}
    monkeypatch.setattr('efb_telegram_master.outbound.time.monotonic', lambda: clock['now'])
    try:
        row = queue.heads()[0]
        assert row.operation == 'send_message' and row.required_sender_bot_id == 'aux-7'
        scheduler.dispatch_once()
        assert executor.submissions[0][1][-1].sender is owner
        executor.submissions[0][2].set_exception(RetryAfter(5))
        scheduler.harvest_completed()
        scheduler.dispatch_once()
        assert len(executor.submissions) == 1
        clock['now'] = 106.0
        scheduler.dispatch_once()
        assert executor.submissions[-1][1][-1].sender is owner
        finish_attempt(executor, scheduler)
        assert main.calls == []
        assert owner.calls == [('send_message', dict(chat_id=-1002, text='text from auxiliary',
            parse_mode='Markdown', disable_notification=True, message_thread_id=42))]
    finally:
        queue.close()


def test_legacy_unpinned_history_is_retained_not_assigned_to_a_random_bot(history):
    manager, executor, scheduler = prepare_runtime(history, Sender())
    queue = manager._outbound_queue
    try:
        queue.enqueue_many([QueueRequest('send_message', (), {
            'chat_id': -1002, 'text': 'old batch with unknown owners',
            '_slave_id': HISTORY_SOURCE_PREFIX + 'slave chat',
            HISTORY_REPLAY_KEY: {'entry_ids': [1, 2]},
        })], manager._queue_operation)
        scheduler.dispatch_once()
        assert not executor.submissions
        assert queue.connection.execute('SELECT delivery_hold FROM outbound_queue').fetchone()[0].startswith('history_failed:')
        with pytest.raises(QueueEnqueueError, match='original sender'):
            manager.enqueue_history_operation(source_key='slave chat', target_chat_id=-1002,
                operation='send_message', args=(), kwargs={'chat_id': -1002, 'text': 'unowned'}, history_entry_ids=[3])
    finally:
        queue.close()


def test_execution_boundary_rejects_wrong_sender_before_even_attempting_copy(history):
    target, operation, kwargs = prepare_owned(history)
    wrong = Sender()
    manager, executor, scheduler = prepare_runtime(history, wrong)
    queue = manager._outbound_queue
    try:
        enqueue_owned(manager, target, operation, kwargs)
        row = queue.heads()[0]
        args, raw = queue.decode_payload(row.payload)
        with pytest.raises(RequiredSenderUnavailableError, match='original bot'):
            manager.execute_queued_call(row, args, raw, SenderSelection(wrong, None))
        assert wrong.calls == []
    finally:
        queue.close()
