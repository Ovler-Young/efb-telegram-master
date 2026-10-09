"""History replay contracts: real SQLite state, bounded batching, sender-owned media."""

import sqlite3
from concurrent.futures import Future
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from telegram import InputFile
from telegram.error import BadRequest, TimedOut

from efb_telegram_master import db as db_module
from efb_telegram_master.aggregate import make_aggregate, make_source_member
from efb_telegram_master.chat_binding import ChatBindingManager
from efb_telegram_master.db import DatabaseManager, HistoryMigrationEntry, MsgLog, MsgLogMember, database
from efb_telegram_master.db_runtime import connection_scope
from efb_telegram_master.outbound import HISTORY_REPLAY_KEY, OutboundQueue, OutboundQueueScheduler
from tests.unit.test_outbound_queue_runtime_evidence import ControlledExecutor, manager_adapter
from tests.unit.test_live_aggregate import source, receipt, SourceCache
from tests.unit.test_database_safety import (
    postgres_config as postgres_config, postgres_server_config as postgres_server_config,
)


@pytest.fixture
def history(tmp_path, monkeypatch, request):
    previous = database.obj
    monkeypatch.setattr(db_module.utils, 'get_data_path', lambda _: tmp_path)
    config = {'database': request.getfixturevalue('postgres_config')} if getattr(request, 'param', None) == 'postgresql' else {}
    db = DatabaseManager(SimpleNamespace(channel_id='history-test', config=config))
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

    binding.bot = SimpleNamespace(enqueue_history_operation=enqueue, owned_history_entries=lambda keys: set(), forget_history_entries=lambda keys: None)
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


def test_text_batches_ignore_original_sender_changes_across_pages(history):
    texts = [f'line {index:02d}\n' for index in range(70)]
    target = populate(history, texts)
    owners = [None] * 33 + ['aux-7'] * 34 + ['aux-8'] * 2 + [None]
    with connection_scope(history.db._managed_database):
        for index, owner in enumerate(owners):
            MsgLog.update(sender_bot_id=owner).where(MsgLog.master_msg_id == f'-1001.{index + 1}').execute()
    assert history.binding._process_history_migration_target(target)
    assert [call['kwargs']['text'] for call in history.calls] == [''.join(texts)]
    assert all('_required_sender_bot_id' not in call['kwargs'] for call in history.calls)
    with connection_scope(history.db._managed_database):
        assert MsgLog.select().count() == 70
        assert HistoryMigrationEntry.select().count() == 0


@pytest.mark.parametrize('text', ['hello', None])
def test_missing_source_log_does_not_prevent_text_or_source_message_copy(history, text):
    target = populate(history, [text])
    with connection_scope(history.db._managed_database):
        MsgLog.delete().execute()  # Simulate an incomplete source, not a runtime cleanup.
    assert history.binding._process_history_migration_target(target)
    assert len(history.calls) == 1
    assert history.calls[0]['operation'] == ('send_message' if text is not None else 'copy_message')
    assert '_required_sender_bot_id' not in history.calls[0]['kwargs']
    with connection_scope(history.db._managed_database):
        assert HistoryMigrationEntry.select().count() == 0


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


@pytest.mark.parametrize('history', ['sqlite', 'postgresql'], indirect=True)
def test_live_members_expand_saved_content_and_interleave_with_legacy_media(history):
    start = datetime(2026, 1, 1)
    messages = [source('one', 'same_<&', 'Alice'), source('two', 'same_<&', 'Bob'),
                source('removed', 'saved before withdrawal', 'Carol'),
                source('moved', 'old moved body', 'Dave')]
    messages[0].target = source('quoted', 'quoted_body', 'Eve')
    members = [make_source_member(message, source_time=start + timedelta(seconds=second),
                                  received_time=start + timedelta(seconds=second + 1),
                                  display_prefix='live destination prefix')
               for message, second in zip(messages, [10, 40, 20, 50])]
    members[2].update(status='removed', source_revision=2)
    history.db.finalize_aggregate_message(receipt(), make_aggregate(members, 1))
    replacement = source('moved', 'new moved body', 'Dave')
    replacement_member = make_source_member(replacement, source_revision=2,
                                           source_time=start + timedelta(seconds=50),
                                           received_time=start + timedelta(seconds=51))
    history.db.finalize_member_redirect('-100.1', replacement_member, replacement, receipt(2))
    origin = members[0]['origin_uid']
    legacy = source('legacy', 'legacy body', 'Frank')
    history.db.add_or_update_message_log(legacy, receipt(3))
    with connection_scope(history.db._managed_database):
        MsgLog.update(time=start + timedelta(seconds=30)).where(MsgLog.master_msg_id == '-100.3').execute()
        MsgLog.create(master_msg_id='-100.4', slave_message_id='video', text='video caption',
                      slave_origin_uid=origin, media_type='Video', msg_type='Video', sent_to='test',
                      time=start + timedelta(seconds=35))
        original_count = MsgLog.select().count()
        original_mappings = MsgLogMember.select().count()
    history.binding.chat_manager = SourceCache()
    assert history.binding._queue_history_migration_entries(origin, -1002, 42) == 6
    staged = history.db.get_history_migration_entries(origin, -1002, 42)
    assert [entry.source_time for entry in staged] == [start + timedelta(seconds=second)
                                                     for second in [10, 20, 30, 35, 40, 50]]
    assert [entry.source_master_msg_id for entry in staged] == ['-100.1', '-100.1', '-100.3',
                                                              '-100.4', '-100.1', '-100.2']
    assert staged[0].formatted_text == ('*Alice* `08:00:10`\n'
                                        f'↪ Eve \\[{origin}/quoted]: quoted\\_body\nsame\\_<&\n\n')
    assert '*Carol*' in staged[1].formatted_text and 'saved before withdrawal' in staged[1].formatted_text
    assert staged[4].formatted_text.startswith('*Bob*') and staged[4].formatted_text.endswith('same\\_<&\n\n')
    assert staged[5].formatted_text.endswith('new moved body\n\n')
    assert all('live destination prefix' not in (entry.formatted_text or '') for entry in staged)
    sender = Sender()
    manager, executor, scheduler = prepare_runtime(history, sender)
    manager.channel = SimpleNamespace(db=history.db)

    def enqueue_and_complete(**kwargs):
        history.calls.append(kwargs)
        waiter = manager.enqueue_history_operation(**kwargs)
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        return waiter

    history.binding.bot = SimpleNamespace(enqueue_history_operation=enqueue_and_complete,
                                         owned_history_entries=manager.owned_history_entries,
                                         forget_history_entries=manager.forget_history_entries)
    try:
        assert history.binding._process_history_migration_target(history.db.get_next_history_migration_target())
        assert not manager._outbound_queue.heads()
    finally:
        manager._outbound_queue.close()
    assert [call['operation'] for call in history.calls] == ['send_message', 'copy_message', 'send_message']
    assert [kind for kind, _ in sender.calls] == ['send_message', 'copy_message', 'send_message']
    assert all('log_context' not in call and 'log_context' not in call['kwargs'] for call in history.calls)
    with connection_scope(history.db._managed_database):
        assert MsgLog.select().count() == original_count
        assert MsgLogMember.select().count() == original_mappings
        assert history.db.get_msg_log(master_msg_id='-100.1').aggregate['children'][2]['status'] == 'removed'


@pytest.mark.parametrize('history', ['sqlite', 'postgresql'], indirect=True)
def test_global_history_order_and_cursor_use_final_position_without_changing_ownership(history):
    start = datetime(2026, 1, 1)
    # Deliberately reverse the insertion/source-container order and include
    # tied source times whose receive times determine their publication order.
    entries = [dict(slave_chat_id='slave chat', target_chat_id='-1002', message_thread_id='42',
                    source_master_msg_id=f'-100.{index}', formatted_text=f'{index}\n', position=64 - index,
                    source_time=start + timedelta(seconds=index // 2),
                    received_time=start + timedelta(seconds=index)) for index in reversed(range(65))]
    entries.extend([
        dict(slave_chat_id='slave chat', target_chat_id='-1002', message_thread_id='42',
             source_master_msg_id='unknown', formatted_text='unknown\n', position=65),
        dict(slave_chat_id='slave chat', target_chat_id='-1002', message_thread_id='42',
             source_master_msg_id='received', formatted_text='received\n', position=66,
             received_time=start + timedelta(seconds=40)),
    ])
    assert history.db.replace_history_migration_entries('slave chat', -1002, 42, entries) == 67
    head = history.db.get_next_history_migration_target()
    assert head.formatted_text == 'unknown\n' and head.position == 0
    assert head.id != min(entry.id for entry in history.db.get_history_migration_entries('slave chat', -1002, 42))
    pages, after = [], None
    while True:
        page = history.db.get_history_migration_entries('slave chat', -1002, 42, limit=32, after=after)
        pages.extend(page)
        if len(page) < 32:
            break
        after = page[-1].position, page[-1].id
    assert [entry.formatted_text for entry in pages] == ['unknown\n'] + [f'{i}\n' for i in range(65)] + ['received\n']
    assert [entry.position for entry in pages] == list(range(67))
    assert len({entry.ownership_key for entry in pages}) == 67
    assert history.db.get_history_migration_ownership_keys([entry.id for entry in pages]) == [entry.ownership_key for entry in pages]
    assert history.binding._process_history_migration_target(head)
    assert history.calls[0]['kwargs']['text'] == ''.join(entry.formatted_text for entry in pages)


def test_old_pending_history_gains_nullable_received_time_and_keeps_its_replay(tmp_path, request):
    with closing(sqlite3.connect(tmp_path / 'tgdata.db')) as legacy:
        legacy.execute(
            'CREATE TABLE historymigrationentry ('
            'id INTEGER PRIMARY KEY, slave_chat_id TEXT NOT NULL, target_chat_id TEXT NOT NULL, '
            'message_thread_id TEXT, source_master_msg_id TEXT NOT NULL, formatted_text TEXT, '
            'media_type TEXT, source_time DATETIME, position INTEGER NOT NULL, created_at DATETIME NOT NULL)'
        )
        legacy.execute(
            'INSERT INTO historymigrationentry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (7, 'slave chat', '-1002', '42', '-1001.1', 'legacy pending\n', 'Text',
             datetime(2026, 1, 1), 12, datetime(2026, 1, 1)),
        )
        legacy.commit()
    # Start the existing fixture only after the historical database is on disk.
    history = request.getfixturevalue('history')
    restored = history.db.get_next_history_migration_target()
    assert restored.received_time is None and restored.ownership_key == 'legacy:7'
    assert restored.position == 12
    assert history.binding._process_history_migration_target(restored)
    assert history.calls[0]['kwargs']['text'] == 'legacy pending\n'
    assert history.db.get_next_history_migration_target() is None


class Sender:
    def __init__(self, copy_error=None, media_error=None):
        self.copy_error = copy_error
        self.media_error = media_error
        self.calls = []
        self.file_requests = []
        self.file_path = None

    def get_file(self, file_id):
        self.file_requests.append(file_id)
        return SimpleNamespace(file_path=self.file_path)

    def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        self.calls.append(('copy_message', dict(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, **kwargs)))
        if self.copy_error:
            raise self.copy_error
        return SimpleNamespace(message_id=500)

    def send_video(self, chat_id, video, **kwargs):
        assert isinstance(video, InputFile)  # Original bot's file ID is not passed to the sender.
        contents = video.input_file_content.read(1024)
        self.calls.append(('send_video', dict(chat_id=chat_id, video=contents, **kwargs)))
        if self.media_error:
            raise self.media_error
        return SimpleNamespace(message_id=501)

    def send_message(self, chat_id, text, **kwargs):
        self.calls.append(('send_message', dict(chat_id=chat_id, text=text, **kwargs)))
        return SimpleNamespace(message_id=502)


def prepare_runtime(history, sender):
    manager = manager_adapter()
    manager._bot = sender
    media = history.path / 'saved-video.mp4'
    media.write_bytes(b'saved media')
    sender.file_path = str(media)
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
def test_video_copy_missing_fetches_with_owner_but_sends_normally(history, auxiliary):
    target = populate(history, [None])
    if auxiliary:
        with connection_scope(history.db._managed_database):
            MsgLog.update(sender_bot_id='aux-7').execute()
    sender = Sender(copy_error=BadRequest('Message to copy not found'))
    manager, executor, scheduler = prepare_runtime(history, sender)
    owner = Sender() if auxiliary else sender
    owner.file_path = sender.file_path
    if auxiliary:
        aux = SimpleNamespace(disabled=False, bot_id='aux-7', bot=owner,
                              check_membership_tri=lambda _: False, peek_delay=lambda _: 0,
                              try_acquire_limits=lambda _: True)
        manager.bot_pool = SimpleNamespace(get_bot_by_id=lambda _: aux,
            candidate_bots=lambda _: [(aux, False)], record_successful_auxiliary_send=Mock())
    operation, kwargs = history.binding._prepare_history_migration_call(target, -1002, 42)
    waiter = manager.enqueue_history_operation(source_key='slave chat', target_chat_id=-1002,
        operation=operation, args=(), kwargs=kwargs, history_entry_ids=[target.id])
    try:
        queue = manager._outbound_queue
        original = queue.heads()[0]
        assert original.required_sender_bot_id is None
        assert queue.decode_payload_raw(original.payload)[1][HISTORY_REPLAY_KEY]['entry_ids'] == [target.id]
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        assert waiter.result().message_id == 501
        assert [kind for kind, _ in sender.calls] == ['copy_message', 'send_video']
        assert sender.calls[-1][1] == dict(chat_id=-1002, video=b'saved media',
            caption='video caption', message_thread_id=42, disable_notification=True)
        assert owner.file_requests == ['saved-video-file-id']
        if auxiliary:
            assert owner.calls == [] and sender.file_requests == []
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
    assert (kwargs['from_chat_id'], kwargs['message_id']) == (-1009, 88)
    assert kwargs[HISTORY_REPLAY_KEY]['source_sender_bot_id'] == 'aux-7'
    assert '_required_sender_bot_id' not in kwargs


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
        finish_attempt(executor, scheduler)
        assert [kind for kind, _ in sender.calls] == ['copy_message']
        assert sender.file_requests == []  # The main bot cannot acquire another bot's file ID.
        assert waiter.exception() is not None
        assert manager._outbound_queue.connection.execute('SELECT delivery_hold FROM outbound_queue').fetchone()[0]
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
            operation='send_message', args=(), kwargs={'chat_id': -1002, 'text': 'later'}, history_entry_ids=[999])
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        assert waiter.result().message_id == 502
        assert not scheduler.stopping
        assert manager._outbound_queue.connection.execute('SELECT COUNT(*) FROM outbound_queue').fetchone()[0] == 1
    finally:
        manager._outbound_queue.close()
