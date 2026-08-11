from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from efb_telegram_master.chat_destination_cache import ChatDestinationCache
from tests.integration import test_master_message_destination as destination_tests
from tests.integration import utils as integration_utils
from tests.integration.helper.filters import BaseFilter


class EventFieldFilter(BaseFilter):
    def __init__(self, predicate) -> None:
        self.predicate = predicate

    def filter(self, event) -> bool:
        return self.predicate(event)


@pytest.mark.asyncio
async def test_start_link_accepts_a_post_trigger_button_panel_without_reply_to(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = SimpleNamespace(id=41, chat_id=9001, buttons=[[SimpleNamespace(click=None)]], edited=False)
    selected.buttons[0][0].click = _async_noop
    completed = SimpleNamespace(id=41, chat_id=9001, buttons=[[SimpleNamespace(url="https://telegram.me/test?startgroup=token")]], edited=True)
    unrelated_chat = SimpleNamespace(
        id=40,
        chat_id=9002,
        buttons=[
            [SimpleNamespace(click=_async_noop)],
        ],
        edited=False,
    )
    no_button = SimpleNamespace(id=40, chat_id=9001, buttons=None, edited=False)
    helper = _QueuedHelper([unrelated_chat, no_button, selected, completed])
    edited_calls: list[int] = []

    def edited(message_id: int) -> BaseFilter:
        edited_calls.append(message_id)
        return EventFieldFilter(lambda event: event.edited and event.id == message_id)

    def reply_to(_message_id: int | None) -> BaseFilter:
        pytest.fail("start-link panels must not require reply_to")

    calls = []

    async def private_response(trigger, receive):
        calls.append((trigger, receive))
        await trigger()
        return await receive(1)

    monkeypatch.setattr(integration_utils, "in_chats", lambda chat_id: EventFieldFilter(lambda event: event.chat_id == chat_id))
    monkeypatch.setattr(integration_utils, "has_button", EventFieldFilter(lambda event: bool(event.buttons)))
    monkeypatch.setattr(integration_utils, "edited", edited)
    monkeypatch.setattr(integration_utils, "reply_to", reply_to)

    start_link = await integration_utils.get_start_link(SimpleNamespace(send_message=_send_link_command), helper, 9001, "chat", private_response)

    assert start_link == integration_utils.StartLink("token", 41)
    assert edited_calls == [41]
    assert len(calls) == 2
    assert helper.queue == [unrelated_chat, no_button]


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
    assert helper.watched == [-100500]
    assert helper.unwatched == [-100500]
    associations.get_chat_assoc.assert_called_once()
    assert helper.event_filter(SimpleNamespace(chat_id=-100500, reply_to_msg_id=99, is_text=True))
    assert not helper.event_filter(SimpleNamespace(chat_id=-100501, reply_to_msg_id=99, is_text=True))
    assert not helper.event_filter(SimpleNamespace(chat_id=-100500, reply_to_msg_id=98, is_text=True))
    assert not helper.event_filter(SimpleNamespace(chat_id=-100500, reply_to_msg_id=99, is_text=False))
    assert not helper.event_filter(SimpleNamespace(chat_id=-100500, reply_to_msg_id=None, is_text=True))


@pytest.mark.asyncio
async def test_unlink_all_chats_unwatches_after_a_reply_wait_failure() -> None:
    associations = SimpleNamespace(get_chat_assoc=Mock(return_value=[]))
    channel = cast(integration_utils.TelegramChannel, SimpleNamespace(channel_id="blueset.telegram", chat_associations=associations))
    helper = _FailingUnlinkHelper()

    with pytest.raises(RuntimeError, match="reply wait failed"):
        await integration_utils.unlink_all_chats(channel, SimpleNamespace(send_message=_send_unlink_command), helper, -100500)

    assert helper.watched == [-100500]
    assert helper.unwatched == [-100500]
    associations.get_chat_assoc.assert_not_called()


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


def test_link_chats_restores_associations_after_partial_setup_failure() -> None:
    associations = _Associations({"blueset.telegram 500": ["slave old"]}, fail_slave_uid="slave new-b")
    channel = cast(integration_utils.TelegramChannel, SimpleNamespace(channel_id="blueset.telegram", chat_associations=associations))
    slave_chats = (SimpleNamespace(module_id="slave", uid="new-a"), SimpleNamespace(module_id="slave", uid="new-b"))

    with pytest.raises(RuntimeError, match="setup failed"):
        with integration_utils.link_chats(channel, slave_chats, 500):
            pytest.fail("The context body must not run after setup failure")

    assert associations.state == {"blueset.telegram 500": ["slave old"]}


def test_expired_destination_lookup_restores_the_cache_snapshot() -> None:
    cache = ChatDestinationCache("enabled", size=3)
    cache.set("unrelated-first", "slave first")
    cache.set("expired", "slave expired")
    cache.set("unrelated-last", "slave last")
    original_weak_items = tuple(cache.weak.items())
    original_strong_entries = tuple(cache.strong)
    expired = cache.weak["expired"]
    original_expiry = expired.expiry

    with destination_tests.preserve_destination_cache(cache):
        expired.expiry = 0
        assert cache.get("expired") is None
        cache.set("created-during-test", "slave created")

    assert tuple(cache.weak.items()) == original_weak_items
    assert tuple(cache.strong) == original_strong_entries
    assert cache.weak["expired"] is expired
    assert expired.expiry == original_expiry
    assert "created-during-test" not in cache.weak


@pytest.mark.asyncio
async def test_cancel_destination_suggestion_waits_for_its_own_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    clicked = False
    calls = []
    helper = object()
    client = object()
    observed = []

    async def click() -> None:
        nonlocal clicked
        clicked = True

    async def private_response(trigger, receive, **kwargs):
        calls.append((trigger, receive, kwargs))
        await trigger()
        return await receive(1.0)

    async def wait_for_state(state_client, chat_id, message_id, expected, *, timeout):
        observed.append((state_client, chat_id, message_id, expected(SimpleNamespace(button_count=0)), timeout))

    message = SimpleNamespace(id=12, chat_id=34, button_count=1, buttons=[[SimpleNamespace(click=click)]])

    monkeypatch.setattr(destination_tests, "wait_for_message_state", wait_for_state)

    await destination_tests.cancel_destination_suggestion(client, helper, private_response, message)

    assert clicked
    assert len(calls) == 1
    assert calls[0][2] == {"target_chat_id": 34}
    assert observed == [(client, 34, 12, True, 1.0)]


@pytest.mark.asyncio
async def test_destination_suggestion_waits_for_the_clicked_prompt_state(monkeypatch: pytest.MonkeyPatch) -> None:
    clicked = False
    observed = []
    client = object()
    selected_button = SimpleNamespace(click=None)
    prompt = SimpleNamespace(id=12, chat_id=34, buttons=[[SimpleNamespace(click=_async_noop)], [selected_button]])
    sent_message = SimpleNamespace(id=77)

    async def click() -> None:
        nonlocal clicked
        clicked = True

    selected_button.click = click

    async def private_response(trigger, receive, **kwargs):
        await trigger()
        await receive(1.0)
        observed.append(kwargs)

    async def wait_for_state(state_client, chat_id, message_id, expected, *, timeout):
        matching = SimpleNamespace(reply_to_msg_id=77, raw_text="Delivering the message to Alice.", text="Delivering the message to Alice.")
        wrong_reply = SimpleNamespace(reply_to_msg_id=78, raw_text="Delivering the message to Alice.", text="Delivering the message to Alice.")
        observed.append((state_client, chat_id, message_id, expected(matching), expected(wrong_reply), timeout))

    monkeypatch.setattr(destination_tests, "wait_for_message_state", wait_for_state)

    await destination_tests.wait_for_destination_delivery(client, prompt, selected_button, sent_message, private_response)

    assert clicked
    assert observed == [(client, 34, 12, True, False, 1.0), {"target_chat_id": 34}]


async def _async_noop(*_args: object, **_kwargs: object) -> None:
    return None


async def _send_link_command(_chat_id: int, _command: str):
    return SimpleNamespace(id=99)


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

    def __init__(self) -> None:
        self.watched: list[int] = []
        self.unwatched: list[int] = []

    def watch_chat(self, chat_id: int) -> None:
        self.watched.append(chat_id)

    def unwatch_chat(self, chat_id: int) -> None:
        self.unwatched.append(chat_id)

    async def wait_for_message(self, _event_filter, *, timeout: float):
        self.event_filter = _event_filter
        self.timeout = timeout
        return SimpleNamespace()


class _FailingUnlinkHelper(_UnlinkHelper):
    async def wait_for_message(self, _event_filter, *, timeout: float):
        self.timeout = timeout
        raise RuntimeError("reply wait failed")


class _QueuedHelper:
    def __init__(self, queue) -> None:
        self.queue = queue
        self.received = None

    async def wait_for_message(self, event_filter, timeout):
        for index, event in enumerate(self.queue):
            if event_filter(event):
                self.received = self.queue.pop(index)
                return self.received
        raise AssertionError("No matching event was queued")


class _Associations:
    def __init__(self, state: dict[str, list[str]], *, fail_first_removal: bool = False, fail_slave_uid: str | None = None) -> None:
        self.state = {master: list(slaves) for master, slaves in state.items()}
        self.removed_masters: list[str] = []
        self.added: list[tuple[str, str]] = []
        self.fail_first_removal = fail_first_removal
        self.fail_slave_uid = fail_slave_uid

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
        if slave_uid == self.fail_slave_uid:
            raise RuntimeError("setup failed")
