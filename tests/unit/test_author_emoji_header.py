"""Group author custom emoji in message headers: rendering, queueing and dispatch."""
import html
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import telegram.error
from ehforwarderbot import Message

from efb_telegram_master import bot_manager as bot_manager_module
from efb_telegram_master import utils
from efb_telegram_master.bot_manager import TelegramBotManager
from efb_telegram_master.custom_emoji import (
    AuthorEmojiHeader,
    CustomEmojiConfig,
    CustomEmojiStore,
    UserEmoji,
    UserEmojiCreator,
    fallback_emoji,
)
from efb_telegram_master.outbound import (
    OutboundQueue,
    QueuedDeliveryResult,
    QueueRequest,
    SenderSelection,
)
from efb_telegram_master.slave_message import SlaveMessageProcessor
from tests.mocks.slave import MockSlaveChannel
from tests.unit.test_custom_emoji import profile, write_legacy_tables  # noqa: F401  (profile is a fixture)
from tests.unit.test_live_aggregate import idle_live_aggregation, source
from tests.unit.test_live_aggregate_runtime import complete, runtime  # noqa: F401  (runtime is a fixture)
from tests.unit.test_live_aggregate_source import processor as aggregation_processor

MAIN = SenderSelection(sender=None, sender_bot_id=None)
CAPABLE_AUXILIARY = "111"
INCAPABLE_AUXILIARY = "222"
TAG = '<tg-emoji emoji-id="{}">{}</tg-emoji>'


class FakeSender:
    """Records the calls a Bot API client would receive."""

    def __init__(self, failures=()):
        self.calls = []
        self._failures = list(failures)

    def send_message(self, chat_id, text, **kwargs):
        self.calls.append((text, kwargs))
        if self._failures:
            raise self._failures.pop(0)
        return SimpleNamespace(message_id=7)

    def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.calls.append((caption, kwargs))
        return SimpleNamespace(message_id=8)


class FakeBot:
    """Gives the queue the Telegram method signatures it binds queued arguments to."""

    def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        raise AssertionError("queued calls are not sent while enqueueing")

    def send_photo(self, chat_id, photo, caption=None, parse_mode=None, **kwargs):
        raise AssertionError("queued calls are not sent while enqueueing")

    def edit_message_text(self, text, chat_id=None, message_id=None, parse_mode=None, **kwargs):
        raise AssertionError("queued calls are not sent while enqueueing")

    def send_sticker(self, chat_id, sticker, reply_markup=None, **kwargs):
        raise AssertionError("queued calls are not sent while enqueueing")

    def send_location(self, chat_id, latitude=None, longitude=None, reply_markup=None, **kwargs):
        raise AssertionError("queued calls are not sent while enqueueing")


@pytest.fixture
def slave():
    return MockSlaveChannel()


@pytest.fixture
def author(slave):
    return next(member for member in slave.group.members if member.uid != "__self__")


@pytest.fixture
def author_uid(author):
    return utils.chat_id_to_str(channel_id=author.module_id, chat_uid=author.uid)


def make_store(profile, rows=(), *, enabled=True, custom_emoji_ids=None):  # noqa: F811
    directory, open_manager = profile
    write_legacy_tables(directory, [], list(rows))
    store = CustomEmojiStore(open_manager(), CustomEmojiConfig.from_channel_config(
        {"user_emoji": {"enabled": enabled, "custom_emoji_ids": custom_emoji_ids or {}}, "auxiliary_bots": [{"token": "t", "custom_emoji": True}]}))
    store.load()
    return store


def saved_row(uid, emoji_id):
    stamp = "2026-06-23 10:00:00"
    return (f"member-user:{uid}", emoji_id, "etm_user_avatars_by_bot", stamp, stamp)


def make_processor(store):
    creator = UserEmojiCreator(store, Mock(), "bot", "ETM user avatars")
    processor = object.__new__(SlaveMessageProcessor)
    processor.logger = Mock()
    processor.channel = SimpleNamespace(custom_emoji=store, custom_emoji_creator=Mock(wraps=creator))
    return processor, creator


def make_manager(tmp_path, store):
    manager = object.__new__(TelegramBotManager)
    manager._bot = FakeBot()
    manager._cleanup_tls = SimpleNamespace(pending_cleanup=[])
    manager._transfer_min_mbps = None
    manager.live_aggregation = idle_live_aggregation()
    manager.channel = SimpleNamespace(custom_emoji=store)
    manager._custom_emoji_auxiliary_ids = frozenset({CAPABLE_AUXILIARY})
    manager._outbound_queue = OutboundQueue(tmp_path)
    manager._outbound_scheduler = SimpleNamespace(
        _lock=threading.RLock(), stopping=False, failure=None, wake_event=threading.Event())
    manager._queued_db_log_context_lock = threading.Lock()
    manager._queued_db_log_contexts = {}
    manager._queued_completion_callbacks = {}
    # Edits and stickers wait for their result; store them as the blocking path would, without waiting.
    manager._enqueue_blocking_send_and_wait = lambda _slave, _chat, function, args, kwargs, **_: (
        manager._enqueue_send_task(function, args, {**kwargs, "_send_mode": "blocking"}))
    manager._enqueue_blocking_api_operation = lambda *, operation, args, kwargs, **_: (
        manager._enqueue_requests([QueueRequest(operation, args, {**kwargs, "_send_mode": "blocking"})]))
    return manager


def enqueue(manager, text, header, *, operation="send_message", parse_mode="HTML"):
    """Send through the real enqueue path and return the stored operation, args and kwargs."""
    kwargs = {"prefix": header, "_send_mode": "eventual", "_slave_id": "slave"}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    if operation == "send_message":
        manager.send_message(123, text, **kwargs)
    else:
        manager.send_photo(123, b"photo", text, **kwargs)
    newest = manager._outbound_queue.connection.execute("SELECT MAX(id) FROM outbound_queue").fetchone()[0]
    row = manager._outbound_queue.load_queued(newest)
    args, queued_kwargs = manager._outbound_queue.decode_payload_raw(row.payload)
    return row, args, queued_kwargs


def sent_text(sender):
    return sender.calls[-1][0]


def test_cached_author_tag_reaches_a_capable_sender_and_the_fallback_an_incapable_one(
        profile, tmp_path, author, author_uid):  # noqa: F811
    store = make_store(profile, [saved_row(author_uid, "5001")])
    processor, _ = make_processor(store)
    manager = make_manager(tmp_path, store)
    message = Message(chat=author.chat, author=author)
    fallback = fallback_emoji(author_uid)

    header = processor.generate_message_template(message, True)
    row, args, kwargs = enqueue(manager, "hello &lt;b&gt;", header)

    tag = TAG.format("5001", fallback)
    assert args[1] == f"{tag} {html.escape(author.long_name)}:\nhello &lt;b&gt;"

    for selection, expected in (
        (MAIN, args[1]),
        (SenderSelection(sender=None, sender_bot_id=CAPABLE_AUXILIARY), args[1]),
        (SenderSelection(sender=None, sender_bot_id=INCAPABLE_AUXILIARY),
         f"{fallback} {html.escape(author.long_name)}:\nhello &lt;b&gt;"),
    ):
        sender = FakeSender()
        manager.execute_queued_call(row, args, kwargs, SenderSelection(sender, selection.sender_bot_id))
        assert sent_text(sender) == expected
        assert ("<tg-emoji" in sent_text(sender)) == (expected is args[1])


def test_header_in_a_group_chat_header_keeps_the_emoji_after_the_chat_name_icons(
        profile, tmp_path, author, author_uid):  # noqa: F811
    store = make_store(profile, [saved_row(author_uid, "5001")])
    processor, _ = make_processor(store)
    manager = make_manager(tmp_path, store)

    header = processor.generate_message_template(Message(chat=author.chat, author=author), False)
    _, args, _ = enqueue(manager, "hi", header)

    fallback = fallback_emoji(author_uid)
    name = html.escape(author.long_name)
    chat = html.escape(author.chat.long_name)
    assert args[1].endswith(f" {TAG.format('5001', fallback)} {name} [{chat}]:\nhi")
    assert not args[1].startswith("<tg-emoji")


@pytest.mark.parametrize("operation", ["send_sticker", "send_location"])
def test_header_in_an_inline_keyboard_is_queued_as_plain_text(
        profile, tmp_path, author, author_uid, operation):  # noqa: F811
    store = make_store(profile, [saved_row(author_uid, "5001")])
    processor, _ = make_processor(store)
    manager = make_manager(tmp_path, store)
    message = Message(chat=author.chat, author=author, text="")

    header = processor.generate_message_template(message, False)
    assert isinstance(header, AuthorEmojiHeader)
    markup = SlaveMessageProcessor.build_chat_info_inline_keyboard(message, header, "", None)
    if operation == "send_sticker":
        manager.send_sticker(123, "sticker-file-id", reply_markup=markup, _slave_id="slave")
    else:
        manager.send_location(123, latitude=1.0, longitude=2.0, reply_markup=markup, _slave_id="slave")
    newest = manager._outbound_queue.connection.execute("SELECT MAX(id) FROM outbound_queue").fetchone()[0]
    row = manager._outbound_queue.load_queued(newest)

    assert b"AuthorEmojiHeader" not in row.payload
    _, kwargs = manager._outbound_queue.decode_payload_raw(row.payload)
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert type(button.text) is str and button.text == str(header)


def test_markdown_send_gets_the_plain_header(profile, tmp_path, author, author_uid):  # noqa: F811
    store = make_store(profile, [saved_row(author_uid, "5001")])
    processor, _ = make_processor(store)
    manager = make_manager(tmp_path, store)

    header = processor.generate_message_template(Message(chat=author.chat, author=author), True)
    _, args, _ = enqueue(manager, "hi", header, parse_mode="Markdown")

    assert args[1] == f"{fallback_emoji(author_uid)} {author.long_name}:\nhi"


def test_edit_by_an_incapable_authoring_bot_drops_the_tag(profile, tmp_path, author, author_uid):  # noqa: F811
    store = make_store(profile, [saved_row(author_uid, "5001")])
    processor, _ = make_processor(store)
    manager = make_manager(tmp_path, store)
    header = processor.generate_message_template(Message(chat=author.chat, author=author), True)
    manager.edit_message_text(
        "edited", chat_id=123, message_id=9, prefix=header, parse_mode="HTML", _sender_bot_id=INCAPABLE_AUXILIARY)
    row = manager._outbound_queue.load_queued(1)
    args, kwargs = manager._outbound_queue.decode_payload_raw(row.payload)
    assert TAG.format("5001", fallback_emoji(author_uid)) in args[0]

    sender = SimpleNamespace(edit_message_text=Mock(return_value=SimpleNamespace(message_id=9)))
    manager.execute_queued_call(
        row, args, kwargs, SenderSelection(sender=sender, sender_bot_id=INCAPABLE_AUXILIARY))

    edited = sender.edit_message_text.call_args.args[0]
    assert "<tg-emoji" not in edited and edited.startswith(fallback_emoji(author_uid))


def test_parse_error_retry_sends_the_fallback_not_the_tag(profile, tmp_path, author, author_uid):  # noqa: F811
    store = make_store(profile, [saved_row(author_uid, "5001")])
    processor, _ = make_processor(store)
    manager = make_manager(tmp_path, store)
    header = processor.generate_message_template(Message(chat=author.chat, author=author), True)
    row, args, kwargs = enqueue(manager, "hi", header)

    sender = FakeSender(failures=[telegram.error.BadRequest("Can't parse entities: unclosed tag")])
    manager.execute_queued_call(row, args, kwargs, SenderSelection(sender, None))

    first, second = sender.calls
    assert "<tg-emoji" in first[0] and first[1]["parse_mode"] == "HTML"
    assert "parse_mode" not in second[1]
    assert "<tg-emoji" not in second[0] and second[0].startswith(fallback_emoji(author_uid))


@pytest.mark.parametrize("operation", ["send_message", "send_photo"])
@pytest.mark.parametrize("name_length", [3, 80, 100])
def test_oversized_content_never_carries_or_splits_the_tag(tmp_path, profile, name_length, operation):  # noqa: F811
    store = make_store(profile)
    manager = make_manager(tmp_path, store)
    emoji = UserEmoji("5001", "🙂")
    name = "n" * name_length
    header = AuthorEmojiHeader(f"🙂 {name}:", emoji, 0)
    limit = 4096 if operation == "send_message" else 1024
    row, args, kwargs = enqueue(manager, "x" * limit, header, operation=operation)

    sender = FakeSender()
    result = manager.execute_queued_call(row, args, kwargs, SenderSelection(sender, None))

    assert isinstance(result, QueuedDeliveryResult)
    assert "<tg-emoji" not in sent_text(sender) and "</tg-emoji" not in sent_text(sender)
    assert sent_text(sender).startswith("🙂 n")
    attachment = result.supplement.kwargs["document"].decode("utf-8")
    assert "tg-emoji" not in attachment and f"🙂 {name}:" in attachment


def test_user_text_with_a_literal_tag_is_delivered_unchanged(profile, tmp_path, author, author_uid):  # noqa: F811
    store = make_store(profile, [saved_row(author_uid, "5001")])
    processor, _ = make_processor(store)
    manager = make_manager(tmp_path, store)
    literal = '<tg-emoji emoji-id="42">🙂</tg-emoji>'
    escaped = html.escape(literal)
    with_header = processor.generate_message_template(Message(chat=author.chat, author=author), True)

    for header in (with_header, ""):
        row, args, kwargs = enqueue(manager, escaped, header)
        for selection in (MAIN, SenderSelection(None, INCAPABLE_AUXILIARY)):
            sender = FakeSender()
            manager.execute_queued_call(row, args, kwargs, SenderSelection(sender, selection.sender_bot_id))
            assert sent_text(sender).endswith(escaped)
            assert sent_text(sender).count("&lt;tg-emoji") == 1
    # Header-less sends hold no tag of ours at all, so the whole text is what the user wrote.
    assert sent_text(sender) == escaped


@pytest.mark.parametrize("enabled", [True, False])
def test_header_text_that_looks_like_a_tag_is_kept_when_no_parse_mode_is_set(
        profile, tmp_path, author_uid, enabled):  # noqa: F811
    store = make_store(profile, enabled=enabled)
    manager = make_manager(tmp_path, store)
    literal = '<tg-emoji emoji-id="1">😀</tg-emoji> Alice:'
    row, args, kwargs = enqueue(manager, "hello", literal, parse_mode=None)
    assert args[1] == f"{literal}\nhello"

    sender = FakeSender()
    manager.execute_queued_call(row, args, kwargs, SenderSelection(sender, INCAPABLE_AUXILIARY))

    assert sent_text(sender) == f"{literal}\nhello"


def test_uncached_author_gets_the_plain_header_and_one_request(profile, tmp_path, author, author_uid):  # noqa: F811
    store = make_store(profile)
    processor, _ = make_processor(store)
    message = Message(chat=author.chat, author=author)

    header = processor.generate_message_template(message, True)

    assert header == f"{author.long_name}:" and type(header) is str
    processor.channel.custom_emoji_creator.request.assert_called_once()
    assert processor.channel.custom_emoji_creator.request.call_args.args[0] == author_uid


def test_uncached_author_avatar_is_fetched_only_when_the_worker_asks(profile, slave, author, author_uid):  # noqa: F811
    store = make_store(profile)
    processor, _ = make_processor(store)
    slave.get_chat_member_picture = Mock(side_effect=AssertionError("fetched on the send path"))
    processor.generate_message_template(Message(chat=author.chat, author=author), False)
    loader = processor.channel.custom_emoji_creator.request.call_args.args[1]

    picture = object()
    slave.get_chat_member_picture = Mock(return_value=picture)
    with patch.dict("ehforwarderbot.coordinator.slaves", {slave.channel_id: slave}):
        assert loader() is picture
    slave.get_chat_member_picture.assert_called_once_with(author)


def test_disabled_feature_leaves_headers_as_they_were(profile, author, author_uid):  # noqa: F811
    store = make_store(profile, [saved_row(author_uid, "5001")], enabled=False)
    processor, _ = make_processor(store)
    message = Message(chat=author.chat, author=author)

    singly = processor.generate_message_template(message, True)
    full = processor.generate_message_template(message, False)

    assert type(singly) is str and singly == f"{author.long_name}:"
    assert type(full) is str
    assert full == f"{author.chat.channel_emoji}{'👥'} {author.long_name} [{author.chat.long_name}]:"
    processor.channel.custom_emoji_creator.request.assert_not_called()


def test_disabled_feature_still_uses_configured_ids_and_creates_nothing(
        profile, tmp_path, author, author_uid):  # noqa: F811
    other = next(member for member in author.chat.members if member.uid not in ("__self__", author.uid))
    store = make_store(profile, [saved_row(author_uid, "5001")], enabled=False,
                       custom_emoji_ids={author_uid: "9001"})
    processor, _ = make_processor(store)

    configured = processor.generate_message_template(Message(chat=author.chat, author=author), True)
    unconfigured = processor.generate_message_template(Message(chat=other.chat, author=other), True)

    assert isinstance(configured, AuthorEmojiHeader) and configured.emoji.custom_emoji_id == "9001"
    assert type(unconfigured) is str
    processor.channel.custom_emoji_creator.request.assert_not_called()

    manager = make_manager(tmp_path, store)
    row, args, kwargs = enqueue(manager, "hi", configured)
    sender = FakeSender()
    manager.execute_queued_call(row, args, kwargs, SenderSelection(sender, None))
    assert TAG.format("9001", configured.emoji.fallback) in sent_text(sender)


def test_auxiliary_bot_may_send_custom_emoji_only_when_its_entry_opts_in(monkeypatch):
    class FakeAuxiliaryBot:
        def __init__(self, token, **_):
            self.bot_id = int(token.split(":")[0])
            self.token = token

        def bind_runtime(self, _):
            pass

        def initialize(self):
            return True

    monkeypatch.setattr(bot_manager_module, "AuxiliaryBot", FakeAuxiliaryBot)
    monkeypatch.setattr(bot_manager_module, "BotPool", lambda bots, manager: SimpleNamespace(bots=bots))
    config = {
        "token": "1:main",
        "auxiliary_bots": [{"token": "2:yes", "custom_emoji": True}, {"token": "3:no"}],
    }
    manager = object.__new__(TelegramBotManager)
    manager._runtime = None
    manager._local_mode = False
    channel = SimpleNamespace(
        flag=lambda name: None,
        custom_emoji=SimpleNamespace(config=CustomEmojiConfig.from_channel_config(config)))

    manager._init_bot_pool(config["auxiliary_bots"], config, channel)

    assert manager._custom_emoji_auxiliary_ids == {"2"}


# The long text is short enough to combine, but its HTML is longer than a message may be.
@pytest.mark.parametrize("enabled, alice_text", [(True, "hi"), (False, "hi"), (True, "<" * 1500)])
def test_combined_message_shows_each_ready_author_emoji_and_asks_for_the_others(
        runtime, enabled, alice_text):  # noqa: F811
    manager = runtime
    manager.channel.flag.config["text_aggregation"] = True
    alice, bob = "tests.source alice", "tests.source bob"
    manager.channel.db.add_user_emoji_cache(f"member-user:{alice}", "5001", "etm_user_avatars_by_bot")
    store = CustomEmojiStore(manager.channel.db, CustomEmojiConfig.from_channel_config(
        {"user_emoji": {"enabled": enabled}}))
    store.load()
    bot_factory = Mock()
    creator = Mock(wraps=UserEmojiCreator(store, bot_factory, "bot", "ETM user avatars"))
    manager.channel.custom_emoji, manager.channel.custom_emoji_creator = store, creator
    handler = aggregation_processor(manager)
    handler.get_slave_msg_dest = lambda msg: (handler.generate_message_template(msg, True), (-100, None))

    for uid, text, name in (("one", alice_text, "Alice"), ("two", "yo", "Bob")):
        message = source(uid, text, name)
        message.chat.members.append(message.author)
        handler.send_message(message)
    complete(manager)

    [(operation, _chat, sent, _kwargs)] = manager.transport.calls
    time = r"<code>\d\d:\d\d:\d\d</code>"
    if enabled:
        alice_header = re.escape(TAG.format("5001", fallback_emoji(alice))) + " Alice:"
        assert [call.args[0] for call in creator.request.call_args_list] == [bob]
    else:
        alice_header = "Alice:"
        creator.request.assert_not_called()
    assert operation == "send_message"
    assert re.fullmatch(rf"{alice_header} {time}\n{re.escape(html.escape(alice_text))}\n\nBob: {time}\nyo", sent), sent
    bot_factory.assert_not_called()
