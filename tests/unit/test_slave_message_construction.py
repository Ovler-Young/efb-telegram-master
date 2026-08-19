import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot.constants import MsgType
from ehforwarderbot.message import MessageCommand
from telegram.constants import ChatType

from efb_telegram_master.slave_message import SlaveMessageService
from efb_telegram_master.slave_message_claims import SlaveMessageClaimLifecycle


def test_lost_renewal_fences_post_send_side_effects() -> None:
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.chat_manager = Mock()
    processor.msglogs = SimpleNamespace(add_or_update_message_log=Mock())
    processor.claim_lifecycle = SlaveMessageClaimLifecycle(Mock(), processor.logger)
    processor.router = Mock(resolve_reply=Mock(return_value=None))
    processor.text_delivery = Mock(text=Mock(return_value=SimpleNamespace(chat=SimpleNamespace(id=100, type=ChatType.PRIVATE), message_id=7)))
    ownership_lost = threading.Event()
    ownership_lost.set()
    message = SimpleNamespace(uid="message", target=None, commands=[MessageCommand("Run", "run")], reactions={}, text="body", type=MsgType.Text, author=SimpleNamespace(module_id="tests.slave"))

    with patch("efb_telegram_master.slave_message.coordinator.get_module_by_id", return_value=Mock()):
        processor.dispatch_message(message, "template", None, 100, None, dedupe_key=("tests.slave chat", "message"), claim_token="claim-token", ownership_lost=ownership_lost)

    processor.claim_lifecycle.delivery_claims.complete.assert_not_called()
    processor.commands.register_command.assert_not_called()
    processor.msglogs.add_or_update_message_log.assert_not_called()
    processor.logger.warning.assert_called_once_with("[%s] Delivery claim ownership was lost before post-send processing.", "message")


def test_failed_completion_fences_command_registration_and_message_logging() -> None:
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.chat_manager = Mock()
    processor.msglogs = SimpleNamespace(add_or_update_message_log=Mock())
    processor.claim_lifecycle = SlaveMessageClaimLifecycle(Mock(complete=Mock(return_value=False)), processor.logger)
    processor.router = SimpleNamespace(resolve_reply=Mock(return_value=None), admins=[100])
    processor.text_delivery = Mock(text=Mock(return_value=SimpleNamespace(chat=SimpleNamespace(id=100, type=ChatType.PRIVATE), message_id=7)))
    message = SimpleNamespace(
        uid="message",
        target=None,
        commands=[MessageCommand("Run", "run")],
        reactions={},
        text="body",
        type=MsgType.Text,
        author=SimpleNamespace(module_id="tests.slave"),
    )

    with patch("efb_telegram_master.slave_message.coordinator.get_module_by_id", return_value=Mock()):
        processor.dispatch_message(message, "template", None, 100, None, dedupe_key=("tests.slave chat", "message"), claim_token="claim-token")

    processor.claim_lifecycle.delivery_claims.complete.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.commands.register_command.assert_not_called()
    processor.msglogs.add_or_update_message_log.assert_not_called()
    processor.logger.warning.assert_called_once_with("[%s] Delivery claim ownership was lost before completion.", "message")


def test_database_mapping_failure_still_runs_dispatch_completion() -> None:
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.chat_manager = Mock()
    processor.msglogs = SimpleNamespace(add_or_update_message_log=Mock(side_effect=RuntimeError("database unavailable")))
    processor.claim_lifecycle = SlaveMessageClaimLifecycle(Mock(), processor.logger)
    processor.router = Mock(resolve_reply=Mock(return_value=None))
    processor.text_delivery = Mock(text=Mock(return_value=SimpleNamespace(chat=SimpleNamespace(id=100), message_id=7)))
    message = SimpleNamespace(
        uid="message",
        target=None,
        commands=None,
        reactions={},
        text="body",
        type=MsgType.Text,
        author=SimpleNamespace(module_id="tests.slave"),
    )
    with patch("efb_telegram_master.slave_message.ETMMsg.from_efbmsg", return_value=Mock()), patch("efb_telegram_master.slave_message.get_msg_type", return_value="text"):
        processor.dispatch_message(message, "template", None, 100, None, dedupe_key=("tests.slave chat", "message"), claim_token="claim-token")

    processor.msglogs.add_or_update_message_log.assert_called_once()
    processor.claim_lifecycle.delivery_claims.complete.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.logger.warning.assert_called_once_with(
        "DB write failed for Telegram message %s; dropping mapping (%s).",
        7,
        "RuntimeError",
    )


def test_command_session_uses_the_telegram_message_owner() -> None:
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.chat_manager = Mock()
    processor.msglogs = Mock()
    processor.router = SimpleNamespace(resolve_reply=Mock(return_value=None), admins=[100])
    telegram_message = SimpleNamespace(chat=SimpleNamespace(id=100, type=ChatType.PRIVATE), message_id=7)
    processor.text_delivery = Mock(text=Mock(return_value=telegram_message))
    command = MessageCommand("Run", "run")
    message = SimpleNamespace(
        uid="message",
        target=None,
        commands=[command],
        reactions={},
        text="body",
        type=MsgType.Text,
        author=SimpleNamespace(module_id="tests.slave"),
    )

    with (
        patch("efb_telegram_master.slave_message.ETMMsg.from_efbmsg", return_value=Mock()),
        patch("efb_telegram_master.slave_message.get_msg_type", return_value="text"),
        patch("efb_telegram_master.slave_message.coordinator.get_module_by_id", return_value=Mock()),
    ):
        processor.dispatch_message(message, "template", None, 100, None)

    storage = processor.commands.register_command.call_args.args[1]
    assert storage.authorized_user_ids == frozenset((100,))


def test_command_session_in_group_allows_configured_admins() -> None:
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.chat_manager = Mock()
    processor.msglogs = Mock()
    processor.router = SimpleNamespace(resolve_reply=Mock(return_value=None), admins=[100])
    telegram_message = SimpleNamespace(chat=SimpleNamespace(id=-100500, type=ChatType.SUPERGROUP), message_id=7)
    processor.text_delivery = Mock(text=Mock(return_value=telegram_message))
    command = MessageCommand("Run", "run")
    message = SimpleNamespace(
        uid="message",
        target=None,
        commands=[command],
        reactions={},
        text="body",
        type=MsgType.Text,
        author=SimpleNamespace(module_id="tests.slave"),
    )

    with (
        patch("efb_telegram_master.slave_message.ETMMsg.from_efbmsg", return_value=Mock()),
        patch("efb_telegram_master.slave_message.get_msg_type", return_value="text"),
        patch("efb_telegram_master.slave_message.coordinator.get_module_by_id", return_value=Mock()),
    ):
        processor.dispatch_message(message, "template", None, -100500, None)

    storage = processor.commands.register_command.call_args.args[1]
    assert storage.authorized_user_ids == frozenset((100,))
