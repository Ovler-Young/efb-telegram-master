"""History replay entry states read from real staging rows and queue ownership."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from efb_telegram_master import db as db_module
from efb_telegram_master.db import HistoryMigrationEntry
from efb_telegram_master.db_runtime import connection_scope
from efb_telegram_master.history_states import EntryState, entry_state
from tests.unit import test_history_replay
from tests.unit.test_history_replay import Sender, populate, prepare_runtime

history = test_history_replay.history


def states(history, queue):
    """The state of every staging row, read the way the writers read it."""
    with connection_scope(history.db._managed_database):
        rows = list(HistoryMigrationEntry.select(
            HistoryMigrationEntry.id, HistoryMigrationEntry.generation,
            history.db._history_entry_target().alias('published_generation'),
        ).order_by(HistoryMigrationEntry.id).objects())
    owned = queue.owned_history_entries([row.ownership_key for row in rows])
    return [entry_state(published=row.generation == row.published_generation, owned=row.ownership_key in owned)
            for row in rows]


def test_entry_is_staged_published_queued_and_finished_once_across_a_restart(history, monkeypatch, make_queue):
    sender = Sender()
    manager, _, _ = prepare_runtime(history, sender, make_queue=make_queue)
    queue = manager._outbound_queue

    def interrupted_publication(**kwargs):
        raise RuntimeError('process interrupted before publication')

    stale = {
        'slave_chat_id': 'slave chat', 'target_chat_id': '-1002', 'message_thread_id': '42',
        'source_master_msg_id': '-1001.99', 'formatted_text': 'stale', 'media_type': 'Text',
        'source_time': datetime(2026, 1, 1), 'position': 0,
    }
    with monkeypatch.context() as interrupted:
        interrupted.setattr(db_module.HistoryMigrationTarget, 'insert', staticmethod(interrupted_publication))
        with pytest.raises(RuntimeError, match='before publication'):
            history.db.replace_history_migration_entries('slave chat', -1002, 42, [stale])
    assert states(history, queue) == [EntryState.STAGED]
    # Publishing another generation supersedes the one left behind.
    target = populate(history, ['A', 'B'])
    assert states(history, queue) == [EntryState.PUBLISHED, EntryState.PUBLISHED]

    def interrupted_enqueue(**kwargs):
        manager.enqueue_history_operation(**kwargs)
        raise RuntimeError('process interrupted after queue commit')

    history.binding.bot = SimpleNamespace(
        enqueue_history_operation=interrupted_enqueue,
        owned_history_entries=manager.owned_history_entries,
        forget_history_entries=manager.forget_history_entries,
    )
    assert not history.binding._process_history_migration_target(target)
    assert states(history, queue) == [EntryState.QUEUED, EntryState.QUEUED]

    queue.close()
    history.db.stop_worker()
    history.db = history.binding.db = db_module.DatabaseManager(
        SimpleNamespace(channel_id='history-test', config={}))
    manager, _, _ = prepare_runtime(history, sender, make_queue=make_queue)
    queue = manager._outbound_queue
    try:
        # Startup reclamation keeps entries the queue already owns.
        assert states(history, queue) == [EntryState.QUEUED, EntryState.QUEUED]
        history.binding.bot = SimpleNamespace(
            enqueue_history_operation=manager.enqueue_history_operation,
            owned_history_entries=manager.owned_history_entries,
            forget_history_entries=manager.forget_history_entries,
        )
        assert history.binding._process_history_migration_target(history.db.get_next_history_migration_target())
        assert states(history, queue) == []
        assert queue.history_ownership_page() == []
        assert queue.connection.execute('SELECT COUNT(*) FROM outbound_queue').fetchone()[0] == 1
    finally:
        queue.close()
        history.db.stop_worker()

