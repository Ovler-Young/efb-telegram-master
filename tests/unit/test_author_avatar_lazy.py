import io
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from efb_telegram_master.chat_binding import ChatBindingManager


class _DummyTopicIconCache:
    def __init__(self):
        self.rows = {}

    def get_topic_icon_cache(self, key):
        return self.rows.get(key)

    def set_topic_icon_cache(self, key, custom_emoji_id, sticker_set_name):
        self.rows[key] = (custom_emoji_id, sticker_set_name)

    def get_topic_icon_cache_custom_emoji_ids(self, key_prefix=None):
        return {
            custom_emoji_id
            for key, (custom_emoji_id, _) in self.rows.items()
            if key_prefix is None or key.startswith(key_prefix)
        }

    def get_topic_icon_cache_entries(self, key_prefix=None):
        return [
            (key, custom_emoji_id, sticker_set_name)
            for key, (custom_emoji_id, sticker_set_name) in self.rows.items()
            if key_prefix is None or key.startswith(key_prefix)
        ]


def _png_bytes(color=(255, 0, 0, 255)):
    out = io.BytesIO()
    Image.new("RGBA", (64, 64), color).save(out, "PNG")
    out.seek(0)
    return out


def _manager():
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.db = _DummyTopicIconCache()
    manager.bot = SimpleNamespace(replace_sticker_in_set=Mock(return_value=True))
    manager.logger = logging.getLogger("test_author_avatar_lazy")
    manager._topic_icon_custom_emoji_unavailable = False
    manager._author_avatar_placeholder_lock = threading.Lock()
    manager._author_avatar_placeholder_ids = {}
    manager._author_avatar_placeholder_prepopulate_running = False
    manager._author_avatar_inflight = set()
    manager._author_avatar_pending = {}
    manager._get_configured_topic_icon_custom_emoji_id = Mock(return_value=None)
    manager._get_topic_icon_owner_user_id = Mock(return_value=99)
    manager._log_topic_icon_custom_emoji = Mock()
    return manager


def test_author_avatar_lazy_cache_hit_does_not_load_picture():
    manager = _manager()
    user_key = manager._author_avatar_cache_key("slave user")
    manager.db.set_topic_icon_cache(user_key, "12345", "set_by_bot")
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded"))

    assert manager.resolve_author_avatar_custom_emoji_id_lazy("slave user", load_picture) == "12345"
    load_picture.assert_not_called()


def test_author_avatar_lazy_empty_pool_starts_background_fill_without_loading_picture():
    manager = _manager()
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded"))

    with patch.object(manager, "_ensure_author_avatar_placeholder_pool_async_locked") as ensure_pool:
        assert manager.resolve_author_avatar_custom_emoji_id_lazy("slave user", load_picture) is None

    ensure_pool.assert_called_once()
    load_picture.assert_not_called()


def test_author_avatar_lazy_uses_available_placeholder_in_background_without_returning_emoji():
    manager = _manager()
    manager.db.set_topic_icon_cache(
        "member-placeholder:set_by_bot:12345",
        "12345",
        "set_by_bot",
    )
    load_picture = Mock(side_effect=AssertionError("avatar should only be loaded in background"))
    thread = Mock()

    with patch.object(manager, "_ensure_author_avatar_placeholder_pool_async_locked") as ensure_pool, \
         patch("efb_telegram_master.chat_binding.threading.Thread", return_value=thread) as thread_cls:
        assert manager.resolve_author_avatar_custom_emoji_id_lazy("slave user", load_picture) is None

    ensure_pool.assert_called_once()
    thread_cls.assert_called_once()
    thread.start.assert_called_once()
    load_picture.assert_not_called()


def test_author_avatar_replace_writes_user_cache():
    manager = _manager()
    old_sticker = SimpleNamespace(custom_emoji_id="12345")
    manager._find_custom_emoji_sticker_entry = Mock(return_value=(old_sticker, 0))
    manager._custom_emoji_id_after_replace = Mock(return_value="12345")

    manager._replace_author_avatar_placeholder(
        "slave user",
        manager._author_avatar_cache_key("slave user"),
        "12345",
        "set_by_bot",
        Mock(return_value=(_png_bytes(), "member")),
        "msg-1",
    )

    manager.bot.replace_sticker_in_set.assert_called_once()
    assert manager.db.get_topic_icon_cache(manager._author_avatar_cache_key("slave user")) == (
        "12345",
        "set_by_bot",
    )


def test_author_avatar_replace_caches_new_custom_emoji_id_when_replace_changes_it():
    manager = _manager()
    old_sticker = SimpleNamespace(custom_emoji_id="12345")
    manager._find_custom_emoji_sticker_entry = Mock(return_value=(old_sticker, 0))
    manager._custom_emoji_id_after_replace = Mock(return_value="67890")

    manager._replace_author_avatar_placeholder(
        "slave user",
        manager._author_avatar_cache_key("slave user"),
        "12345",
        "set_by_bot",
        Mock(return_value=(_png_bytes(), "member")),
        "msg-1",
    )

    assert manager.db.get_topic_icon_cache(manager._author_avatar_cache_key("slave user")) == (
        "67890",
        "set_by_bot",
    )


def test_author_avatar_lazy_returns_cache_after_background_update():
    manager = _manager()
    user_key = manager._author_avatar_cache_key("slave user")
    manager.db.set_topic_icon_cache(user_key, "12345", "set_by_bot")
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded after cache update"))

    assert manager.resolve_author_avatar_custom_emoji_id_lazy("slave user", load_picture) == "12345"
    load_picture.assert_not_called()


def test_author_avatar_replace_success_without_cache_keeps_placeholder_reserved():
    manager = _manager()
    user_key = manager._author_avatar_cache_key("slave user")
    manager._author_avatar_pending[user_key] = ("12345", "set_by_bot")
    manager._author_avatar_inflight.add(user_key)
    old_sticker = SimpleNamespace(custom_emoji_id="12345")
    manager._find_custom_emoji_sticker_entry = Mock(return_value=(old_sticker, 0))
    manager._custom_emoji_id_after_replace = Mock(return_value="12345")
    manager.db.set_topic_icon_cache = Mock(side_effect=RuntimeError("db down"))

    manager._replace_author_avatar_placeholder(
        "slave user",
        user_key,
        "12345",
        "set_by_bot",
        Mock(return_value=(_png_bytes(), "member")),
        "msg-1",
    )

    assert manager._author_avatar_pending[user_key] == ("12345", "set_by_bot")
    assert user_key in manager._author_avatar_inflight
