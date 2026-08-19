from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot.constants import MsgType
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from efb_telegram_master.slave_delivery_types import DeliveryPlan
from efb_telegram_master.slave_status import SlaveStatusService


@pytest.mark.parametrize(
    ("delivered_to", "expected"),
    [("blueset.telegram", "100.10"), ("tests.slave", "100.11")],
)
def test_reaction_target_selects_primary_or_bot_reply(delivered_to, expected) -> None:
    message = SimpleNamespace(type=MsgType.Text, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id=delivered_to))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11")
    assert SlaveStatusService.reaction_target_message_id(message, row) == expected


def _reaction_processor(row, message, *, side_effect=None):
    service = object.__new__(SlaveStatusService)
    service.REACTION_DB_WAIT_TIMEOUT = 0
    service.REACTION_DB_WAIT_INTERVAL = 0
    service.chat_manager = Mock()
    service.router = Mock(route=Mock(return_value=DeliveryPlan("template", 100, None)))
    service.logger = Mock()
    service.msglogs = SimpleNamespace(get_msg_log=Mock(return_value=row))
    service.message_reconstructor = Mock(build=Mock(return_value=message))
    service.reaction_dispatcher = Mock(dispatch_message=Mock(side_effect=side_effect))
    return service, SimpleNamespace(chat=SimpleNamespace(module_id="tests.slave", uid="chat"), msg_id="message", reactions={"R": [object()]})


def test_reaction_from_telegram_origin_creates_bot_reply_to_user_message() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt=None, sender_bot_id=None, build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)
    processor.update_reactions(status)
    processor.reaction_dispatcher.dispatch_message.assert_called_once_with(message, "template", None, 100, None, database_old_msg_id=(100, 10), target_msg_id_override=10)


def test_reaction_update_waits_for_message_log_write() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="blueset.telegram"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt=None, sender_bot_id=None, build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)
    processor.REACTION_DB_WAIT_TIMEOUT = 0.1
    processor.REACTION_DB_WAIT_INTERVAL = 0
    processor.msglogs.get_msg_log.side_effect = [None, row]

    processor.update_reactions(status)

    assert processor.msglogs.get_msg_log.call_count == 2
    processor.reaction_dispatcher.dispatch_message.assert_called_once_with(message, "template", (100, 10), 100, None)


def test_reaction_update_edits_existing_bot_reply_and_persists_sender() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11", sender_bot_id="777", build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)
    processor.update_reactions(status)
    processor.reaction_dispatcher.dispatch_message.assert_called_once_with(message, "template", (100, 11), 100, None)
    assert message.vendor_specific == {"_sender_bot_id": "777"}


def test_missing_reaction_edit_target_creates_replacement_reply() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11", sender_bot_id="777", build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message, side_effect=[BadRequest("Message to edit not found"), None])
    processor.update_reactions(status)
    assert processor.reaction_dispatcher.dispatch_message.call_args_list[1].args == (message, "template", None, 100, None)
    assert processor.reaction_dispatcher.dispatch_message.call_args_list[1].kwargs == {"database_old_msg_id": (100, 11), "target_msg_id_override": 10}


@pytest.mark.parametrize("error", [BadRequest("Not enough rights"), RetryAfter(1), NetworkError("transport"), TelegramError("other")])
def test_nonmissing_reaction_edit_errors_are_propagated(error) -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11", sender_bot_id=None, build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message, side_effect=error)
    with pytest.raises(type(error)):
        processor.update_reactions(status)


def test_reaction_retries_bot_reply_until_database_records_alternate() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt=None, sender_bot_id=None, build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)

    def record_alternate(*_args, **_kwargs):
        if processor.reaction_dispatcher.dispatch_message.call_count == 2:
            row.master_msg_id_alt = "100.12"
            row.sender_bot_id = "888"

    processor.reaction_dispatcher.dispatch_message.side_effect = record_alternate
    for reaction in ("R0", "R1", "R2"):
        status.reactions = {reaction: [object()]}
        processor.update_reactions(status)

    assert [call.args[2] for call in processor.reaction_dispatcher.dispatch_message.call_args_list] == [None, None, (100, 12)]


def test_reaction_retries_missing_alternate_until_replacement_is_recorded() -> None:
    message = SimpleNamespace(type=MsgType.Text, reactions={}, vendor_specific=None, chat=SimpleNamespace(module_id="tests.slave"), deliver_to=SimpleNamespace(channel_id="tests.slave"))
    row = SimpleNamespace(master_msg_id="100.10", master_msg_id_alt="100.11", sender_bot_id="777", build_etm_msg=Mock(return_value=message))
    processor, status = _reaction_processor(row, message)
    replies = 0

    def replace_after_second_reply(_message, _template, old_msg_id, *_args, **_kwargs):
        nonlocal replies
        if old_msg_id == (100, 11):
            raise BadRequest("Message to edit not found")
        replies += 1
        if replies == 2:
            row.master_msg_id_alt = "100.13"
            row.sender_bot_id = "999"

    processor.reaction_dispatcher.dispatch_message.side_effect = replace_after_second_reply
    for reaction in ("R0", "R1", "R2"):
        status.reactions = {reaction: [object()]}
        processor.update_reactions(status)

    assert [call.args[2] for call in processor.reaction_dispatcher.dispatch_message.call_args_list] == [(100, 11), None, (100, 11), None, (100, 13)]
