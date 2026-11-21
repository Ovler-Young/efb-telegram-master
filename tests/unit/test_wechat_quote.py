import datetime
from unittest.mock import patch

import pytest
from ehforwarderbot.chat import ChatMember
from ehforwarderbot.message import Substitutions
from telegram.error import BadRequest

from efb_telegram_master.db import MsgLog
from efb_telegram_master.utils import ExperimentalFlagsManager, parse_wechat_quote
from tests.unit.test_live_aggregate import source
from tests.unit.test_live_aggregate_runtime import append, complete, runtime  # noqa: F401
from tests.unit.test_live_aggregate_source import processor

DIVIDER = "- - - - - - - - - - - - - - -"


@pytest.fixture
def manager(request):
    return request.getfixturevalue("runtime")


def handler(manager, enabled=True, thread=None):
    manager.channel.config["flags"] = {"wechat_quote_reply": enabled}
    manager.channel.flag = ExperimentalFlagsManager(manager.channel)
    result = processor(manager)
    result.get_slave_msg_dest = lambda msg: ("Alice:", (-100, thread))
    return result


def send(manager, handler, uid, text, name="Alice", members=()):
    message = source(uid, text, name)
    message.chat.members.append(message.author)
    message.chat.members.extend(members)
    handler.send_message(message)
    return message


def quote(sender, text, reply="reply"):
    return f"「@{sender} ：{text}」\n{DIVIDER}\n{reply}"


def authors(*names):
    chat = source().chat
    return [ChatMember(chat, uid=name.lower(), name=name) for name in names]


def last_send(manager):
    return manager.transport.calls[-1]


def test_flag_off_sends_the_raw_text_and_never_searches(manager):
    h = handler(manager, enabled=False)
    send(manager, h, "target", "hello there")
    complete(manager)
    with patch.object(manager.channel.db, "find_quote_sources", wraps=manager.channel.db.find_quote_sources) as spy:
        send(manager, h, "quoting", quote("Alice", "hello there"))
        complete(manager)
    assert spy.call_count == 0
    _, _, text, kwargs = last_send(manager)
    assert text == f"Alice:\n「@Alice ：hello there」\n{DIVIDER}\nreply"
    assert "reply_to_message_id" not in kwargs or kwargs["reply_to_message_id"] is None


def test_unique_match_replies_to_the_target_and_keeps_the_log_time(manager):
    h = handler(manager)
    send(manager, h, "target", "hello there")
    complete(manager)
    before = MsgLog.get(MsgLog.slave_message_id == "target").time
    send(manager, h, "quoting", quote("Alice", "hello there"), name="Bob")
    complete(manager)
    _, _, text, kwargs = last_send(manager)
    assert kwargs["reply_to_message_id"] == 1
    assert text == "Alice:\n<blockquote expandable>@Alice ：hello there</blockquote>\nreply"
    assert MsgLog.get(MsgLog.slave_message_id == "target").time == before


@pytest.mark.parametrize("age", [None, datetime.timedelta(hours=49)])
def test_no_match_keeps_the_quote_without_a_reply(manager, age):
    h = handler(manager)
    if age is not None:
        send(manager, h, "target", "hello there")
        complete(manager)
        MsgLog.update(time=datetime.datetime.now() - age).execute()
    send(manager, h, "quoting", quote("Alice", "hello there"), name="Bob")
    complete(manager)
    _, _, text, kwargs = last_send(manager)
    assert kwargs.get("reply_to_message_id") is None
    assert "<blockquote expandable>@Alice ：hello there</blockquote>\nreply" in text


def test_same_text_from_two_authors_needs_the_named_sender(manager):
    h = handler(manager)
    alice, bob = authors("Alice", "Bob")
    send(manager, h, "a", "same words", name="Alice")
    complete(manager)
    send(manager, h, "b", "same words", name="Bob")
    complete(manager)
    send(manager, h, "quote-bob", quote("Bob", "same words"), name="Carol", members=[alice, bob])
    complete(manager)
    assert last_send(manager)[3]["reply_to_message_id"] == 2
    send(manager, h, "quote-nobody", quote("Dave", "same words"), name="Carol", members=[alice, bob])
    complete(manager)
    assert last_send(manager)[3].get("reply_to_message_id") is None


def test_same_text_twice_from_one_author_is_ambiguous(manager):
    h = handler(manager)
    alice, = authors("Alice")
    for uid in ("first", "second"):
        send(manager, h, uid, "again")
        complete(manager)
    send(manager, h, "quoting", quote("Alice", "again"), name="Bob", members=[alice])
    complete(manager)
    assert last_send(manager)[3].get("reply_to_message_id") is None


def test_an_earlier_quote_does_not_hide_a_quoting_target(manager):
    h = handler(manager)
    alice, bob = authors("Alice", "Bob")
    send(manager, h, "plain", "z", name="Alice")
    complete(manager)
    # This message quotes the first one; its own text is only what follows the quote block.
    send(manager, h, "reply", quote("Alice", "z", "agreed"), name="Bob", members=[alice])
    complete(manager)
    assert last_send(manager)[3]["reply_to_message_id"] == 1
    # Quoting the reply by its own text finds it, not the first message that its quote repeats.
    send(manager, h, "third", quote("Bob", "agreed", "yes"), name="Alice", members=[bob])
    complete(manager)
    assert last_send(manager)[3]["reply_to_message_id"] == 2


def test_short_quote_still_replies_when_other_messages_contain_its_text(manager):
    h = handler(manager)
    send(manager, h, "target", "ok")
    complete(manager)
    for index in range(6):
        send(manager, h, f"other{index}", f"ok then {index}")
        complete(manager)
    send(manager, h, "quoting", quote("Alice", "ok"), name="Bob")
    complete(manager)
    assert last_send(manager)[3]["reply_to_message_id"] == 1


def test_quote_of_an_aggregated_message_replies_to_the_container(manager):
    h = handler(manager)
    now = datetime.datetime.now().timestamp()
    append(manager, "one", now, "first body")
    append(manager, "two", now + 0.1, "second body")
    complete(manager)
    container = MsgLog.get()
    assert len(container.aggregate["children"]) == 2
    send(manager, h, "quoting", quote("Alice", "second body"), name="Bob")
    complete(manager)
    assert last_send(manager)[3]["reply_to_message_id"] == 1


def test_a_target_that_is_still_queued_gets_no_reply(manager):
    h = handler(manager)
    send(manager, h, "target", "queued words")
    send(manager, h, "quoting", quote("Alice", "queued words"), name="Bob")
    complete(manager)
    complete(manager)
    assert all(call[3].get("reply_to_message_id") is None for call in manager.transport.calls)


def test_a_deleted_reply_target_does_not_lose_the_message(manager):
    h = handler(manager)
    send(manager, h, "target", "hello there")
    complete(manager)
    deliver = manager.transport.send_message

    def reject_missing_target(chat_id, text, **kwargs):
        if kwargs.get("reply_to_message_id") is not None and not kwargs.get("allow_sending_without_reply"):
            raise BadRequest("Message to be replied not found")
        return deliver(chat_id, text, **kwargs)

    manager.transport.send_message = reject_missing_target
    send(manager, h, "quoting", quote("Alice", "hello there"), name="Bob")
    complete(manager)
    _, _, text, kwargs = last_send(manager)
    assert kwargs["reply_to_message_id"] == 1
    assert text.endswith("\nreply")


def test_forum_topic_attaches_no_reply_for_a_native_reply_or_a_quote(manager):
    h = handler(manager, thread=7)
    target = send(manager, h, "target", "topic words")
    complete(manager)
    quoting = source("native", "native reply", "Bob")
    quoting.target = target
    quoting.chat.members.append(quoting.author)
    h.send_message(quoting)
    complete(manager)
    assert last_send(manager)[3].get("reply_to_message_id") is None
    send(manager, h, "quoting", quote("Alice", "topic words"), name="Bob")
    complete(manager)
    _, _, text, kwargs = last_send(manager)
    assert kwargs.get("reply_to_message_id") is None
    assert "<blockquote expandable>" in text and "topic words</blockquote>\nreply" in text


def test_mention_across_the_quote_end_leaves_plain_text(manager):
    h = handler(manager)
    author = authors("Alice")[0]
    text = quote("Alice", "hello")
    message = source("m", text)
    message.substitutions = Substitutions({(2, len(text) - 3): author})
    assert "blockquote" not in h.html_substitutions(message, wechat_quote=True)
    inside = source("n", text)
    inside.substitutions = Substitutions({(2, 7): author})
    rendered = h.html_substitutions(inside, wechat_quote=True)
    assert rendered == "<blockquote expandable>@<code>Alice</code>\u2005：hello</blockquote>\nreply"


def test_oversized_text_is_not_wrapped_in_a_quote(manager):
    h = handler(manager)
    message = source("big", quote("Alice", "x" * 4100))
    assert "blockquote" not in h.html_substitutions(message, wechat_quote=True)


def test_parse_wechat_quote():
    text = f"「@Alice ：time：3pm」\n{DIVIDER}\nok"
    quote_ = parse_wechat_quote(text)
    assert (quote_.sender, quote_.body, text[quote_.end:]) == ("Alice", "time：3pm", "ok")
    assert parse_wechat_quote(f"「private words」\n{DIVIDER}\nok").sender is None
    assert parse_wechat_quote("「no divider」\nok") is None
    assert parse_wechat_quote(f"hello\n「x」\n{DIVIDER}\nok") is None
