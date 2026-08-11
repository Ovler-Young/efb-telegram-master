from telethon.events import MessageEdited, NewMessage

from tests.integration.helper import filters


class DummyNewMessageEvent(NewMessage.Event):
    pass


class DummyEditedMessageEvent(MessageEdited.Event):
    pass


def test_message_filter_accepts_new_message_events():
    event = object.__new__(DummyNewMessageEvent)

    assert filters.message(event)


def test_message_filter_accepts_edited_message_events():
    event = object.__new__(DummyEditedMessageEvent)

    assert filters.message(event)


def test_edited_filter_rejects_a_new_message_event_for_the_same_message_id():
    event = object.__new__(DummyNewMessageEvent)
    event.__dict__["_init"] = False
    event.message = type("Message", (), {"id": 42})()

    assert not filters.edited(42)(event)
    assert not filters.edited(43)(event)


def test_edited_filter_accepts_an_edited_message_event_with_the_expected_message_id():
    event = object.__new__(DummyEditedMessageEvent)
    event.__dict__["_init"] = False
    event.message = type("Message", (), {"id": 42})()

    assert filters.edited(42)(event)
    assert not filters.edited(43)(event)
