"""History snapshot, seek cost, and cross-database ownership recovery."""

import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest

from efb_telegram_master.db import HistoryMigrationEntry, HistoryMigrationTarget, MsgLog
from efb_telegram_master.db_runtime import connection_scope
from tests.unit.test_database_safety import postgres_config, postgres_server_config
from tests.unit.test_history_replay import (
    history, populate, prepare_runtime, Sender, finish_attempt,
)


def staging(text, position=0):
    return dict(slave_chat_id='slave chat', target_chat_id='-1002', message_thread_id='42',
                source_master_msg_id=str(position), formatted_text=text, position=position)


def test_snapshot_is_finite_and_formatting_does_not_block_live_writes(history):
    populate(history, ['old'] * 70)
    seen = []
    live = sqlite3.connect(history.path / 'tgdata.db', timeout=0.1)

    def entries():
        after = None
        while True:
            page = history.db.get_recent_messages('slave chat', limit=32, after=after)
            if not page:
                return
            # New traffic, including a backdated message, must neither block nor
            # enter this replay's read snapshot.
            for timestamp in ('2025-01-01', '2027-01-01'):
                key = f'live-{len(seen)}-{timestamp}'
                live.execute(
                    'INSERT INTO msglog (master_msg_id, slave_message_id, text, slave_origin_uid, '
                    'msg_type, sent_to, time) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (key, key, 'new', 'slave chat', 'Text', 'test', timestamp),
                )
            live.commit()
            for row in page:
                seen.append(row.master_msg_id)
                yield staging(row.text, len(seen))
            after = page[-1].time, page[-1].master_msg_id

    try:
        assert history.db.replace_history_migration_entries('slave chat', -1002, 42, entries()) == 70
    finally:
        live.close()
    assert len(seen) == len(set(seen)) == 70
    with connection_scope(history.db._managed_database):
        assert MsgLog.select().count() == 76


@pytest.mark.parametrize('boundary', ['format', 'stage', 'publish', 'cleanup'])
def test_replacement_publication_is_atomic_at_every_boundary(history, monkeypatch, boundary):
    populate(history, ['old'])
    if boundary == 'stage':
        with connection_scope(history.db._managed_database):
            HistoryMigrationTarget.delete().execute()
            HistoryMigrationEntry.delete().execute()
            HistoryMigrationEntry.create(**staging('old'))
    original_insert = HistoryMigrationEntry.insert_many
    original_delete = HistoryMigrationEntry.delete
    calls = 0

    def insert(batch):
        nonlocal calls
        calls += 1
        # A separate connection can write after each staging page committed.
        with sqlite3.connect(history.path / 'tgdata.db', timeout=0.1) as live:
            live.execute("UPDATE msglog SET text = 'live'")
        assert [entry.formatted_text for entry in history.db.get_history_migration_entries(
            'slave chat', -1002, 42,
        )] == ['old']
        if boundary == 'stage' and calls == 2:
            raise RuntimeError('interrupted')
        return original_insert(batch)

    def fail(*args, **kwargs):
        raise RuntimeError('interrupted')

    def entries():
        for i in range(70):
            if boundary == 'format' and i == 40:
                raise RuntimeError('interrupted')
            yield staging('new', i)

    # insert_many itself is inside the short writer transaction, before its
    # first statement; the previous page has committed when this runs.
    monkeypatch.setattr(HistoryMigrationEntry, 'insert_many', insert)
    if boundary == 'publish':
        monkeypatch.setattr(HistoryMigrationTarget, 'insert', fail)
    if boundary == 'cleanup':
        monkeypatch.setattr(HistoryMigrationEntry, 'delete', fail)
    with pytest.raises(RuntimeError, match='interrupted'):
        history.db.replace_history_migration_entries('slave chat', -1002, 42, entries())
    visible = history.db.get_history_migration_entries('slave chat', -1002, 42)
    assert [entry.formatted_text for entry in visible] == (['new'] * 70 if boundary == 'cleanup' else ['old'])
    monkeypatch.setattr(HistoryMigrationEntry, 'insert_many', original_insert)
    monkeypatch.setattr(HistoryMigrationEntry, 'delete', original_delete)


@pytest.mark.parametrize('null_time', [False, True])
def test_history_seek_cost_does_not_grow_with_consumed_prefix(history, null_time):
    timestamp = None if null_time else datetime(2026, 1, 1)
    with connection_scope(history.db._managed_database):
        with history.db._managed_database.atomic():
            for offset in range(0, 100000, 500):
                MsgLog.insert_many([
                    dict(master_msg_id=f'{i:06d}', slave_message_id=str(i), text='x',
                         slave_origin_uid='slave chat', msg_type='Text', sent_to='test', time=timestamp)
                    for i in range(offset, offset + 500)
                ]).execute()
        connection = history.db._managed_database.connection()
        costs = []
        for offset in (0, 99000):
            steps = 0

            def progress():
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(progress, 1)
            try:
                rows = history.db.get_recent_messages('slave chat', limit=32, after=(timestamp, f'{offset:06d}'))
            finally:
                connection.set_progress_handler(None, 0)
            assert [row.master_msg_id for row in rows] == [f'{i:06d}' for i in range(offset + 1, offset + 33)]
            costs.append(steps)
        assert costs[1] < costs[0] * 2
        assert costs[1] < 5000


def test_null_and_equal_timestamps_cross_pages_without_loss(history):
    populate(history, ['x'] * 75)
    with connection_scope(history.db._managed_database):
        MsgLog.update(time=None).where(MsgLog.master_msg_id.in_([f'-1001.{i}' for i in range(1, 36)])).execute()
        MsgLog.update(time=datetime(2026, 1, 1)).where(MsgLog.time.is_null(False)).execute()
    expected = [row.master_msg_id for row in history.db.get_recent_messages('slave chat', limit=0)]
    actual, after = [], None
    while True:
        page = history.db.get_recent_messages('slave chat', limit=7, after=after)
        if not page:
            break
        actual.extend(row.master_msg_id for row in page)
        after = page[-1].time, page[-1].master_msg_id
    assert actual == expected
    assert len(set(actual)) == 75


@pytest.mark.parametrize('history', ['sqlite', 'postgresql'], indirect=True)
@pytest.mark.parametrize('boundary', ['before_commit', 'after_commit', 'first_delete', 'all_deleted', 'completed'])
def test_durable_handoff_survives_each_interruption(history, monkeypatch, boundary):
    target = populate(history, ['A', 'B'])
    sender = Sender()
    manager, executor, scheduler = prepare_runtime(history, sender)
    enqueue = manager.enqueue_history_operation
    delete = history.db.delete_history_migration_entry
    forgotten = manager.forget_history_entries
    deleted = 0

    def interrupted_enqueue(**kwargs):
        waiter = enqueue(**kwargs)
        if boundary == 'completed':
            scheduler.dispatch_once()
            finish_attempt(executor, scheduler)
        if boundary in ('after_commit', 'completed'):
            raise RuntimeError('process interrupted after queue commit')
        return waiter

    def interrupted_delete(identifier):
        nonlocal deleted
        delete(identifier)
        deleted += 1
        if boundary == 'first_delete' and deleted == 1:
            raise RuntimeError('process interrupted after first deletion')

    def interrupted_forget(keys):
        raise RuntimeError('process interrupted before receipt cleanup')

    history.binding.bot = SimpleNamespace(
        enqueue_history_operation=interrupted_enqueue,
        owned_history_entries=manager.owned_history_entries,
        forget_history_entries=interrupted_forget if boundary == 'all_deleted' else forgotten,
    )
    monkeypatch.setattr(history.db, 'delete_history_migration_entry', interrupted_delete)
    queue = manager._outbound_queue
    if boundary == 'before_commit':
        queue.connection.execute("CREATE TRIGGER interrupt_enqueue BEFORE INSERT ON outbound_queue BEGIN SELECT RAISE(ABORT, 'interrupted'); END")
    try:
        if boundary in ('first_delete', 'all_deleted'):
            with pytest.raises(RuntimeError, match='interrupted'):
                history.binding._process_history_migration_target(target)
        else:
            assert not history.binding._process_history_migration_target(target)
        if boundary == 'before_commit':
            queue.connection.execute('DROP TRIGGER interrupt_enqueue')
        queue.close()
        # Reopen the real queue: completion may have already deleted its row.
        manager, executor, scheduler = prepare_runtime(history, sender)
        queue = manager._outbound_queue
        monkeypatch.setattr(history.db, 'delete_history_migration_entry', delete)

        def finish_enqueue(**kwargs):
            waiter = manager.enqueue_history_operation(**kwargs)
            scheduler.dispatch_once()
            finish_attempt(executor, scheduler)
            return waiter

        history.binding.bot = SimpleNamespace(
            enqueue_history_operation=finish_enqueue,
            owned_history_entries=manager.owned_history_entries,
            history_ownership_page=manager.history_ownership_page,
            forget_history_entries=manager.forget_history_entries,
        )
        monkeypatch.setattr(history.binding, '_start_history_migration_worker',
                            history.binding._process_pending_history_migrations_locked)
        history.binding.resume_pending_history_migrations()
        if queue.heads():
            scheduler.dispatch_once()
            finish_attempt(executor, scheduler)
        assert [call[1]['text'] for call in sender.calls] == ['AB']
        assert not history.db.has_pending_history_migrations()
        assert not queue.heads()
        assert queue.history_ownership_page() == []
        # A new explicit relink is a new replay, even if SQLite reuses stage IDs.
        history.db.replace_history_migration_entries('slave chat', -1002, 42, [staging('A'), staging('B', 1)])
        target = history.db.get_next_history_migration_target()
        assert history.binding._process_history_migration_target(target)
        assert [call[1]['text'] for call in sender.calls] == ['AB', 'AB']
    finally:
        manager._outbound_queue.close()


def test_interrupted_generation_is_reclaimed_on_reopen_without_hidden_history_scans(history, monkeypatch):
    from efb_telegram_master import db as db_module
    from peewee import IntegrityError

    published = populate(history, ['published'])
    generation = published.generation
    history.db.delete_history_migration_entry(published.id)
    managed = history.db._managed_database
    with connection_scope(managed):
        HistoryMigrationTarget.delete().execute()
        managed.execute_sql(
            "CREATE TRIGGER interrupt_publication BEFORE INSERT ON historymigrationtarget "
            "BEGIN SELECT RAISE(ABORT, 'publication interrupted'); END"
        )
    with pytest.raises(IntegrityError, match='publication interrupted'):
        history.db.replace_history_migration_entries(
            'slave chat', -1002, 42, (staging('hidden', i) for i in range(100000)),
        )

    def check_visibility(expected):
        with connection_scope(history.db._managed_database):
            connection = history.db._managed_database.connection()
            steps = 0
            statements = []

            def progress():
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(progress, 1)
            connection.set_trace_callback(statements.append)
            try:
                assert history.db.has_pending_history_migrations() is bool(expected)
                head = history.db.get_next_history_migration_target()
                assert (head.formatted_text if head else None) == (expected[0] if expected else None)
                page = history.db.get_history_migration_entries('slave chat', -1002, 42, limit=32)
                assert [entry.formatted_text for entry in page] == expected
            finally:
                connection.set_progress_handler(None, 0)
                connection.set_trace_callback(None)
            assert steps < 3000, steps
            plans = [row[3] for sql in statements if sql.lstrip().upper().startswith('SELECT')
                     for row in connection.execute('EXPLAIN QUERY PLAN ' + sql)]
            assert any('history_generation_id' in plan for plan in plans)
            assert any('history_target_generation_position' in plan for plan in plans)

    check_visibility([])
    with connection_scope(managed):
        assert HistoryMigrationEntry.select().count() == 100000
        managed.execute_sql("DROP TRIGGER interrupt_publication")
        HistoryMigrationTarget.create(slave_chat_id='slave chat', target_chat_id='-1002',
                                      message_thread_id='42', generation=generation)
        current = HistoryMigrationEntry.create(**staging('published', 100001), generation=generation)
    check_visibility(['published'])
    with connection_scope(managed):
        legacy = HistoryMigrationEntry.create(**dict(staging('legacy'), target_chat_id='-1003'))
    history.db.stop_worker()

    create_database = db_module.sqlite_database
    delete_sizes = []

    def instrument_database(*args, **kwargs):
        reopened = create_database(*args, **kwargs)
        execute = reopened.execute_sql

        def execute_sql(sql, params=None, *args, **kwargs):
            if sql.startswith('DELETE FROM "historymigrationentry"'):
                delete_sizes.append(len(params))
                # Reclamation must not keep the startup schema transaction or
                # the previous deletion's writer lock across batches.
                with sqlite3.connect(history.path / 'tgdata.db', timeout=0.1) as live:
                    live.execute("UPDATE msglog SET text = 'live during reclamation'")
            return execute(sql, params, *args, **kwargs)

        reopened.execute_sql = execute_sql
        return reopened

    monkeypatch.setattr(db_module, 'sqlite_database', instrument_database)
    reopened = db_module.DatabaseManager(SimpleNamespace(channel_id='history-test', config={}))
    history.db = reopened
    try:
        assert delete_sizes and max(delete_sizes) <= 256
        with connection_scope(reopened._managed_database):
            assert {row.id for row in HistoryMigrationEntry.select()} == {current.id, legacy.id}
            assert HistoryMigrationEntry.get_by_id(current.id).generation == generation
            assert MsgLog.select().count() == 1
        check_visibility(['published'])
    finally:
        reopened.stop_worker()


def test_visibility_checks_during_preparation_do_not_reclaim_active_generation(history, monkeypatch):
    populate(history, ['published'])
    original_insert = HistoryMigrationEntry.insert_many
    batches = 0

    def insert(batch):
        nonlocal batches
        batches += 1
        assert history.db.has_pending_history_migrations()
        assert history.db.get_next_history_migration_target().formatted_text == 'published'
        assert [entry.formatted_text for entry in history.db.get_history_migration_entries('slave chat', -1002, 42)] == ['published']
        assert HistoryMigrationEntry.select().count() == 1 + (batches - 1) * 32
        return original_insert(batch)

    monkeypatch.setattr(HistoryMigrationEntry, 'insert_many', insert)
    assert history.db.replace_history_migration_entries(
        'slave chat', -1002, 42, (staging('new', i) for i in range(70)),
    ) == 70
    assert len(history.db.get_history_migration_entries('slave chat', -1002, 42)) == 70
