from types import SimpleNamespace
import io
from unittest.mock import Mock, patch

from PIL import Image
from telegram import Update
from telegram.error import BadRequest

from ehforwarderbot import Message
from ehforwarderbot.types import ChatID

from efb_telegram_master import utils
from efb_telegram_master.chat_binding import ChatListStorage
from efb_telegram_master.ptb_compat import sync_reply_text
from efb_telegram_master.utils import TelegramChatID, TelegramTopicID, TelegramMessageID


class _ReadOnlyReplyMessage:
    def __init__(self, reply_to_message):
        self.chat = SimpleNamespace(id=0, is_forum=True)
        self.message_id = reply_to_message.message_thread_id + 1
        self.message_thread_id = reply_to_message.message_thread_id
        self._reply_to_message = reply_to_message
        self.to_dict = Mock(return_value={})

    @property
    def reply_to_message(self):
        return self._reply_to_message


def _build_slave_message(slave, chat=None, author=None):
    chat = chat or slave.chat_with_alias
    author = author or chat.self
    msg = Message()
    msg.uid = "topic-group-test"
    msg.chat = chat
    msg.author = author
    msg.text = "topic group text"
    return msg


def _png_bytes(color=(255, 0, 0, 255)):
    out = io.BytesIO()
    Image.new("RGBA", (64, 64), color).save(out, "PNG")
    out.seek(0)
    return out


def _sticker_set(name, stickers):
    return SimpleNamespace(name=name, title=name, stickers=stickers)


def _sticker(emoji, custom_emoji_id):
    return SimpleNamespace(emoji=emoji, custom_emoji_id=custom_emoji_id)


def _command_update(chat_type, chat_id, *, is_forum=False, user_id=1):
    chat = SimpleNamespace(id=chat_id, type=chat_type, is_forum=is_forum)
    message = Mock()
    message.chat = chat
    message.chat_id = chat_id
    message.message_id = 1
    message.message_thread_id = None
    message.from_user = SimpleNamespace(id=user_id)
    return Update(update_id=10, message=message)


def test_topic_assoc_crud(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(10001)
    thread_id = TelegramTopicID(20002)

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) == thread_id
    assert channel.db.get_topic_slaves(topic_chat_id) == [(slave_uid, thread_id)]
    assert channel.db.get_topic_slave(topic_chat_id, thread_id) == slave_uid

    channel.db.remove_topic_assoc(topic_chat_id=topic_chat_id, message_thread_id=thread_id)
    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) is None


def test_get_topic_chat_ids_lists_all_known_topic_groups(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    other_slave_uid = "tests.mocks.slave other-topic-chat"
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.remove_topic_assoc(slave_uid=other_slave_uid)

    channel.db.add_topic_assoc(TelegramChatID(10001), TelegramTopicID(20002), slave_uid)
    channel.db.add_topic_assoc(TelegramChatID(10002), TelegramTopicID(20003), other_slave_uid)

    assert channel.db.get_topic_chat_ids() == [TelegramChatID(10001), TelegramChatID(10002)]

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.remove_topic_assoc(slave_uid=other_slave_uid)


def test_get_slave_msg_dest_uses_topic_group(channel, slave):
    topic_group = TelegramChatID(30003)
    msg = _build_slave_message(slave)

    with patch.object(channel.bot_manager, "get_chat_info", return_value=SimpleNamespace(is_forum=True)), \
         patch.object(channel.chat_binding, "create_topic", return_value=TelegramTopicID(40004)) as create_topic, \
         patch.object(channel, "topic_group", topic_group):
        _, (tg_dest, thread_id) = channel.slave_messages.get_slave_msg_dest(msg)

    assert tg_dest == topic_group
    assert thread_id == TelegramTopicID(40004)
    create_topic.assert_called_once_with(
        slave_uid=utils.chat_id_to_str(chat=slave.chat_with_alias),
        telegram_chat_id=topic_group,
    )


def test_flag_manager_reads_top_level_topic_group(channel):
    channel.config["topic_group"] = 34567
    channel.config["flags"] = {}

    flag = utils.ExperimentalFlagsManager(channel)

    assert flag("topic_group") == 34567


def test_create_topic_creates_once_and_reuses_cached_assoc(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(50005)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.config["topic_icons"] = {
        "sync_avatar_to_custom_emoji": False,
    }

    forum_topic = SimpleNamespace(message_thread_id=TelegramTopicID(60006))
    with patch.object(channel.bot_manager, "create_forum_topic", return_value=forum_topic) as create_forum_topic:
        first = channel.chat_binding.create_topic(slave_uid, topic_chat_id)
        second = channel.chat_binding.create_topic(slave_uid, topic_chat_id)

    assert first == TelegramTopicID(60006)
    assert second == TelegramTopicID(60006)
    assert create_forum_topic.call_count == 1
    assert channel.db.get_topic_thread_id(slave_uid, topic_chat_id) == TelegramTopicID(60006)

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.config.pop("topic_icons", None)


def test_create_topic_does_not_set_configured_custom_emoji_id(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(61005)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.config["topic_icons"] = {
        "custom_emoji_ids": {
            slave_uid: "emoji-configured",
        },
    }

    forum_topic = SimpleNamespace(message_thread_id=TelegramTopicID(61006))
    with patch.object(channel.bot_manager, "create_forum_topic", return_value=forum_topic) as create_forum_topic:
        result = channel.chat_binding.create_topic(slave_uid, topic_chat_id)

    assert result == TelegramTopicID(61006)
    create_forum_topic.assert_called_once_with(
        chat_id=topic_chat_id,
        name=channel.chat_manager.get_chat(slave.chat_with_alias.module_id, slave.chat_with_alias.uid).chat_title,
    )

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.config.pop("topic_icons", None)


def test_topic_icon_set_name_keeps_bot_username_suffix(channel):
    channel.config.pop("topic_icons", None)

    with patch.object(channel.chat_binding, "_get_bot_user", return_value=SimpleNamespace(id=1, username="testbot")):
        base_name = channel.chat_binding._get_topic_icon_set_base_name()
        next_name = channel.chat_binding._build_topic_icon_set_name(base_name, 2)

    assert base_name == "etm_topic_icons"
    assert next_name == "etm_topic_icons_2_by_testbot"


def test_topic_icon_adds_to_existing_set_without_guessing_by_emoji(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    picture = _png_bytes((255, 0, 0, 255))
    channel.config["topic_icons"] = {
        "sync_avatar_to_custom_emoji": True,
        "sticker_set_name": "etm_topic_icons",
        "owner_user_id": 99,
    }
    existing_set = _sticker_set("etm_topic_icons_by_testbot", [_sticker("😀", "emoji-existing")])
    updated_set = _sticker_set("etm_topic_icons_by_testbot", [
        _sticker("😀", "emoji-existing"),
        _sticker(channel.chat_binding._topic_icon_emoji_name(slave_uid), "emoji-added"),
    ])

    with patch.object(channel.chat_binding, "_get_bot_user", return_value=SimpleNamespace(id=1, username="testbot")), \
         patch.object(
             channel.bot_manager,
             "get_sticker_set",
             side_effect=[existing_set, updated_set],
         ) as get_sticker_set, \
         patch.object(channel.bot_manager, "add_sticker_to_set") as add_sticker_to_set, \
         patch.object(channel.bot_manager, "create_new_sticker_set") as create_new_sticker_set:
        custom_emoji_id = channel.chat_binding._get_or_create_topic_icon_custom_emoji(slave_uid, picture)

    assert custom_emoji_id == "emoji-added"
    assert get_sticker_set.call_args_list[-1].args == ("etm_topic_icons_by_testbot",)
    add_sticker_to_set.assert_called_once()
    create_new_sticker_set.assert_not_called()
    channel.config.pop("topic_icons", None)


def test_topic_icon_reuses_db_cache_for_duplicate_avatar(channel, slave, caplog):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    picture = _png_bytes((254, 0, 0, 255))
    channel.config["topic_icons"] = {
        "sync_avatar_to_custom_emoji": True,
        "sticker_set_name": "etm_topic_icons",
        "owner_user_id": 99,
    }
    existing_set = _sticker_set("etm_topic_icons_by_testbot", [_sticker("😀", "emoji-existing")])
    updated_set = _sticker_set("etm_topic_icons_by_testbot", [
        _sticker("😀", "emoji-existing"),
        _sticker(channel.chat_binding._topic_icon_emoji_name(slave_uid), "emoji-added"),
    ])

    with patch.object(channel.chat_binding, "_get_bot_user", return_value=SimpleNamespace(id=1, username="testbot")), \
         patch.object(
             channel.bot_manager,
             "get_sticker_set",
             side_effect=[existing_set, updated_set],
         ), \
         patch.object(channel.bot_manager, "add_sticker_to_set") as add_sticker_to_set, \
         patch.object(channel.bot_manager, "create_new_sticker_set") as create_new_sticker_set:
        first = channel.chat_binding._get_or_create_topic_icon_custom_emoji(slave_uid, picture)

    with caplog.at_level("DEBUG", logger="efb_telegram_master.chat_binding"), \
         patch.object(channel.chat_binding, "_get_bot_user", return_value=SimpleNamespace(id=1, username="testbot")), \
         patch.object(channel.bot_manager, "get_sticker_set") as get_sticker_set, \
         patch.object(channel.bot_manager, "add_sticker_to_set") as add_sticker_to_set_again, \
         patch.object(channel.bot_manager, "create_new_sticker_set") as create_new_sticker_set_again:
        second = channel.chat_binding._get_or_create_topic_icon_custom_emoji(slave_uid, _png_bytes((254, 0, 0, 255)))

    assert first == "emoji-added"
    assert second == "emoji-added"
    add_sticker_to_set.assert_called_once()
    create_new_sticker_set.assert_not_called()
    get_sticker_set.assert_not_called()
    add_sticker_to_set_again.assert_not_called()
    create_new_sticker_set_again.assert_not_called()
    assert "tg://emoji?id=emoji-added" in caplog.text
    assert "https://t.me/addemoji/etm_topic_icons_by_testbot" in caplog.text
    channel.config.pop("topic_icons", None)


def test_member_avatar_namespace_does_not_reuse_legacy_global_cache(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.group.members[0])
    picture = _png_bytes((253, 0, 0, 255))
    png = channel.chat_binding._make_topic_icon_png(picture)
    assert png is not None
    avatar_hash = channel.chat_binding._hash_topic_icon_png(png)
    channel.db.set_topic_icon_cache(avatar_hash, "emoji-legacy", "etm_topic_icons_by_testbot")
    channel.config["topic_icons"] = {
        "sync_avatar_to_custom_emoji": True,
        "sticker_set_name": "etm_topic_icons",
        "owner_user_id": 99,
    }
    existing_set = _sticker_set("etm_topic_icons_by_testbot", [_sticker("😀", "emoji-existing")])
    updated_set = _sticker_set("etm_topic_icons_by_testbot", [
        _sticker("😀", "emoji-existing"),
        _sticker(channel.chat_binding._topic_icon_emoji_name(slave_uid), "emoji-member"),
    ])

    with patch.object(channel.chat_binding, "_get_bot_user", return_value=SimpleNamespace(id=1, username="testbot")), \
         patch.object(
             channel.bot_manager,
             "get_sticker_set",
             side_effect=[existing_set, updated_set],
         ), \
         patch.object(channel.bot_manager, "add_sticker_to_set") as add_sticker_to_set:
        custom_emoji_id = channel.chat_binding._get_or_create_topic_icon_custom_emoji(
            slave_uid,
            _png_bytes((253, 0, 0, 255)),
            cache_namespace="member",
        )

    assert custom_emoji_id == "emoji-member"
    add_sticker_to_set.assert_called_once()
    assert channel.db.get_topic_icon_cache(avatar_hash) == ("emoji-legacy", "etm_topic_icons_by_testbot")
    assert channel.db.get_topic_icon_cache(f"member:{slave_uid}:{avatar_hash}") == (
        "emoji-member",
        "etm_topic_icons_by_testbot",
    )
    channel.config.pop("topic_icons", None)


def test_author_avatar_lazy_reuses_user_cache_without_loading_picture(channel, slave):
    slave_uid = f"{utils.chat_id_to_str(chat=slave.group.members[0])}:cached"
    cache_key = channel.chat_binding._author_avatar_cache_key(slave_uid)
    channel.db.set_topic_icon_cache(cache_key, "emoji-user", "etm_topic_icons_by_testbot")
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded on user cache hit"))

    custom_emoji_id = channel.chat_binding.resolve_author_avatar_custom_emoji_id_lazy(slave_uid, load_picture)

    assert custom_emoji_id == "emoji-user"
    load_picture.assert_not_called()


def test_author_avatar_lazy_returns_none_when_pool_empty_without_loading_picture(channel, slave):
    slave_uid = f"{utils.chat_id_to_str(chat=slave.group.members[0])}:empty-pool"
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded without a placeholder"))

    with patch.object(channel.db, "get_topic_icon_cache_entries", return_value=[]), \
         patch.object(channel.db, "get_topic_icon_cache_custom_emoji_ids", return_value=set()), \
         patch.object(channel.chat_binding, "_ensure_author_avatar_placeholder_pool_async_locked") as ensure_pool:
        custom_emoji_id = channel.chat_binding.resolve_author_avatar_custom_emoji_id_lazy(slave_uid, load_picture)

    assert custom_emoji_id is None
    ensure_pool.assert_called_once()
    load_picture.assert_not_called()


def test_author_avatar_lazy_uses_available_placeholder_and_starts_background_update(channel, slave):
    slave_uid = f"{utils.chat_id_to_str(chat=slave.group.members[0])}:placeholder"
    channel.db.set_topic_icon_cache(
        "member-placeholder:etm_topic_icons_by_testbot:emoji-placeholder",
        "emoji-placeholder",
        "etm_topic_icons_by_testbot",
    )
    load_picture = Mock(side_effect=AssertionError("avatar should be loaded only by background thread"))
    thread = Mock()

    with patch.object(channel.chat_binding, "_ensure_author_avatar_placeholder_pool_async_locked") as ensure_pool, \
         patch("efb_telegram_master.chat_binding.threading.Thread", return_value=thread) as thread_cls:
        custom_emoji_id = channel.chat_binding.resolve_author_avatar_custom_emoji_id_lazy(slave_uid, load_picture)

    assert custom_emoji_id == "emoji-placeholder"
    ensure_pool.assert_called_once()
    thread_cls.assert_called_once()
    thread.start.assert_called_once()
    load_picture.assert_not_called()


def test_author_avatar_replace_updates_sticker_and_user_cache(channel, slave):
    slave_uid = f"{utils.chat_id_to_str(chat=slave.group.members[0])}:replace"
    user_cache_key = channel.chat_binding._author_avatar_cache_key(slave_uid)
    picture = _png_bytes((200, 10, 10, 255))
    old_sticker = SimpleNamespace(custom_emoji_id="emoji-placeholder", file_id="file-placeholder")

    with patch.object(channel.chat_binding, "_get_topic_icon_owner_user_id", return_value=99), \
         patch.object(channel.chat_binding, "_find_custom_emoji_sticker", return_value=old_sticker), \
         patch.object(channel.bot_manager, "replace_sticker_in_set", return_value=True) as replace:
        channel.chat_binding._replace_author_avatar_placeholder(
            slave_uid,
            user_cache_key,
            "emoji-placeholder",
            "etm_topic_icons_by_testbot",
            Mock(return_value=(picture, "member")),
            "msg-1",
        )

    replace.assert_called_once()
    assert replace.call_args.kwargs["user_id"] == 99
    assert replace.call_args.kwargs["name"] == "etm_topic_icons_by_testbot"
    assert replace.call_args.kwargs["old_sticker"] is old_sticker
    assert channel.db.get_topic_icon_cache(user_cache_key) == (
        "emoji-placeholder",
        "etm_topic_icons_by_testbot",
    )


def test_author_avatar_replace_keeps_placeholder_reserved_when_cache_write_fails(channel, slave):
    slave_uid = f"{utils.chat_id_to_str(chat=slave.group.members[0])}:replace-cache-fail"
    user_cache_key = channel.chat_binding._author_avatar_cache_key(slave_uid)
    picture = _png_bytes((190, 10, 10, 255))
    old_sticker = SimpleNamespace(custom_emoji_id="emoji-placeholder-fail", file_id="file-placeholder")
    channel.chat_binding._author_avatar_pending[user_cache_key] = (
        "emoji-placeholder-fail",
        "etm_topic_icons_by_testbot",
    )
    channel.chat_binding._author_avatar_inflight.add(user_cache_key)

    with patch.object(channel.chat_binding, "_get_topic_icon_owner_user_id", return_value=99), \
         patch.object(channel.chat_binding, "_find_custom_emoji_sticker", return_value=old_sticker), \
         patch.object(channel.bot_manager, "replace_sticker_in_set", return_value=True), \
         patch.object(channel.db, "set_topic_icon_cache", side_effect=RuntimeError("db down")):
        channel.chat_binding._replace_author_avatar_placeholder(
            slave_uid,
            user_cache_key,
            "emoji-placeholder-fail",
            "etm_topic_icons_by_testbot",
            Mock(return_value=(picture, "member")),
            "msg-1",
        )

    assert channel.chat_binding._author_avatar_pending[user_cache_key] == (
        "emoji-placeholder-fail",
        "etm_topic_icons_by_testbot",
    )
    assert user_cache_key in channel.chat_binding._author_avatar_inflight
    channel.chat_binding._author_avatar_pending.pop(user_cache_key, None)
    channel.chat_binding._author_avatar_inflight.discard(user_cache_key)


def test_topic_icon_telegram_error_log_includes_context(channel, caplog):
    error = BadRequest("Premium_account_required")

    with caplog.at_level("WARNING", logger="efb_telegram_master.chat_binding"):
        channel.chat_binding._log_topic_icon_telegram_error(
            "edit_forum_topic_with_icon",
            error,
            tg_chat_id=TelegramChatID(-10062005),
            thread_id=TelegramTopicID(40489),
            slave_uid="milkice.qq group_1107463201",
            custom_emoji_id="emoji-test",
            retry_without_icon=True,
        )

    assert "Topic icon Telegram error" in caplog.text
    assert "error_type='BadRequest'" in caplog.text
    assert "error_message='Premium_account_required'" in caplog.text
    assert "thread_id=40489" in caplog.text
    assert "custom_emoji_id='emoji-test'" in caplog.text
    assert "retry_without_icon=True" in caplog.text


def test_topic_icon_creates_next_set_when_existing_sets_are_full(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    picture = _png_bytes((252, 0, 0, 255))
    channel.config["topic_icons"] = {
        "sync_avatar_to_custom_emoji": True,
        "sticker_set_name": "etm_topic_icons",
        "owner_user_id": 99,
    }
    full_set = _sticker_set(
        "etm_topic_icons_by_testbot",
        [_sticker("😀", f"full-{i}") for i in range(channel.chat_binding.TELEGRAM_CUSTOM_EMOJI_SET_LIMIT)],
    )
    created_set = _sticker_set(
        "etm_topic_icons_2_by_testbot",
        [_sticker(channel.chat_binding._topic_icon_emoji_name(slave_uid), "emoji-created")],
    )
    get_sticker_set = Mock(side_effect=[full_set, BadRequest("Sticker set not found"), created_set])

    with patch.object(channel.chat_binding, "_get_bot_user", return_value=SimpleNamespace(id=1, username="testbot")), \
         patch.object(channel.bot_manager, "get_sticker_set", get_sticker_set), \
         patch.object(channel.bot_manager, "create_new_sticker_set", return_value=True) as create_new_sticker_set:
        custom_emoji_id = channel.chat_binding._get_or_create_topic_icon_custom_emoji(slave_uid, picture)

    assert custom_emoji_id == "emoji-created"
    create_new_sticker_set.assert_called_once()
    assert create_new_sticker_set.call_args.kwargs["name"] == "etm_topic_icons_2_by_testbot"
    assert create_new_sticker_set.call_args.kwargs["user_id"] == 99
    assert create_new_sticker_set.call_args.kwargs["sticker_type"] == "custom_emoji"
    assert create_new_sticker_set.call_args.kwargs["stickers"][0].format == "static"
    channel.config.pop("topic_icons", None)


def test_topic_icon_generation_failure_marks_unavailable_and_falls_back(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    picture = _png_bytes((251, 0, 0, 255))
    channel.config["topic_icons"] = {
        "sticker_set_name": "etm_topic_icons",
        "owner_user_id": 99,
    }

    with patch.object(channel.chat_binding, "_get_bot_user", return_value=SimpleNamespace(id=1, username="testbot")), \
         patch.object(channel.bot_manager, "get_sticker_set", side_effect=BadRequest("Sticker set not found")), \
         patch.object(channel.bot_manager, "create_new_sticker_set", side_effect=BadRequest("not enough rights")):
        custom_emoji_id = channel.chat_binding._get_or_create_topic_icon_custom_emoji(slave_uid, picture)

    assert custom_emoji_id is None
    assert channel.chat_binding._topic_icon_custom_emoji_unavailable is True
    channel.config.pop("topic_icons", None)
    channel.chat_binding._topic_icon_custom_emoji_unavailable = False
    channel.chat_binding._topic_icon_custom_emoji_unavailable_reason = None


def test_create_topic_does_not_generate_or_set_topic_icon(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(61505)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.config.pop("topic_icons", None)
    forum_topic = SimpleNamespace(message_thread_id=TelegramTopicID(61506))

    with patch.object(channel.chat_binding, "_get_topic_icon_picture") as get_topic_icon_picture, \
         patch.object(channel.chat_binding, "_get_or_create_topic_icon_custom_emoji") as get_or_create, \
         patch.object(channel.bot_manager, "create_forum_topic", return_value=forum_topic) as create_forum_topic:
        result = channel.chat_binding.create_topic(slave_uid, topic_chat_id)

    assert result == TelegramTopicID(61506)
    get_topic_icon_picture.assert_not_called()
    get_or_create.assert_not_called()
    create_forum_topic.assert_called_once()
    assert "icon_custom_emoji_id" not in create_forum_topic.call_args.kwargs
    assert channel.chat_binding._topic_icon_custom_emoji_unavailable is False

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.chat_binding._topic_icon_custom_emoji_unavailable = False
    channel.chat_binding._topic_icon_custom_emoji_unavailable_reason = None


def test_update_topic_info_does_not_generate_or_set_topic_icon(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(62005)
    thread_id = TelegramTopicID(62006)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)
    channel.config["topic_icons"] = {
        "sync_avatar_to_custom_emoji": True,
        "sticker_set_name": "etm_topic_icons_by_testbot",
    }
    sent_message = SimpleNamespace(message_id=123)

    with patch.object(channel.chat_binding, "_get_or_create_topic_icon_custom_emoji") as get_or_create, \
         patch.object(channel.bot_manager, "edit_forum_topic", return_value=True) as edit_forum_topic, \
         patch.object(channel.bot_manager, "send_photo", return_value=sent_message), \
         patch.object(channel.bot_manager, "pin_chat_message"), \
         patch("time.sleep"):
        success, _, count = channel.chat_binding._update_forum_group_info(
            topic_chat_id,
            sync_topic_icons=True,
        )

    assert success is True
    assert count == 1
    get_or_create.assert_not_called()
    edit_forum_topic.assert_called_once()
    assert "icon_custom_emoji_id" not in edit_forum_topic.call_args.kwargs

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.config.pop("topic_icons", None)


def test_sync_topic_icons_command_allows_non_configured_topic_group(channel):
    update = _command_update("supergroup", -10062005, is_forum=True)
    channel.topic_group = TelegramChatID(-1001)

    with patch.object(channel.bot_manager, "reply_error") as reply_error:
        channel.chat_binding.sync_topic_icons(update, Mock())

    reply_error.assert_called_once()
    assert "disabled" in reply_error.call_args.args[1]
    channel.topic_group = TelegramChatID(channel.flag('topic_group'))


def test_sync_topic_icons_private_command_reports_disabled(channel):
    update = _command_update("private", 12345)

    with patch.object(channel.bot_manager, "reply_error") as reply_error:
        channel.chat_binding.sync_topic_icons(update, Mock())

    reply_error.assert_called_once()
    assert "disabled" in reply_error.call_args.args[1]


def test_update_topic_info_does_not_clear_existing_icon_when_sync_fails(channel, slave):
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    topic_chat_id = TelegramChatID(63005)
    thread_id = TelegramTopicID(63006)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)
    channel.config["topic_icons"] = {
        "sync_avatar_to_custom_emoji": True,
        "sticker_set_name": "etm_topic_icons",
    }
    sent_message = SimpleNamespace(message_id=123)

    with patch.object(channel.chat_binding, "_get_or_create_topic_icon_custom_emoji", return_value=None), \
         patch.object(channel.bot_manager, "edit_forum_topic", return_value=True) as edit_forum_topic, \
         patch.object(channel.bot_manager, "send_photo", return_value=sent_message), \
         patch.object(channel.bot_manager, "pin_chat_message"), \
         patch("time.sleep"):
        success, _, count = channel.chat_binding._update_forum_group_info(
            topic_chat_id,
            sync_topic_icons=True,
        )

    assert success is True
    assert count == 1
    assert "icon_custom_emoji_id" not in edit_forum_topic.call_args.kwargs

    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.config.pop("topic_icons", None)


def test_master_message_routes_forum_thread_to_slave(channel, slave):
    topic_chat_id = TelegramChatID(70007)
    thread_id = TelegramTopicID(80008)
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    message = Mock()
    message.chat = SimpleNamespace(id=int(topic_chat_id), is_forum=True)
    message.message_thread_id = int(thread_id)
    message.reply_to_message = Mock(message_id=int(thread_id) + 1, message_thread_id=int(thread_id))
    message.to_dict.return_value = {}

    update = Update(update_id=1, message=message)

    with patch.object(channel.master_messages, "process_telegram_message") as process_telegram_message:
        channel.master_messages.msg(update, None)

    process_telegram_message.assert_called_once()
    args = process_telegram_message.call_args.args
    kwargs = process_telegram_message.call_args.kwargs
    assert args[2] == slave_uid
    assert kwargs["quote"] is True

    channel.db.remove_topic_assoc(slave_uid=slave_uid)


def test_master_message_ignores_forum_topic_auto_reply_without_mutating_message(channel, slave):
    topic_chat_id = TelegramChatID(80009)
    thread_id = TelegramTopicID(80010)
    slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    channel.db.remove_topic_assoc(slave_uid=slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, thread_id, slave_uid)

    reply_to_topic_starter = Mock(message_id=int(thread_id), message_thread_id=int(thread_id))
    message = _ReadOnlyReplyMessage(reply_to_topic_starter)
    message.chat.id = int(topic_chat_id)

    update = Update(update_id=3, message=message)

    with patch.object(channel.master_messages, "process_telegram_message") as process_telegram_message:
        channel.master_messages.msg(update, None)

    process_telegram_message.assert_called_once()
    kwargs = process_telegram_message.call_args.kwargs
    assert kwargs["quote"] is False

    channel.db.remove_topic_assoc(slave_uid=slave_uid)


def test_sync_reply_text_keeps_forum_topic_thread():
    bot = Mock()
    bot.send_message.return_value = Mock(message_id=2)
    message = Mock()
    message.chat.id = -100123
    message.message_id = 1
    message.message_thread_id = 456

    sync_reply_text(bot, message, "Processing...")

    bot.send_message.assert_called_once_with(-100123, text="Processing...", message_thread_id=456)


def test_master_message_ignores_unknown_forum_thread(channel, slave):
    topic_chat_id = TelegramChatID(90009)
    thread_id = TelegramTopicID(90010)
    other_slave_uid = utils.chat_id_to_str(chat=slave.chat_with_alias)
    channel.db.remove_topic_assoc(slave_uid=other_slave_uid)
    channel.db.add_topic_assoc(topic_chat_id, TelegramTopicID(42), other_slave_uid)

    message = Mock()
    message.chat = SimpleNamespace(id=int(topic_chat_id), is_forum=True)
    message.message_thread_id = int(thread_id)
    message.reply_to_message = None
    message.to_dict.return_value = {}

    update = Update(update_id=2, message=message)

    with patch.object(channel.master_messages, "process_telegram_message") as process_telegram_message:
        channel.master_messages.msg(update, None)

    process_telegram_message.assert_not_called()
    channel.db.remove_topic_assoc(slave_uid=other_slave_uid)
