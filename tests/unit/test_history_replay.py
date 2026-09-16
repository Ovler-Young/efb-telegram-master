"""History replay contracts: real SQLite state, bounded batching, sender-owned media."""

from concurrent.futures import Future
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from telegram.error import BadRequest, TimedOut

from efb_telegram_master import db as db_module
from efb_telegram_master.chat_binding import ChatBindingManager
from efb_telegram_master.db import DatabaseManager, HistoryMigrationEntry, MsgLog, database
from efb_telegram_master.db_runtime import connection_scope
from efb_telegram_master.outbound import HISTORY_REPLAY_KEY, OutboundQueue, OutboundQueueScheduler
from tests.unit.test_outbound_queue_runtime_evidence import ControlledExecutor, manager_adapter


@pytest.fixture
def history(tmp_path, monkeypatch):
    previous = database.obj
    monkeypatch.setattr(db_module.utils, 'get_data_path', lambda _: tmp_path)
    db = DatabaseManager(SimpleNamespace(channel_id='history-test', config={}))
    binding = ChatBindingManager.__new__(ChatBindingManager)
    binding.db = db
    binding.chat_manager = Mock()
    binding.logger = Mock()
    calls = []

    def enqueue(**kwargs):
        calls.append(kwargs)
        waiter = Future()
        waiter.set_result(SimpleNamespace(message_id=len(calls)))
        return waiter

    binding.bot = SimpleNamespace(enqueue_history_operation=enqueue)
    yield SimpleNamespace(db=db, binding=binding, calls=calls, path=tmp_path)
    db.stop_worker()
    database.initialize(previous)


def populate(history, messages):
    """None means a video between runs of formatted text."""
    entries = []
    with connection_scope(history.db._managed_database):
        for index, text in enumerate(messages):
            media_type = 'Video' if text is None else 'Text'
            key = f'-1001.{index + 1}'
            MsgLog.create(
                master_msg_id=key, slave_message_id=f'source-{index}', text=text or 'video caption',
                slave_origin_uid='slave chat', slave_member_uid='slave user', media_type=media_type,
                msg_type='Video' if text is None else 'Text', sent_to='blueset.telegram',
                file_id='saved-video-file-id' if text is None else None,
                time=datetime(2026, 1, 1) + timedelta(seconds=index),
            )
            entries.append(dict(
                slave_chat_id='slave chat', target_chat_id='-1002', message_thread_id='42',
                source_master_msg_id=key, formatted_text=text, media_type=media_type,
                source_time=datetime(2026, 1, 1) + timedelta(seconds=index), position=index,
            ))
    history.db.replace_history_migration_entries('slave chat', -1002, 42, entries)
    return history.db.get_next_history_migration_target()


def test_consecutive_text_is_combined_but_not_across_video(history):
    target = populate(history, ['first\n', 'second\n', None, 'third\n', 'fourth\n'])
    assert history.binding._process_history_migration_target(target)
    assert [call['operation'] for call in history.calls] == ['send_message', 'copy_message', 'send_message']
    assert history.calls[0]['kwargs']['text'] == 'first\nsecond\n'
    assert history.calls[2]['kwargs']['text'] == 'third\nfourth\n'
    assert [len(call['history_entry_ids']) for call in history.calls] == [2, 1, 2]
    assert all(call['kwargs']['message_thread_id'] == 42 for call in history.calls)
    assert history.calls[1]['kwargs'][HISTORY_REPLAY_KEY]['fallback_operation'] == 'send_video'
    with connection_scope(history.db._managed_database):
        assert MsgLog.select().count() == 5
        assert HistoryMigrationEntry.select().count() == 0


def test_text_batch_crosses_sqlite_page_boundaries_without_loading_entire_history(history, monkeypatch):
    texts = [f'line {index:02d}\n' for index in range(75)]
    target = populate(history, texts)
    original = history.db.get_history_migration_entries
    page_sizes = []

    def read_page(*args, **kwargs):
        assert kwargs['limit'] == 32
        page = original(*args, **kwargs)
        page_sizes.append(len(page))
        return page

    monkeypatch.setattr(history.db, 'get_history_migration_entries', read_page)
    assert history.binding._process_history_migration_target(target)
    assert page_sizes == [32, 32, 11]
    assert len(history.calls) == 1
    assert history.calls[0]['kwargs']['text'] == ''.join(texts)
    assert len(history.calls[0]['history_entry_ids']) == 75


def test_text_batches_split_at_original_sender_changes_even_across_pages(history):
    texts = [f'line {index:02d}\n' for index in range(70)]
    target = populate(history, texts)
    owners = [None] * 33 + ['aux-7'] * 34 + ['aux-8'] * 2 + [None]
    with connection_scope(history.db._managed_database):
        for index, owner in enumerate(owners):
            MsgLog.update(sender_bot_id=owner).where(MsgLog.master_msg_id == f'-1001.{index + 1}').execute()
    assert history.binding._process_history_migration_target(target)
    assert [call['kwargs']['text'] for call in history.calls] == [
        ''.join(texts[:33]), ''.join(texts[33:67]), ''.join(texts[67:69]), texts[69],
    ]
    assert [call['kwargs']['_required_sender_bot_id'] for call in history.calls] == [
        '__main__', 'aux-7', 'aux-8', '__main__',
    ]
    with connection_scope(history.db._managed_database):
        assert MsgLog.select().count() == 70
        assert HistoryMigrationEntry.select().count() == 0


@pytest.mark.parametrize('text', ['hello', None])
def test_missing_source_log_retains_replay_instead_of_guessing_sender(history, text):
    target = populate(history, [text])
    with connection_scope(history.db._managed_database):
        MsgLog.delete().execute()  # Simulate an incomplete source, not a runtime cleanup.
    assert not history.binding._process_history_migration_target(target)
    assert history.calls == []
    with connection_scope(history.db._managed_database):
        assert HistoryMigrationEntry.select().count() == 1


def test_original_4076_character_boundary_is_preserved(history):
    target = populate(history, ['a' * 2038, 'b' * 2038, 'c'])
    history.binding._process_history_migration_target(target)
    assert [len(call['kwargs']['text']) for call in history.calls] == [4076, 1]


def test_failed_batch_enqueue_preserves_all_source_entries(history):
    target = populate(history, ['a', 'b'])
    history.binding.bot.enqueue_history_operation = Mock(side_effect=RuntimeError('disk unavailable'))
    assert not history.binding._process_history_migration_target(target)
    with connection_scope(history.db._managed_database):
        assert HistoryMigrationEntry.select().count() == 2
        assert MsgLog.select().count() == 2


class Sender:
    def __init__(self, copy_error=None, media_error=None):
        self.copy_error = copy_error
        self.media_error = media_error
        self.calls = []

    def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        self.calls.append(('copy_message', dict(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, **kwargs)))
        if self.copy_error:
            raise self.copy_error
        return SimpleNamespace(message_id=500)

    def send_video(self, chat_id, video, **kwargs):
        self.calls.append(('send_video', dict(chat_id=chat_id, video=video, **kwargs)))
        assert isinstance(video, str)  # A Telegram file ID, never a downloaded file.
        if self.media_error:
            raise self.media_error
        return SimpleNamespace(message_id=501)

    def send_message(self, chat_id, text, **kwargs):
        self.calls.append(('send_message', dict(chat_id=chat_id, text=text, **kwargs)))
        return SimpleNamespace(message_id=502)


def prepare_runtime(history, sender):
    manager = manager_adapter()
    manager._bot = sender
    manager.logger = Mock()
    manager._outbound_queue = OutboundQueue(history.path)
    executor = ControlledExecutor()
    scheduler = OutboundQueueScheduler(manager._outbound_queue, manager, executor, worker_count=2)
    manager._outbound_scheduler = scheduler
    return manager, executor, scheduler


def finish_attempt(executor, scheduler):
    function, args, future = executor.submissions[-1]
    try:
        result = function(*args)
    except BaseException as error:
        future.set_exception(error)
    else:
        future.set_result(result)
    scheduler.harvest_completed()


@pytest.mark.parametrize('auxiliary', [False, True])
def test_video_copy_missing_uses_saved_file_id_with_owning_sender(history, auxiliary):
    target = populate(history, [None])
    if auxiliary:
        with connection_scope(history.db._managed_database):
            MsgLog.update(sender_bot_id='aux-7').execute()
    sender = Sender(copy_error=BadRequest('Message to copy not found'))
    manager, executor, scheduler = prepare_runtime(history, sender)
    if auxiliary:
        manager._bot = Sender(copy_error=AssertionError('must not use main bot with an auxiliary file ID'))
        aux = SimpleNamespace(disabled=False, bot_id='aux-7', bot=sender,
                              check_membership_tri=lambda _: True, peek_delay=lambda _: 0,
                              try_acquire_limits=lambda _: True)
        manager.bot_pool = SimpleNamespace(get_bot_by_id=lambda _: aux, record_successful_auxiliary_send=Mock())
    operation, kwargs = history.binding._prepare_history_migration_call(target, -1002, 42)
    waiter = manager.enqueue_history_operation(source_key='slave chat', target_chat_id=-1002,
        operation=operation, args=(), kwargs=kwargs, history_entry_ids=[target.id])
    try:
        queue = manager._outbound_queue
        original = queue.heads()[0]
        assert original.required_sender_bot_id == ('aux-7' if auxiliary else '__main__')
        assert queue.decode_payload_raw(original.payload)[1][HISTORY_REPLAY_KEY]['entry_ids'] == [target.id]
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        assert waiter.result().message_id == 501
        assert [kind for kind, _ in sender.calls] == ['copy_message', 'send_video']
        assert sender.calls[-1][1] == dict(chat_id=-1002, video='saved-video-file-id',
            caption='video caption', message_thread_id=42, disable_notification=True)
        assert not queue.heads()
        with connection_scope(history.db._managed_database):
            assert MsgLog.select().count() == 1
            assert MsgLog.get().file_id == 'saved-video-file-id'
    finally:
        manager._outbound_queue.close()


def test_edited_video_replay_copies_the_latest_alternate_message(history):
    target = populate(history, [None])
    with connection_scope(history.db._managed_database):
        MsgLog.update(master_msg_id_alt='-1009.88', sender_bot_id='aux-7').execute()
    op, kwargs = history.binding._prepare_history_migration_call(target, -1002, 42)
    assert op == 'copy_message'
    assert (kwargs['from_chat_id'], kwargs['message_id'], kwargs['_required_sender_bot_id']) == (-1009, 88, 'aux-7')


def test_unavailable_original_sender_never_reuses_its_file_id_with_main_bot(history):
    target = populate(history, [None])
    with connection_scope(history.db._managed_database):
        MsgLog.update(sender_bot_id='removed-bot').execute()
    sender = Sender(copy_error=BadRequest('Message to copy not found'))
    manager, executor, scheduler = prepare_runtime(history, sender)
    op, kwargs = history.binding._prepare_history_migration_call(target, -1002, 42)
    try:
        waiter = manager.enqueue_history_operation(source_key='slave chat', target_chat_id=-1002,
            operation=op, args=(), kwargs=kwargs, history_entry_ids=[target.id])
        scheduler.dispatch_once()
        assert executor.submissions == []
        assert sender.calls == []  # Not even a source copy may switch to the main bot.
        assert waiter.exception() is not None
        assert manager._outbound_queue.connection.execute('SELECT delivery_hold FROM outbound_queue').fetchone()[0] == 'history_failed:RequiredSenderUnavailableError'
    finally:
        manager._outbound_queue.close()


def test_video_copy_success_never_invokes_file_id_fallback(history):
    target = populate(history, [None])
    sender = Sender()
    manager, executor, scheduler = prepare_runtime(history, sender)
    op, kwargs = history.binding._prepare_history_migration_call(target, -1002, 42)
    try:
        waiter = manager.enqueue_history_operation(source_key='slave chat', target_chat_id=-1002,
            operation=op, args=(), kwargs=kwargs, history_entry_ids=[target.id])
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        assert waiter.result().message_id == 500
        assert [kind for kind, _ in sender.calls] == ['copy_message']
    finally:
        manager._outbound_queue.close()


@pytest.mark.parametrize('during_fallback', [False, True])
def test_video_response_loss_stays_uncertain_and_is_never_sent_again(history, during_fallback):
    target = populate(history, [None])
    timeout = TimedOut()
    timeout.__cause__ = httpx.ReadTimeout('response lost')
    sender = Sender(copy_error=BadRequest('Message to copy not found') if during_fallback else timeout,
                    media_error=timeout if during_fallback else None)
    manager, executor, scheduler = prepare_runtime(history, sender)
    op, kwargs = history.binding._prepare_history_migration_call(target, -1002, 42)
    try:
        manager.enqueue_history_operation(source_key='slave chat', target_chat_id=-1002,
            operation=op, args=(), kwargs=kwargs, history_entry_ids=[target.id])
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        for _ in range(5):
            scheduler.dispatch_once()
        assert len(executor.submissions) == 1
        assert len(sender.calls) == (2 if during_fallback else 1)
        assert manager._outbound_queue.connection.execute('SELECT delivery_hold FROM outbound_queue').fetchone()[0].startswith('uncertain:')
    finally:
        manager._outbound_queue.close()


def test_failed_video_replay_is_retained_and_later_history_can_continue(history):
    target = populate(history, [None])
    sender = Sender(copy_error=BadRequest('Message to copy not found'), media_error=BadRequest('Wrong file identifier'))
    manager, executor, scheduler = prepare_runtime(history, sender)
    op, kwargs = history.binding._prepare_history_migration_call(target, -1002, 42)
    try:
        manager.enqueue_history_operation(source_key='slave chat', target_chat_id=-1002,
            operation=op, args=(), kwargs=kwargs, history_entry_ids=[target.id])
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        assert manager._outbound_queue.connection.execute('SELECT delivery_hold FROM outbound_queue').fetchone()[0] == 'history_failed:BadRequest'
        waiter = manager.enqueue_history_operation(source_key='slave chat', target_chat_id=-1002,
            operation='send_message', args=(), kwargs={'chat_id': -1002, 'text': 'later',
                '_required_sender_bot_id': '__main__'}, history_entry_ids=[999])
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        assert waiter.result().message_id == 502
        assert not scheduler.stopping
        assert manager._outbound_queue.connection.execute('SELECT COUNT(*) FROM outbound_queue').fetchone()[0] == 1
    finally:
        manager._outbound_queue.close()
