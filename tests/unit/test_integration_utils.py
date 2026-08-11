from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from tests.integration import utils as integration_utils
from tests.integration.helper.filters import BaseFilter


class EditedSessionFilter(BaseFilter):
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id

    def filter(self, _event) -> bool:
        return True


class EventFieldFilter(BaseFilter):
    def __init__(self, predicate) -> None:
        self.predicate = predicate

    def filter(self, event) -> bool:
        return self.predicate(event)


@pytest.mark.asyncio
async def test_start_link_waits_for_the_selected_session_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = SimpleNamespace(id=41, buttons=[[SimpleNamespace(click=None)]])
    selected.buttons[0][0].click = _async_noop
    completed = SimpleNamespace(id=41, buttons=[[SimpleNamespace(url="https://telegram.me/test?startgroup=token")]])
    helper = _StartLinkHelper(selected, completed)
    edited_calls: list[int] = []

    def edited(message_id: int) -> EditedSessionFilter:
        edited_calls.append(message_id)
        return EditedSessionFilter(message_id)

    calls = []

    async def private_response(trigger, receive):
        calls.append((trigger, receive))
        await trigger()
        return await receive(1)

    monkeypatch.setattr(integration_utils, "edited", edited)

    start_link = await integration_utils.get_start_link(SimpleNamespace(send_message=_async_noop), helper, 9001, "chat", private_response)

    assert start_link == integration_utils.StartLink("token", 41)
    assert edited_calls == [41]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_unlink_all_chats_waits_for_the_confirmed_bot_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    associations = SimpleNamespace(get_chat_assoc=Mock(return_value=[]))
    channel = cast(integration_utils.TelegramChannel, SimpleNamespace(channel_id="blueset.telegram", chat_associations=associations))
    client = SimpleNamespace(send_message=_send_unlink_command)
    helper = _UnlinkHelper()

    monkeypatch.setattr(integration_utils, "in_chats", lambda chat_id: EventFieldFilter(lambda event: event.chat_id == chat_id))
    monkeypatch.setattr(integration_utils, "reply_to", lambda message_id: EventFieldFilter(lambda event: event.reply_to_msg_id == message_id))
    monkeypatch.setattr(integration_utils, "text", EventFieldFilter(lambda event: event.is_text))

    await integration_utils.unlink_all_chats(channel, client, helper, -100500)

    assert helper.timeout == 65.0
    associations.get_chat_assoc.assert_called_once()
    assert helper.event_filter(SimpleNamespace(chat_id=-100500, reply_to_msg_id=99, is_text=True))
    assert not helper.event_filter(SimpleNamespace(chat_id=-100501, reply_to_msg_id=99, is_text=True))
    assert not helper.event_filter(SimpleNamespace(chat_id=-100500, reply_to_msg_id=98, is_text=True))
    assert not helper.event_filter(SimpleNamespace(chat_id=-100500, reply_to_msg_id=99, is_text=False))
    assert not helper.event_filter(SimpleNamespace(chat_id=-100500, reply_to_msg_id=None, is_text=True))


def test_link_chats_restores_only_captured_associations_after_a_context_failure() -> None:
    associations = _Associations({"blueset.telegram 500": ["slave old-a", "slave old-b"], "other.master": ["slave.other"]})
    channel = cast(integration_utils.TelegramChannel, SimpleNamespace(channel_id="blueset.telegram", chat_associations=associations))
    slave_chats = (SimpleNamespace(module_id="slave", uid="new-a"), SimpleNamespace(module_id="slave", uid="new-b"))

    with pytest.raises(RuntimeError, match="context failed"):
        with integration_utils.link_chats(channel, slave_chats, 500):
            associations.add_chat_assoc("blueset.telegram 500", "slave added-during-test", multiple_slave=True)
            raise RuntimeError("context failed")

    assert associations.state == {"blueset.telegram 500": ["slave old-a", "slave old-b"], "other.master": ["slave.other"]}
    assert associations.removed_masters == ["blueset.telegram 500", "blueset.telegram 500"]
    assert associations.added == [
        ("blueset.telegram 500", "slave new-a"),
        ("blueset.telegram 500", "slave new-b"),
        ("blueset.telegram 500", "slave added-during-test"),
        ("blueset.telegram 500", "slave old-a"),
        ("blueset.telegram 500", "slave old-b"),
    ]


def test_link_chats_does_not_restore_when_setup_fails() -> None:
    associations = _Associations({"blueset.telegram 500": ["slave old"]}, fail_first_removal=True)
    channel = cast(integration_utils.TelegramChannel, SimpleNamespace(channel_id="blueset.telegram", chat_associations=associations))
    slave_chats = (SimpleNamespace(module_id="slave", uid="new"),)

    with pytest.raises(RuntimeError, match="setup failed"):
        with integration_utils.link_chats(channel, slave_chats, 500):
            pytest.fail("The context body must not run after setup failure")

    assert associations.state == {"blueset.telegram 500": ["slave old"]}
    assert associations.removed_masters == ["blueset.telegram 500"]
    assert associations.added == []


async def _async_noop(*_args: object, **_kwargs: object) -> None:
    return None


async def _send_unlink_command(_chat_id: int, _command: str):
    return SimpleNamespace(id=99)


class _StartLinkHelper:
    def __init__(self, selected, completed) -> None:
        self._messages = [selected, completed]

    async def wait_for_message(self, *_args):
        return self._messages.pop(0)


class _UnlinkHelper:
    timeout: float | None = None
    event_filter = None

    async def wait_for_message(self, _event_filter, *, timeout: float):
        self.event_filter = _event_filter
        self.timeout = timeout
        return SimpleNamespace()


class _Associations:
    def __init__(self, state: dict[str, list[str]], *, fail_first_removal: bool = False) -> None:
        self.state = {master: list(slaves) for master, slaves in state.items()}
        self.removed_masters: list[str] = []
        self.added: list[tuple[str, str]] = []
        self.fail_first_removal = fail_first_removal

    def get_chat_assoc(self, *, master_uid: str) -> list[str]:
        return list(self.state.get(master_uid, []))

    def remove_chat_assoc(self, *, master_uid: str) -> None:
        self.removed_masters.append(master_uid)
        if self.fail_first_removal:
            self.fail_first_removal = False
            raise RuntimeError("setup failed")
        self.state.pop(master_uid, None)

    def add_chat_assoc(self, master_uid: str, slave_uid: str, *, multiple_slave: bool) -> None:
        assert multiple_slave
        self.added.append((master_uid, slave_uid))
        self.state.setdefault(master_uid, []).append(slave_uid)
