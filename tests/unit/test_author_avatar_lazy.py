import io
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from efb_telegram_master.chat_binding import ChatBindingManager


class _DummyUserEmojiCache:
    def __init__(self):
        self.rows = {}

    def get_user_emoji_cache(self, key):
        return self.rows.get(key)

    def set_user_emoji_cache(self, key, custom_emoji_id, sticker_set_name):
        self.rows[key] = (custom_emoji_id, sticker_set_name)


def _png_bytes(color=(255, 0, 0, 255)):
    out = io.BytesIO()
    Image.new("RGBA", (64, 64), color).save(out, "PNG")
    out.seek(0)
    return out


def _manager():
    manager = ChatBindingManager.__new__(ChatBindingManager)
    manager.channel = SimpleNamespace(_=lambda text: text)
    manager.db = _DummyUserEmojiCache()
    manager.bot = Mock()
    manager.logger = logging.getLogger("test_user_avatar_lazy")
    manager._user_emoji_unavailable = False
    manager._user_emoji_lock = threading.Lock()
    manager._user_emoji_inflight = set()
    manager._get_configured_user_custom_emoji_id = Mock(return_value=None)
    manager._is_user_emoji_enabled = Mock(return_value=True)
    manager._log_user_emoji = Mock()
    return manager


def test_user_avatar_lazy_cache_hit_does_not_load_picture():
    manager = _manager()
    user_key = manager._user_avatar_cache_key("slave user")
    manager.db.set_user_emoji_cache(user_key, "12345", "set_by_bot")
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded"))

    assert manager.resolve_user_avatar_custom_emoji_id_lazy("slave user", load_picture) == "12345"
    load_picture.assert_not_called()


def test_user_avatar_lazy_cache_miss_starts_background_create_without_loading_picture():
    manager = _manager()
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded"))
    thread = Mock()

    with patch("efb_telegram_master.chat_binding.threading.Thread", return_value=thread) as thread_cls:
        assert manager.resolve_user_avatar_custom_emoji_id_lazy("slave user", load_picture) is None

    thread_cls.assert_called_once()
    thread.start.assert_called_once()
    load_picture.assert_not_called()


def test_user_avatar_lazy_disabled_does_not_generate():
    manager = _manager()
    manager._is_user_emoji_enabled = Mock(return_value=False)
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded when disabled"))

    with patch("efb_telegram_master.chat_binding.threading.Thread") as thread_cls:
        assert manager.resolve_user_avatar_custom_emoji_id_lazy("slave user", load_picture) is None

    thread_cls.assert_not_called()
    load_picture.assert_not_called()


def test_user_avatar_lazy_configured_custom_emoji_takes_precedence_when_disabled():
    manager = _manager()
    manager._is_user_emoji_enabled = Mock(return_value=False)
    manager._get_configured_user_custom_emoji_id = Mock(return_value="emoji-configured")
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded for configured emoji"))

    with patch("efb_telegram_master.chat_binding.threading.Thread") as thread_cls:
        assert manager.resolve_user_avatar_custom_emoji_id_lazy("slave user", load_picture) == "emoji-configured"

    thread_cls.assert_not_called()
    load_picture.assert_not_called()


def test_user_avatar_lazy_inflight_user_does_not_start_duplicate_background_create():
    manager = _manager()
    manager._user_emoji_inflight.add(manager._user_avatar_cache_key("slave user"))
    load_picture = Mock(side_effect=AssertionError("avatar should only be loaded in background"))

    with patch("efb_telegram_master.chat_binding.threading.Thread") as thread_cls:
        assert manager.resolve_user_avatar_custom_emoji_id_lazy("slave user", load_picture) is None

    thread_cls.assert_not_called()
    load_picture.assert_not_called()


def test_user_avatar_background_create_writes_user_cache():
    manager = _manager()
    manager._get_or_create_user_avatar_custom_emoji_entry = Mock(return_value=("12345", "set_by_bot"))

    manager._load_user_avatar_custom_emoji(
        "slave user",
        manager._user_avatar_cache_key("slave user"),
        Mock(return_value=(_png_bytes(), "member")),
        "msg-1",
    )

    manager._get_or_create_user_avatar_custom_emoji_entry.assert_called_once()
    assert manager.db.get_user_emoji_cache(manager._user_avatar_cache_key("slave user")) == (
        "12345",
        "set_by_bot",
    )


def test_user_avatar_background_create_clears_inflight_after_success():
    manager = _manager()
    user_key = manager._user_avatar_cache_key("slave user")
    manager._user_emoji_inflight.add(user_key)
    manager._get_or_create_user_avatar_custom_emoji_entry = Mock(return_value=("12345", "set_by_bot"))

    manager._load_user_avatar_custom_emoji(
        "slave user",
        user_key,
        Mock(return_value=(_png_bytes(), "member")),
        "msg-1",
    )

    assert user_key not in manager._user_emoji_inflight


def test_user_avatar_lazy_returns_cache_after_background_update():
    manager = _manager()
    user_key = manager._user_avatar_cache_key("slave user")
    manager.db.set_user_emoji_cache(user_key, "12345", "set_by_bot")
    load_picture = Mock(side_effect=AssertionError("avatar should not be loaded after cache update"))

    assert manager.resolve_user_avatar_custom_emoji_id_lazy("slave user", load_picture) == "12345"
    load_picture.assert_not_called()


def test_user_avatar_background_create_clears_inflight_after_failure():
    manager = _manager()
    user_key = manager._user_avatar_cache_key("slave user")
    manager._user_emoji_inflight.add(user_key)
    manager._get_or_create_user_avatar_custom_emoji_entry = Mock(side_effect=RuntimeError("telegram down"))

    manager._load_user_avatar_custom_emoji(
        "slave user",
        user_key,
        Mock(return_value=(_png_bytes(), "member")),
        "msg-1",
    )

    assert user_key not in manager._user_emoji_inflight
