"""A file ID's acquisition identity must not constrain replay sending or batching."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from telegram import InputFile
from telegram.error import BadRequest

from efb_telegram_master.db import MsgLog
from efb_telegram_master.db_runtime import connection_scope
from efb_telegram_master.outbound import HISTORY_REPLAY_KEY, OutboundQueue, RequiredSenderUnavailableError
from tests.unit.test_history_replay import (
    Sender, finish_attempt, history as history, populate, prepare_runtime,
)

MEDIA = [
    ('Photo', 'send_photo', 'photo'), ('Video', 'send_video', 'video'),
    ('Animation', 'send_animation', 'animation'), ('Document', 'send_document', 'document'),
    ('Audio', 'send_audio', 'audio'), ('Voice', 'send_voice', 'voice'),
    ('Sticker', 'send_sticker', 'sticker'), ('AnimatedSticker', 'send_sticker', 'sticker'),
    ('VideoSticker', 'send_sticker', 'sticker'),
]


class MediaSender(Sender):
    def __getattr__(self, operation):
        arguments = {item[1]: item[2] for item in MEDIA}
        if operation not in arguments:
            raise AttributeError(operation)

        def send(**kwargs):
            argument = arguments[operation]
            upload = kwargs[argument]
            assert isinstance(upload, InputFile)
            kwargs[argument] = upload.input_file_content.read(1024)
            self.calls.append((operation, kwargs))
            return SimpleNamespace(message_id=501)
        return send


def attach_owner(manager, owner, *, disabled=False, membership=False, prefer=False):
    auxiliary = SimpleNamespace(
        disabled=disabled, bot_id='aux-7', bot=owner,
        check_membership_tri=Mock(return_value=membership), peek_delay=lambda _: 0,
        try_acquire_limits=lambda _: True,
    )
    manager.bot_pool = SimpleNamespace(
        get_bot_by_id=lambda value: auxiliary if str(value) == 'aux-7' else None,
        candidate_bots=lambda _: [] if disabled else [(auxiliary, membership)],
        preferred_sender=lambda _: auxiliary if prefer else None,
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
def test_media_is_acquired_by_owner_and_sent_by_normal_selection(history, media_type, operation, argument, owner_id):
    target, copy_operation, kwargs = prepare_owned(history, owner=owner_id, media_type=media_type)
    main = MediaSender(copy_error=BadRequest('Message to copy not found'))
    manager, executor, scheduler = prepare_runtime(history, main)
    manager.me = SimpleNamespace(id=123)
    owner = MediaSender() if owner_id == 'aux-7' else main
    owner.file_path = main.file_path
    attach_owner(manager, owner, membership=False)
    queue = manager._outbound_queue
    try:
        waiter = enqueue_owned(manager, target, copy_operation, kwargs)
        assert queue.heads()[0].required_sender_bot_id is None
        scheduler.dispatch_once()
        assert executor.submissions[0][1][-1].sender is main
        finish_attempt(executor, scheduler)
        assert waiter.result().message_id == 501
        assert [kind for kind, _ in main.calls] == ['copy_message', operation]
        assert main.calls[-1][1][argument] == b'saved media'
        assert main.calls[-1][1]['message_thread_id'] == 42
        assert owner.file_requests == ['saved-video-file-id']
        if owner is not main:
            assert main.file_requests == [] and owner.calls == []
        with connection_scope(history.db._managed_database):
            assert MsgLog.get().sender_bot_id == owner_id
            assert MsgLog.get().file_id == 'saved-video-file-id'
    finally:
        queue.close()


def test_send_can_choose_auxiliary_even_when_file_owner_is_main(history):
    target, operation, kwargs = prepare_owned(history, owner=None)
    main, selected = MediaSender(), MediaSender(copy_error=BadRequest('Message to copy not found'))
    manager, executor, scheduler = prepare_runtime(history, main)
    attach_owner(manager, selected, membership=True, prefer=True)
    try:
        waiter = enqueue_owned(manager, target, operation, kwargs)
        scheduler.dispatch_once()
        assert executor.submissions[0][1][-1].sender is selected
        finish_attempt(executor, scheduler)
        assert waiter.result().message_id == 501
        assert main.file_requests == ['saved-video-file-id'] and main.calls == []
        assert selected.file_requests == []
        assert [kind for kind, _ in selected.calls] == ['copy_message', 'send_video']
    finally:
        manager._outbound_queue.close()


@pytest.mark.parametrize('unavailable', ['missing', 'disabled', 'not_member'])
def test_text_does_not_depend_on_original_bot_availability(history, unavailable, monkeypatch):
    target, operation, kwargs = prepare_owned(history, 'historical text')
    main, owner = Sender(), Sender()
    manager, executor, scheduler = prepare_runtime(history, main)
    if unavailable != 'missing':
        attach_owner(manager, owner, disabled=unavailable == 'disabled')
    monkeypatch.setattr(history.db, 'get_msg_log', Mock(side_effect=AssertionError('text must not fetch its owner')))
    operation, kwargs = history.binding._prepare_history_migration_call(target, -1002, 42)
    try:
        waiter = enqueue_owned(manager, target, operation, kwargs)
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        assert waiter.result().message_id == 502
        assert main.calls[0][0] == 'send_message'
        assert not main.file_requests and not owner.file_requests and not owner.calls
    finally:
        manager._outbound_queue.close()


@pytest.mark.parametrize('text', ['old text', None])
def test_old_queued_sender_pin_is_not_a_send_constraint_after_restart(history, text):
    target, operation, kwargs = prepare_owned(history, text)
    sender = Sender()  # Successful source copy needs no media acquisition.
    manager, executor, scheduler = prepare_runtime(history, sender)
    queue = manager._outbound_queue
    try:
        enqueue_owned(manager, target, operation, kwargs)
        row = queue.heads()[0]
        with queue.connection:
            queue.connection.execute('UPDATE outbound_queue SET required_sender_bot_id=? WHERE id=?', ('removed-bot', row.id))
        queue.close()
        manager._outbound_queue = queue = OutboundQueue(history.path)
        scheduler.queue = queue
        scheduler.dispatch_once()
        assert executor.submissions[0][1][-1].sender is sender
        finish_attempt(executor, scheduler)
        assert sender.calls[0][0] == operation
        assert not queue.heads()
    finally:
        queue.close()


@pytest.mark.parametrize('hold,attempted,ready', [
    ('history_failed:RequiredSenderUnavailableError', False, True),
    ('history_failed:RequiredSenderUnavailableError', True, False),
    ('uncertain:TimedOut', True, False),
    ('in_flight', True, False),
])
def test_only_legacy_unattempted_sender_rejection_is_released(history, hold, attempted, ready):
    target, operation, kwargs = prepare_owned(history, 'text')
    manager, _, _ = prepare_runtime(history, Sender())
    queue = manager._outbound_queue
    enqueue_owned(manager, target, operation, kwargs)
    with queue.connection:
        queue.connection.execute('UPDATE outbound_queue SET delivery_hold=?, attempt_started_at=?', (hold, 123.0 if attempted else None))
    queue.close()
    with_reopened = OutboundQueue(history.path)
    try:
        assert bool(with_reopened.heads(ready_only=True)) is ready
        assert with_reopened.connection.execute('SELECT COUNT(*) FROM outbound_queue').fetchone()[0] == 1
    finally:
        with_reopened.close()


@pytest.mark.parametrize('state', ['missing', 'disabled', 'rejected'])
def test_get_file_never_falls_back_to_another_bot(history, state):
    main, owner = Sender(), Sender()
    manager, _, _ = prepare_runtime(history, main)
    if state != 'missing':
        attach_owner(manager, owner, disabled=state == 'disabled')
    if state == 'rejected':
        owner.get_file = Mock(side_effect=BadRequest('wrong file identifier'))
    try:
        with pytest.raises((RequiredSenderUnavailableError, BadRequest)):
            manager.get_file('owner-file-id', sender_bot_id='aux-7')
        assert main.file_requests == []
    finally:
        manager._outbound_queue.close()


def test_remote_download_is_chunked_and_not_forwarded_as_owner_url(history, monkeypatch):
    manager, _, _ = prepare_runtime(history, Sender())
    url = 'https://files.example/file/botOWNER/media.mp4'
    owner = SimpleNamespace(get_file=Mock(return_value=SimpleNamespace(file_path=url)))
    attach_owner(manager, owner, membership=False)
    requested = []

    @contextmanager
    def stream(method, path, **kwargs):
        requested.append((method, path))
        def chunks(*, chunk_size):
            assert chunk_size == 1024 * 1024
            yield b'one'
            yield b'two'
        yield SimpleNamespace(raise_for_status=lambda: None, iter_bytes=chunks)

    monkeypatch.setattr(httpx, 'stream', stream)
    try:
        with manager._history_media_upload('file-id', 'aux-7') as upload:
            assert isinstance(upload, InputFile)
            handle = upload.input_file_content
            assert handle.read(16) == b'onetwo'
        assert handle.closed
        owner.get_file.assert_called_once_with('file-id')
        assert requested == [('GET', url)]
        assert manager._bot.file_requests == []
    finally:
        manager._outbound_queue.close()


def test_legacy_media_owner_column_is_used_only_for_acquisition(history):
    target, operation, kwargs = prepare_owned(history)
    main = Sender(copy_error=BadRequest('Message to copy not found'))
    owner = Sender()
    manager, executor, scheduler = prepare_runtime(history, main)
    owner.file_path = main.file_path
    attach_owner(manager, owner, membership=False)
    queue = manager._outbound_queue
    try:
        enqueue_owned(manager, target, operation, kwargs)
        row = queue.heads()[0]
        args, raw = queue.decode_payload_raw(row.payload)
        raw[HISTORY_REPLAY_KEY].pop('source_sender_bot_id')
        with queue.connection:
            queue.connection.execute(
                'UPDATE outbound_queue SET required_sender_bot_id=?, payload=? WHERE id=?',
                ('aux-7', queue.encode_payload(args, raw), row.id),
            )
        queue.close()
        manager._outbound_queue = queue = OutboundQueue(history.path)
        scheduler.queue = queue
        scheduler.dispatch_once()
        finish_attempt(executor, scheduler)
        assert [kind for kind, _ in main.calls] == ['copy_message', 'send_video']
        assert owner.file_requests == ['saved-video-file-id'] and owner.calls == []
        assert main.file_requests == [] and not queue.heads()
    finally:
        queue.close()


def test_etm_message_acquisition_uses_shared_owner_route_without_main_fallback(history, monkeypatch):
    from efb_telegram_master import message as message_module
    main, owner = Sender(), Sender()
    manager, _, _ = prepare_runtime(history, main)
    attach_owner(manager, owner)
    owner.get_file = Mock(side_effect=BadRequest('file unavailable'))
    monkeypatch.setattr(message_module, 'coordinator', SimpleNamespace(master=SimpleNamespace(bot_manager=manager)))
    message = message_module.ETMMsg(file_id='owned-file')
    message.sender_bot_id = 'aux-7'
    try:
        message._load_file()
        owner.get_file.assert_called_once_with('owned-file')
        assert main.file_requests == []
    finally:
        manager._outbound_queue.close()


def test_local_api_acquisition_reuses_local_path_without_reading_file(history, monkeypatch):
    manager, _, _ = prepare_runtime(history, Sender())
    manager._local_mode = True
    owner = SimpleNamespace(get_file=Mock(return_value=SimpleNamespace(file_path=manager._bot.file_path)))
    attach_owner(manager, owner, membership=False)
    monkeypatch.setattr(httpx, 'stream', Mock(side_effect=AssertionError('local file must not be downloaded')))
    try:
        with manager._history_media_upload('file-id', 'aux-7') as upload:
            assert upload == (history.path / 'saved-video.mp4').as_uri()
        owner.get_file.assert_called_once_with('file-id')
    finally:
        manager._outbound_queue.close()
