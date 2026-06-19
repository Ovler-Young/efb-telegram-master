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
