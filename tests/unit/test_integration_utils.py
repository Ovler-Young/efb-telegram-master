from types import SimpleNamespace

import pytest

from tests.integration import utils as integration_utils
from tests.integration.helper.filters import BaseFilter


class EditedSessionFilter(BaseFilter):
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id

    def filter(self, _event) -> bool:
        return True


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

    async def private_response(trigger, receive):
        await trigger()
        return await receive(1)

    monkeypatch.setattr(integration_utils, "edited", edited)

    start_link = await integration_utils.get_start_link(SimpleNamespace(send_message=_async_noop), helper, 9001, "chat", private_response)

    assert start_link == integration_utils.StartLink("token", 41)
    assert edited_calls == [41]


async def _async_noop(*_args: object, **_kwargs: object) -> None:
    return None


class _StartLinkHelper:
    def __init__(self, selected, completed) -> None:
        self._messages = [selected, completed]

    async def wait_for_message(self, *_args):
        return self._messages.pop(0)
