import asyncio
from types import SimpleNamespace

from efb_telegram_master.ptb_compat import (
    ConversationHandler,
    build_request_kwargs,
    forwarded_from_chat,
    run_sync,
    wrap_callback,
)


def test_run_sync_resolves_coroutine():
    async def value():
        return "ok"

    assert run_sync(value()) == "ok"


def test_run_sync_resolves_coroutine_from_running_loop():
    async def value():
        return "ok"

    async def caller():
        return run_sync(value())

    assert asyncio.run(caller()) == "ok"


def test_build_request_kwargs_maps_ptb13_proxy_names():
    result = build_request_kwargs({
        "proxy_url": "socks5://127.0.0.1:1080",
        "urllib3_proxy_kwargs": {
            "username": "user",
            "password": "pass",
        },
        "read_timeout": 15,
    })

    assert result == {
        "proxy": "socks5://user:pass@127.0.0.1:1080",
        "read_timeout": 15,
    }


def test_build_request_kwargs_maps_http_proxy_auth_and_drops_unsupported_keys():
    result = build_request_kwargs({
        "proxy_url": "http://127.0.0.1:8080/",
        "username": "user",
        "password": "pass",
        "urllib3_proxy_kwargs": {"unused": True},
        "read_timeout": 15,
    })

    assert result == {
        "proxy": "http://user:pass@127.0.0.1:8080/",
        "read_timeout": 15,
    }


def test_build_request_kwargs_maps_socks_proxy_auth():
    result = build_request_kwargs({
        "proxy_url": "socks5://127.0.0.1:1080",
        "urllib3_proxy_kwargs": {
            "username": "user",
            "password": "pass",
        },
    })

    assert result == {
        "proxy": "socks5://user:pass@127.0.0.1:1080",
    }


def test_forwarded_from_chat_reads_ptb22_forward_origin_chat():
    chat = SimpleNamespace(id=-100123)
    message = SimpleNamespace(forward_origin=SimpleNamespace(chat=chat), forward_from_chat=None)

    assert forwarded_from_chat(message) is chat


def test_forwarded_from_chat_keeps_legacy_forward_from_chat_fallback():
    chat = SimpleNamespace(id=-100123)
    message = SimpleNamespace(forward_from_chat=chat)

    assert forwarded_from_chat(message) is chat


def test_wrap_callback_exposes_sync_message_methods():
    calls = []

    class Message:
        async def reply_text(self, text):
            calls.append(text)

    class Update:
        callback_query = None
        effective_message = Message()

    def callback(update, _context):
        update.effective_message.reply_text("hello")
        return "done"

    context = SimpleNamespace(bot=SimpleNamespace())

    assert asyncio.run(wrap_callback(callback)(Update(), context)) == "done"
    assert calls == ["hello"]


def test_wrap_callback_exposes_sync_update_message_methods():
    calls = []

    class Message:
        def __init__(self, label):
            self.label = label

        async def reply_text(self, text):
            calls.append((self.label, text))

    class Update:
        callback_query = None
        message = Message("message")
        edited_message = Message("edited_message")
        channel_post = Message("channel_post")
        edited_channel_post = Message("edited_channel_post")
        effective_message = message

    def callback(update, _context):
        update.message.reply_text("hello")
        update.edited_message.reply_text("edited")
        update.channel_post.reply_text("channel")
        update.edited_channel_post.reply_text("edited channel")
        return "done"

    context = SimpleNamespace(bot=SimpleNamespace())

    assert asyncio.run(wrap_callback(callback)(Update(), context)) == "done"
    assert calls == [
        ("message", "hello"),
        ("edited_message", "edited"),
        ("channel_post", "channel"),
        ("edited_channel_post", "edited channel"),
    ]


def test_wrap_callback_maps_reply_quote_argument():
    calls = []

    class Message:
        async def reply_text(self, text, **kwargs):
            calls.append((text, kwargs))

    class Update:
        callback_query = None
        effective_message = Message()

    def callback(update, _context):
        update.effective_message.reply_text("hello", quote=True)

    context = SimpleNamespace(bot=SimpleNamespace())

    asyncio.run(wrap_callback(callback)(Update(), context))
    assert calls == [("hello", {"do_quote": True})]


def test_conversation_handler_wraps_handler_callbacks(monkeypatch):
    wrapped = []
    captured = {}

    def entry_callback(_update, _context):
        return "state"

    def state_callback(_update, _context):
        return "state"

    def fallback_callback(_update, _context):
        return "fallback"

    entry_handler = SimpleNamespace(callback=entry_callback)
    state_handler = SimpleNamespace(callback=state_callback)
    fallback_handler = SimpleNamespace(callback=fallback_callback)

    def fake_init(self, entry_points, states, fallbacks, **kwargs):
        captured["entry_points"] = entry_points
        captured["states"] = states
        captured["fallbacks"] = fallbacks
        captured["kwargs"] = kwargs

    monkeypatch.setattr(ConversationHandler.__mro__[1], "__init__", fake_init)

    def fake_wrap(callback):
        wrapped.append(callback)
        return SimpleNamespace(wrapped=callback)

    monkeypatch.setattr("efb_telegram_master.ptb_compat.wrap_callback", fake_wrap)

    ConversationHandler(
        entry_points=[entry_handler],
        states={"state": [state_handler]},
        fallbacks=[fallback_handler],
        per_chat=True,
    )

    assert captured == {
        "entry_points": [entry_handler],
        "states": {"state": [state_handler]},
        "fallbacks": [fallback_handler],
        "kwargs": {"per_chat": True},
    }
    assert entry_handler.callback.wrapped is entry_callback
    assert state_handler.callback.wrapped is state_callback
    assert fallback_handler.callback.wrapped is fallback_callback
    assert wrapped == [entry_callback, state_callback, fallback_callback]
