import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from telegram import CallbackQuery, Chat, Document, Message, TextQuote, Update, User
from ehforwarderbot import Channel, MsgType, coordinator
from ehforwarderbot.exceptions import EFBOperationNotSupported

from efb_telegram_master import TelegramChannel
from efb_telegram_master.aggregate import make_source_member, utf16_length
from efb_telegram_master.chat_destination_cache import ChatDestinationCache
from efb_telegram_master.db import MsgLog
from efb_telegram_master.master_message import MasterMessageProcessor
from efb_telegram_master.member_selection import SourceMemberSelector
from efb_telegram_master.msg_type import TGMsgType
from efb_telegram_master.utils import chat_id_to_str
from tests.unit.test_live_aggregate import source, SourceCache, receipt
from tests.unit.test_live_aggregate_runtime import runtime, append, complete


@pytest.fixture
def handler(runtime, monkeypatch):
    manager = runtime
    processor = MasterMessageProcessor.__new__(MasterMessageProcessor)
    processor.bot = manager
    processor.channel = Mock(spec=Channel, **vars(manager.channel))
    manager.channel = processor.channel
    processor.channel.channel_id = 'tests.master'
    processor.channel._ = lambda text: text
    processor.channel.topic_group = None
    processor.channel_id = 'tests.master'
    processor.db = manager.channel.db
    processor.chat_manager = SourceCache()
    processor.logger = manager.logger
    processor.chat_dest_cache = ChatDestinationCache(False)
    manager.channel.chat_binding = SimpleNamespace(warn_forum_limit=lambda *args: None)
    manager.channel.chat_manager = processor.chat_manager
    processor.db.add_chat_assoc(chat_id_to_str('tests.master', '-100'), chat_id_to_str('tests.source', 'group'))
    manager.dispatcher = SimpleNamespace(add_handler=lambda handler: None)
    manager.reply_error = lambda update, text: manager.send_message(update.effective_chat.id, text)
    manager.edit_message_reply_markup = lambda **kwargs: None
    manager.answer_callback_query = lambda *args, **kwargs: None
    processor.members = SourceMemberSelector(processor)
    notices = []
    def send_message(chat_id, text, **kwargs):
        message = Message(1000 + len(notices), datetime.datetime.now(datetime.timezone.utc), Chat(chat_id, 'supergroup'),
                          text=text, reply_markup=kwargs.get('reply_markup'))
        notices.append(message)
        return message
    manager.send_message = send_message
    processor.notices = notices
    processor.sent = []
    def send_source(message):
        processor.sent.append(message)
        return SimpleNamespace(uid='sent-' + str(len(processor.sent)))
    monkeypatch.setattr(coordinator, 'send_message', send_source)
    monkeypatch.setattr(coordinator, 'slaves', {'tests.source': Mock(spec=Channel,
        channel_id='tests.source', channel_name='Source', supported_message_types={MsgType.Text, MsgType.File})})
    return processor


def incoming(target, text='reply body', quote=None, document=None, user=1):
    message = Message(500, datetime.datetime.now(datetime.timezone.utc), target.chat,
                      from_user=User(user, 'Admin', False), reply_to_message=target,
                      text=text if document is None else None,
                      caption=text if document else None, quote=quote, document=document,
                      message_thread_id=target.message_thread_id)
    return Update(1, message=message)


def click(handler, notice=None, choice=1, user=1, data=None):
    notice = notice or handler.notices[-1]
    data = data or notice.reply_markup.inline_keyboard[choice][0].callback_data
    query = CallbackQuery('callback', User(user, 'User', False), 'instance', message=notice, data=data)
    handler.members.callback(Update(2, callback_query=query), SimpleNamespace())
    return data


def container(handler, texts=('first source', 'second source')):
    for index, text in enumerate(texts):
        append(handler.bot, 'source-' + str(index), 1 + index / 10, text)
    complete(handler.bot)
    return handler.bot.transport.messages[1]


def test_ordinary_reply_selects_nonfirst_source_and_preserves_media(handler, monkeypatch):
    target = container(handler)
    update = incoming(target)
    handler.msg(update, SimpleNamespace())
    assert handler.sent == []
    click(handler)
    assert [(message.text, message.target.uid) for message in handler.sent] == [('reply body', 'source-1')]
    # A media reply remains intact while a separate chooser is pending.
    document = Document('file-id', 'unique-id', file_name='report.txt', mime_type='text/plain', file_size=10)
    media = incoming(target, 'original caption', document=document)
    # Actual Telegram downloads are outside this fake transport; preserve the
    # ordinary forwarding pipeline and verify the emitted source media identity.
    monkeypatch.setattr('efb_telegram_master.message.ETMMsg._load_file', lambda self: None)
    handler.msg(media, SimpleNamespace())
    assert len(handler.sent) == 1
    click(handler, choice=0)
    emitted = handler.sent[-1]
    assert (emitted.type, emitted.file_id, emitted.file_unique_id, emitted.filename,
            emitted.text, emitted.target.uid) == (MsgType.File, 'file-id', 'unique-id', 'report.txt',
                                                   'original caption', 'source-0')
    assert handler.bot.channel.flag('text_aggregation') is False


def test_confirmed_utf16_quote_is_unique_and_ambiguities_choose(handler):
    target = container(handler, ('emoji 😀 first', 'uniquely selected'))
    position = utf16_length(target.text[:target.text.index('uniquely')])
    update = incoming(target, quote=TextQuote('uniquely', position))
    handler.msg(update, SimpleNamespace())
    assert handler.sent[-1].target.uid == 'source-1'
    sent_count = len(handler.sent)
    for quote in (TextQuote('Alice', 0), TextQuote('first\n\nAlice', utf16_length(target.text[:target.text.index('first')])),
                  TextQuote('uniquely', position - 1)):
        handler.msg(incoming(target, quote=quote), SimpleNamespace())
        assert len(handler.sent) == sent_count
        assert handler.notices[-1].reply_markup
    # A quote still present in an older Telegram snapshot must use selection.
    old = Message(target.message_id, target.date, target.chat, text=target.text + '\nolder snapshot')
    handler.msg(incoming(old, quote=TextQuote('uniquely', position)), SimpleNamespace())
    assert len(handler.sent) == sent_count
    click(handler)
    assert handler.sent[-1].target.uid == 'source-1'


def test_callbacks_bind_requester_and_expire_and_recheck_source_route(handler):
    target = container(handler)
    handler.msg(incoming(target), SimpleNamespace())
    notice = handler.notices[-1]
    data = click(handler, notice, user=2)
    assert not handler.sent
    click(handler, notice, data=data)
    assert handler.sent[-1].target.uid == 'source-1'
    handler.msg(incoming(target), SimpleNamespace())
    notice = handler.notices[-1]
    handler.members = SourceMemberSelector(handler)  # restart discards transient selection only
    click(handler, notice)
    assert len(handler.sent) == 1
    handler.msg(incoming(target), SimpleNamespace())
    notice = handler.notices[-1]
    handler.db.add_chat_assoc(chat_id_to_str('tests.master', '-200'), chat_id_to_str('tests.source', 'group'))
    click(handler, notice)
    assert len(handler.sent) == 1
    assert 'no longer available' in handler.notices[-1].text


def test_rm_selected_source_updates_only_after_source_success(handler, monkeypatch):
    handler.channel.flag.config['prevent_message_removal'] = False
    deleted = []
    handler.bot.delete_message = lambda *args, **kwargs: deleted.append(args)
    target = container(handler)
    statuses = []
    def fail(status):
        raise EFBOperationNotSupported('removal unavailable')
    monkeypatch.setattr(coordinator, 'send_status', fail)
    handler.delete_message(incoming(target, '/rm'), SimpleNamespace())
    click(handler)
    assert [child['status'] for child in MsgLog.get().aggregate['children']] == ['active', 'active']
    assert not handler.bot._outbound_queue.aggregation_rows()
    handler.msg(incoming(target), SimpleNamespace())
    stale_reply_selection = handler.notices[-1]
    monkeypatch.setattr(coordinator, 'send_status', statuses.append)
    handler.delete_message(incoming(target, '/rm'), SimpleNamespace())
    click(handler)
    click(handler, stale_reply_selection)
    assert not handler.sent
    complete(handler.bot)
    assert statuses[-1].message.uid == 'source-1'
    row = MsgLog.get()
    assert [child['status'] for child in row.aggregate['children']] == ['active', 'removed']
    assert 'first source' in row.text and '[message removed]' in row.text
    assert len(handler.bot.transport.messages) == 1 and not deleted


def test_old_display_quote_resolves_redirect_and_known_source_lookup(handler):
    target = container(handler)
    restored = TelegramChannel.get_message_by_id(handler.channel, source('source-1').chat, 'source-1')
    assert (restored.uid, restored.text) == ('source-1', 'second source')
    row = MsgLog.get()
    changed = source('source-1', 'independent updated content')
    new_member = make_source_member(changed, source_revision=2)
    handler.db.finalize_member_redirect(row.master_msg_id, new_member, changed, receipt(2))
    position = utf16_length(target.text[:target.text.index('second source')])
    handler.msg(incoming(target, quote=TextQuote('second source', position)), SimpleNamespace())
    assert (handler.sent[-1].target.uid, handler.sent[-1].target.text) == ('source-1', 'independent updated content')
    restored = TelegramChannel.get_message_by_id(handler.channel, changed.chat, changed.uid)
    assert (restored.uid, restored.text) == ('source-1', 'independent updated content')
    untouched = source('source-0', 'first source')
    restored = TelegramChannel.get_message_by_id(handler.channel, untouched.chat, untouched.uid)
    assert restored.uid == 'source-0'


def test_pagination_preserves_duplicate_members_and_revalidates_topic(handler):
    target = container(handler, tuple(['duplicate'] * 10))
    # Equal text cannot identify a source using its location alone.
    update = incoming(target, quote=TextQuote('duplicate', utf16_length(target.text[:target.text.index('duplicate')])))
    handler.msg(update, SimpleNamespace())
    assert not handler.sent
    notice = handler.notices[-1]
    edits = []
    handler.bot.edit_message_reply_markup = lambda **kwargs: edits.append(kwargs)
    next_page = notice.reply_markup.inline_keyboard[-1][-1].callback_data
    click(handler, notice, data=next_page)
    second_page = edits[-1]['reply_markup']
    click(handler, notice, data=second_page.inline_keyboard[1][0].callback_data)
    assert handler.sent[-1].target.uid == 'source-9'
    handler.msg(incoming(target), SimpleNamespace())
    notice = handler.notices[-1]
    handler.channel.topic_group = -100
    handler.db.add_topic_assoc(-100, 77, chat_id_to_str('tests.source', 'group'))
    click(handler, notice, choice=0)
    assert len(handler.sent) == 1
    assert 'no longer available' in handler.notices[-1].text


def test_single_message_paths_and_deferred_aggregate_reactions(handler, monkeypatch):
    original = source('legacy', 'ordinary single source')
    handler.db.add_or_update_message_log(original, receipt(88))
    target = Message(88, datetime.datetime.now(datetime.timezone.utc), Chat(-100, 'supergroup'), text=original.text)
    handler.msg(incoming(target), SimpleNamespace())
    assert handler.sent[-1].target.uid == 'legacy'
    statuses = []
    deleted = []
    monkeypatch.setattr(coordinator, 'send_status', statuses.append)
    handler.channel.flag.config['prevent_message_removal'] = False
    handler.bot.delete_message = lambda *args, **kwargs: deleted.append(args)
    handler.delete_message(incoming(target, '/rm'), SimpleNamespace())
    assert statuses[-1].message.uid == 'legacy'
    assert deleted == [(-100, 88)]
    handler.channel.bot_manager = handler.bot
    aggregate = container(handler)
    TelegramChannel.react(handler.channel, incoming(aggregate, '/react 👍'), SimpleNamespace())
    assert len(statuses) == 1
    assert 'not supported yet' in handler.notices[-1].text


def test_managed_alternate_output_reply_and_rm_use_effective_member_state(handler, monkeypatch):
    container(handler)
    changed = source('source-1', 'independent first version')
    member = make_source_member(changed, source_revision=2)
    handler.db.finalize_member_redirect('-100.1', member, changed, receipt(2))
    changed.type = MsgType.File
    changed.type_telegram = TGMsgType.Document
    changed.text = 'current independent attachment'
    changed.file_id = 'alternate-file'
    changed.mime = 'text/plain'
    latest = make_source_member(changed, source_revision=3)
    handler.db.finalize_source_message(changed, receipt(3), latest, old_message_id=(-100, 2))
    row = handler.db.get_msg_log(master_msg_id='-100.2')
    assert row.master_msg_id_alt == '-100.3'
    assert handler.db.get_msg_log(master_msg_id='-100.3') is None
    handler.db.add_chat_assoc(chat_id_to_str('tests.master', '-100'),
                              chat_id_to_str('tests.source', 'other'), multiple_slave=True)
    document = Document('alternate-file', 'alternate-unique', file_name='updated.txt', mime_type='text/plain')
    actual = Message(3, datetime.datetime.now(datetime.timezone.utc), Chat(-100, 'supergroup'),
                     document=document, caption=changed.text)
    handler.msg(incoming(actual), SimpleNamespace())
    assert (handler.sent[-1].target.uid, handler.sent[-1].target.text, handler.sent[-1].target.type) == (
        'source-1', changed.text, MsgType.File)
    statuses = []
    monkeypatch.setattr(coordinator, 'send_status', statuses.append)
    handler.bot.transport.delete_message = lambda chat_id, message_id, **kwargs: True
    handler.delete_message(incoming(actual, '/rm'), SimpleNamespace())
    assert statuses[-1].message.uid == 'source-1'
    key = (member['origin_uid'], -100, None)
    assert handler.bot.live_aggregation.source_state(key, (member['origin_uid'], 'source-1'))[1]['status'] == 'removed'
    assert handler.db.get_msg_log(master_msg_id='-100.1').aggregate['children'][0]['status'] == 'active'
    # The withdrawal is durable but the original Telegram file is still visible.
    # A reply to either output ID must reject the pending removed source.
    handler.msg(incoming(actual), SimpleNamespace())
    canonical = Message(2, actual.date, actual.chat, text='independent first version')
    handler.msg(incoming(canonical), SimpleNamespace())
    assert len(handler.sent) == 1
    assert 'no longer available' in handler.notices[-1].text
