import pytest
from telethon.events import MessageEdited, NewMessage

from tests.integration.helper.filter_messages import edited, message


class DummyNewMessageEvent(NewMessage.Event):
    pass


class DummyEditedMessageEvent(MessageEdited.Event):
    pass


@pytest.mark.parametrize("event_type", [DummyNewMessageEvent, DummyEditedMessageEvent])
def test_message_filter_accepts_new_and_edited_message_events(event_type):
    event = object.__new__(event_type)

    assert message(event)


def test_edited_filter_rejects_a_new_message_event_for_the_same_message_id():
    event = object.__new__(DummyNewMessageEvent)
    event.__dict__["_init"] = False
    event.message = type("Message", (), {"id": 42})()

    assert not edited(42)(event)
    assert not edited(43)(event)


def test_edited_filter_accepts_an_edited_message_event_with_the_expected_message_id():
    event = object.__new__(DummyEditedMessageEvent)
    event.__dict__["_init"] = False
    event.message = type("Message", (), {"id": 42})()

    assert edited(42)(event)
    assert not edited(43)(event)
