import datetime
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User

from efb_telegram_master.aggregation_settings import AggregationSettings
from efb_telegram_master.chat import ETMGroupChat
from efb_telegram_master.db import DatabaseManager, SlaveChatInfo, database
from efb_telegram_master import migrate_db
from efb_telegram_master.utils import ExperimentalFlagsManager, chat_id_to_str
from tests.unit.test_live_aggregate import manager


def source_chat(manager, uid='group'):
    return ETMGroupChat(manager, module_id='tests.source', module_name='Source', uid=uid, name=uid)


def test_override_cache_persistence_metadata_and_deletion(manager):
    chat = source_chat(manager)
    manager.set_text_aggregation_override(chat, True)
    chat.name = 'Renamed'
    manager.set_slave_chat_info(chat)
    row = manager.get_slave_chat_info(chat.module_id, chat.uid)
    assert row.text_aggregation_enabled is True
    assert row.slave_chat_name == 'Renamed'
    with patch.object(database.obj, 'execute_sql', side_effect=AssertionError('hot-path query')):
        assert manager.get_text_aggregation_override(chat.module_id, chat.uid) is True
        assert manager.get_text_aggregation_override(chat.module_id, 'other') is None
    with patch.object(database.obj, 'execute_sql', side_effect=RuntimeError('write failed')):
        with pytest.raises(RuntimeError, match='write failed'):
            manager.set_text_aggregation_override(chat, False)
    assert manager.get_text_aggregation_override(chat.module_id, chat.uid) is True
    manager.set_text_aggregation_override(chat, False)
    assert manager.get_slave_chat_info(chat.module_id, chat.uid).text_aggregation_enabled is False
    manager.set_text_aggregation_override(chat, None)
    assert manager.get_text_aggregation_override(chat.module_id, chat.uid) is None
    assert manager.get_slave_chat_info(chat.module_id, chat.uid).text_aggregation_enabled is None
    manager.set_text_aggregation_override(chat, True)
    # A member with the same UID is a different record; deleting it cannot reset the chat.
    manager.delete_slave_chat_info(chat.module_id, chat.uid, 'parent')
    assert manager.get_text_aggregation_override(chat.module_id, chat.uid) is True
    manager.delete_slave_chat_info(chat.module_id, chat.uid)
    assert manager.get_text_aggregation_override(chat.module_id, chat.uid) is None
    assert manager.get_slave_chat_info(chat.module_id, chat.uid) is None


def test_old_schema_upgrade_and_restart_load_overrides(tmp_path, monkeypatch):
    original = database.obj
    monkeypatch.setattr('efb_telegram_master.db.utils.get_data_path', lambda _: tmp_path)
    with sqlite3.connect(tmp_path / 'tgdata.db') as db:
        db.execute('''CREATE TABLE slavechatinfo (
            id INTEGER PRIMARY KEY, slave_channel_id TEXT NOT NULL, slave_channel_emoji TEXT NOT NULL,
            slave_chat_uid TEXT NOT NULL, slave_chat_group_id TEXT, slave_chat_name TEXT NOT NULL,
            slave_chat_alias TEXT, slave_chat_type TEXT NOT NULL, pickle BLOB)''')
        db.execute("INSERT INTO slavechatinfo VALUES (1, 'tests.source', '', 'group', NULL, 'Group', NULL, 'GroupChat', NULL)")
    config = SimpleNamespace(channel_id='tests.settings', config={})
    first = DatabaseManager(config)
    try:
        assert first.get_slave_chat_info('tests.source', 'group').text_aggregation_enabled is None
        first.set_text_aggregation_override(source_chat(first), False)
        first.set_text_aggregation_override(source_chat(first, 'other'), True)
    finally:
        first.stop_worker()
    second = DatabaseManager(config)
    try:
        assert second.get_text_aggregation_override('tests.source', 'group') is False
        assert second.get_text_aggregation_override('tests.source', 'other') is True
        assert second.get_text_aggregation_override('tests.source', 'missing') is None
        # The offline importer must normalize SQLite booleans for PostgreSQL.
        with sqlite3.connect(tmp_path / 'tgdata.db') as db:
            imported = list(migrate_db._source_rows(db, SlaveChatInfo))
        field_index = next(index for index, field in enumerate(SlaveChatInfo._meta.sorted_fields)
                           if field.name == 'text_aggregation_enabled')
        assert [row[field_index] for row in imported] == [False, True]
    finally:
        second.stop_worker()
        database.initialize(original)


@pytest.fixture
def settings(manager):
    chats = {uid: source_chat(manager, uid) for uid in ('one', 'two')}
    bot = Mock()
    bot.send_message.return_value = SimpleNamespace(message_id=100)
    channel = SimpleNamespace(db=manager, config={'admins': [7, 8]}, bot_manager=bot,
                              channel_id='tests.master', _=lambda text: text,
                              chat_manager=SimpleNamespace(get_chat=lambda module, uid, **kwargs: chats[uid]))
    channel.flag = ExperimentalFlagsManager(channel)
    channel.flag.config['text_aggregation'] = True
    return AggregationSettings(channel)


def update(*, user_id=7, topic=None, forum=False, callback=None, message_id=1):
    user = User(user_id, 'Admin', False)
    chat = Chat(-100, 'supergroup', is_forum=forum)
    message = Message(message_id, datetime.datetime.now(datetime.timezone.utc), chat,
                      from_user=user, text='/aggregation', message_thread_id=topic,
                      is_topic_message=topic is not None)
    if callback:
        return Update(1, callback_query=CallbackQuery('query', user, 'chat', data=callback, message=message))
    return Update(1, message=message)


def bind(settings, uid, topic=None):
    origin = chat_id_to_str('tests.source', uid)
    settings.channel.db.add_chat_assoc(chat_id_to_str('tests.master', '-100'), origin, multiple_slave=True)
    if topic is not None:
        settings.channel.db.add_topic_assoc(-100, topic, origin)
    return origin


def test_command_status_override_reset_and_authorization(settings):
    bind(settings, 'one')
    command = lambda args, **kwargs: settings.command(update(**kwargs), SimpleNamespace(args=args))
    command(['off'], user_id=9)
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'one') is None
    command([])
    assert 'inherited' in settings.bot.send_message.call_args.kwargs['text']
    command(['off'])
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'one') is False
    command(['reset'])
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'one') is None
    settings.channel.flag.config['text_aggregation'] = False
    command(['on'])
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'one') is True
    text = settings.bot.send_message.call_args.kwargs['text']
    assert 'off' in text and 'global' in text
    command(['invalid'])
    assert 'Usage:' in settings.bot.send_message.call_args.kwargs['text']


def test_topic_command_changes_only_bound_source(settings):
    bind(settings, 'one', topic=11)
    bind(settings, 'two', topic=1)
    settings.command(update(topic=11, forum=True), SimpleNamespace(args=['off']))
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'one') is False
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'two') is None
    settings.command(update(forum=True), SimpleNamespace(args=['on']))
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'two') is True
    settings.command(update(topic=999, forum=True), SimpleNamespace(args=['off']))
    assert 'no linked source chat' in settings.bot.send_message.call_args.kwargs['text']
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'two') is True


def test_multiple_bindings_require_selection_and_recheck(settings):
    bind(settings, 'one')
    second = bind(settings, 'two')
    settings.command(update(), SimpleNamespace(args=['off']))
    callback_data = settings.bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard[1][0].callback_data
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'two') is None
    for kwargs in ({'user_id': 9}, {'user_id': 8}, {'message_id': 101}):
        args = {'message_id': 100, **kwargs}
        settings.callback(update(callback=callback_data, **args), SimpleNamespace())
        assert settings.channel.db.get_text_aggregation_override('tests.source', 'two') is None
    settings.channel.db.remove_chat_assoc(slave_uid=second)
    settings.callback(update(callback=callback_data, message_id=100), SimpleNamespace())
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'two') is None
    bind(settings, 'two')
    settings.callback(update(callback=callback_data, message_id=100), SimpleNamespace())
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'two') is False
    assert settings.channel.db.get_text_aggregation_override('tests.source', 'one') is None
    assert not settings.sessions
