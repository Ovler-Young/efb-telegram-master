import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot.constants import MsgType
from ehforwarderbot.message import MessageCommand
from telegram.constants import ChatType

from efb_telegram_master import TelegramChannel
from efb_telegram_master.slave_delivery_helpers import send_identity
from efb_telegram_master.slave_delivery_types import DeliveryPlan
from efb_telegram_master.slave_message import SlaveMessageService
from efb_telegram_master.slave_routing import SlaveMessageRouter


def _message(uid="message"):
    return SimpleNamespace(
        uid=uid,
        edit=False,
        type=MsgType.Text,
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
    )


def _dedupe_processor() -> SlaveMessageService:
    processor = object.__new__(SlaveMessageService)
    processor.msglogs = Mock()
    processor.logger = Mock()
    processor.router = Mock(route=Mock(return_value=DeliveryPlan("template", 123, None)))
    processor.is_silent = Mock(return_value=False)
    processor.dispatch_message = Mock()
    processor.delivery_claims = Mock()
    processor.delivery_claims.claim.return_value = "claim-token"
    return processor


def test_channel_stopping_gate_drops_messages_and_statuses() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.message_service = Mock()
    channel.status_service = Mock()
    channel.bot_manager = SimpleNamespace(_stopping=False)
    channel._stop_polling_called = True

    message = SimpleNamespace(uid="late")
    assert TelegramChannel.send_message(channel, message) is message
    assert TelegramChannel.send_status(channel, SimpleNamespace()) is None
    channel.message_service.send_message.assert_not_called()
    channel.status_service.send_status.assert_not_called()


def test_channel_manager_stopping_gate_drops_message() -> None:
    channel = TelegramChannel.__new__(TelegramChannel)
    channel.message_service = Mock()
    channel.bot_manager = SimpleNamespace(_stopping=True)
    channel._stop_polling_called = False

    message = SimpleNamespace(uid="late")
    assert TelegramChannel.send_message(channel, message) is message
    channel.message_service.send_message.assert_not_called()


def test_forum_destination_uses_cached_chat_info_until_ttl() -> None:
    processor = object.__new__(SlaveMessageRouter)
    chat_uid = "tests.slave chat"
    tg_chat = "telegram -100123"

    def get_chat_assoc(*, slave_uid=None, master_uid=None):
        return [tg_chat] if slave_uid == chat_uid else [chat_uid] if master_uid == tg_chat else []

    processor.admins = [1]
    processor.topic_group = -100999
    processor.topic_sync = SimpleNamespace(create_topic=Mock(return_value=55))
    processor.bot = SimpleNamespace(get_chat_info=Mock(return_value=SimpleNamespace(is_forum=True)))
    processor.db = SimpleNamespace()
    processor.chat_associations = SimpleNamespace(get_chat_assoc=Mock(side_effect=get_chat_assoc), get_topic_thread_id=Mock(return_value=55))
    processor.chat_manager = SimpleNamespace(update_chat_obj=lambda chat: chat, get_or_enrol_member=lambda chat, author: author)
    processor.chat_dest_cache = SimpleNamespace(get=Mock(return_value=chat_uid), remove=Mock())
    processor.generate_message_template = Mock(return_value="template")
    processor.logger = Mock()
    processor._known_forum_chat_ids = {}
    processor._known_forum_chat_ids_lock = threading.Lock()

    first = SimpleNamespace(uid="one", chat=SimpleNamespace(module_id="tests.slave", uid="chat"), author=SimpleNamespace())
    second = SimpleNamespace(uid="two", chat=SimpleNamespace(module_id="tests.slave", uid="chat"), author=SimpleNamespace())
    assert processor.route(first).destination == -100123
    assert processor.route(second).thread_id == 55
    processor.bot.get_chat_info.assert_called_once_with(-100123)

    processor._known_forum_chat_ids[-100123] = time.monotonic() - processor.FORUM_CHAT_CACHE_TTL - 1
    assert processor.route(second).thread_id == 55
    assert processor.bot.get_chat_info.call_count == 2


def test_new_slave_message_claims_durable_dedupe_without_msglog_lookup() -> None:
    processor = _dedupe_processor()
    message = _message()

    assert processor.send_message(message) is message
    processor.delivery_claims.claim.assert_called_once_with("tests.slave chat", "message")
    processor.msglogs.get_msg_log.assert_not_called()
    processor.dispatch_message.assert_called_once()
    assert processor.dispatch_message.call_args.args == (message, "template", None, 123, None, False)
    assert processor.dispatch_message.call_args.kwargs["dedupe_key"] == ("tests.slave chat", "message")
    assert processor.dispatch_message.call_args.kwargs["claim_token"] == "claim-token"
    assert not processor.dispatch_message.call_args.kwargs["ownership_lost"].is_set()


def test_pending_duplicate_and_muted_message_do_not_dispatch() -> None:
    processor = _dedupe_processor()
    processor.delivery_claims.claim.return_value = None
    assert processor.send_message(_message()) is not None
    processor.dispatch_message.assert_not_called()

    processor = _dedupe_processor()
    processor.is_silent.return_value = None
    assert processor.send_message(_message()) is not None
    processor.delivery_claims.release.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.dispatch_message.assert_not_called()


def test_destination_mapping_failure_releases_the_pending_dedupe_claim() -> None:
    processor = _dedupe_processor()
    processor.router.route.side_effect = RuntimeError("database unavailable")

    assert processor.send_message(_message()) is not None
    processor.delivery_claims.release.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.dispatch_message.assert_not_called()


def test_terminal_delivery_failure_releases_the_dedupe_claim_without_completing_it() -> None:
    processor = _dedupe_processor()
    processor.dispatch_message.side_effect = ValueError("attachment failed")

    assert processor.send_message(_message()) is not None
    processor.delivery_claims.release.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.delivery_claims.complete.assert_not_called()


def test_active_delivery_renews_the_owned_claim() -> None:
    processor = _dedupe_processor()
    processor.CLAIM_RENEW_INTERVAL = 0.01
    started, release = threading.Event(), threading.Event()

    def dispatch(*_args, **_kwargs):
        started.set()
        assert release.wait(1)

    processor.dispatch_message.side_effect = dispatch
    worker = threading.Thread(target=processor.send_message, args=(_message(),))
    worker.start()
    try:
        assert started.wait(1)
        deadline = time.monotonic() + 1
        while not processor.delivery_claims.renew.called and time.monotonic() < deadline:
            time.sleep(0.01)
        processor.delivery_claims.renew.assert_called_with("tests.slave chat", "message", "claim-token")
    finally:
        release.set()
        worker.join(1)


def test_renewal_exception_fences_post_send_side_effects() -> None:
    processor = _dedupe_processor()
    processor.CLAIM_RENEW_INTERVAL = 0.01
    processor.delivery_claims.renew.side_effect = RuntimeError("database unavailable")
    started, release = threading.Event(), threading.Event()

    def dispatch(*_args, **_kwargs):
        started.set()
        assert release.wait(1)

    processor.dispatch_message.side_effect = dispatch
    worker = threading.Thread(target=processor.send_message, args=(_message(),))
    worker.start()
    try:
        assert started.wait(1)
        ownership_lost = processor.dispatch_message.call_args.kwargs["ownership_lost"]
        deadline = time.monotonic() + 1
        while not ownership_lost.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ownership_lost.is_set()
        processor.logger.exception.assert_called_once_with("Failed to renew delivery claim (%s).", "RuntimeError")
    finally:
        release.set()
        worker.join(1)


def test_lost_renewal_fences_post_send_side_effects() -> None:
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.chat_manager = Mock()
    processor.msglogs = SimpleNamespace(add_or_update_message_log=Mock())
    processor.delivery_claims = Mock()
    processor.router = Mock(resolve_reply=Mock(return_value=None))
    processor.text_delivery = Mock(text=Mock(return_value=SimpleNamespace(chat=SimpleNamespace(id=100, type=ChatType.PRIVATE), message_id=7)))
    ownership_lost = threading.Event()
    ownership_lost.set()
    message = SimpleNamespace(uid="message", target=None, commands=[MessageCommand("Run", "run")], reactions={}, text="body", type=MsgType.Text, author=SimpleNamespace(module_id="tests.slave"))

    with patch("efb_telegram_master.slave_message.coordinator.get_module_by_id", return_value=Mock()):
        processor.dispatch_message(message, "template", None, 100, None, dedupe_key=("tests.slave chat", "message"), claim_token="claim-token", ownership_lost=ownership_lost)

    processor.delivery_claims.complete.assert_not_called()
    processor.commands.register_command.assert_not_called()
    processor.msglogs.add_or_update_message_log.assert_not_called()
    processor.logger.warning.assert_called_once_with("[%s] Delivery claim ownership was lost before post-send processing.", "message")


def test_failed_completion_fences_command_registration_and_message_logging() -> None:
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.chat_manager = Mock()
    processor.msglogs = SimpleNamespace(add_or_update_message_log=Mock())
    processor.delivery_claims = Mock(complete=Mock(return_value=False))
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

    processor.delivery_claims.complete.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.commands.register_command.assert_not_called()
    processor.msglogs.add_or_update_message_log.assert_not_called()
    processor.logger.warning.assert_called_once_with("[%s] Delivery claim ownership was lost before completion.", "message")


def test_database_mapping_failure_still_runs_dispatch_completion() -> None:
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.chat_manager = Mock()
    processor.msglogs = SimpleNamespace(add_or_update_message_log=Mock(side_effect=RuntimeError("database unavailable")))
    processor.delivery_claims = Mock()
    processor.router = Mock(resolve_reply=Mock(return_value=None))
    processor._release_pending_slave_message = Mock()
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
    processor.delivery_claims.complete.assert_called_once_with("tests.slave chat", "message", "claim-token")
    processor.logger.warning.assert_called_once_with(
        "DB write failed for Telegram message %s; dropping mapping (%s).",
        7,
        "RuntimeError",
    )
    processor._release_pending_slave_message.assert_not_called()


def test_command_session_uses_the_telegram_message_owner() -> None:
    processor = object.__new__(SlaveMessageService)
    processor.logger = Mock()
    processor.commands = SimpleNamespace(register_command=Mock())
    processor.chat_manager = Mock()
    processor.msglogs = Mock()
    processor.router = SimpleNamespace(resolve_reply=Mock(return_value=None), admins=[100])
    processor._release_pending_slave_message = Mock()
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
    processor._release_pending_slave_message = Mock()
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


def test_ingested_message_edit_has_no_telegram_side_effect() -> None:
    processor = _dedupe_processor()
    processor.msglogs.get_msg_log.return_value = SimpleNamespace(provenance="mtproto_ingested")
    message = _message("mtproto-ingested:100.1")
    message.edit = True

    assert processor.send_message(message) is message
    processor.router.route.assert_not_called()
    processor.dispatch_message.assert_not_called()


def test_send_kwargs_preserve_slave_routing_identity() -> None:
    message = SimpleNamespace(chat=SimpleNamespace(module_id="tests.slave", uid="chat"))
    assert send_identity(message) == {"_slave_id": "tests.slave chat"}
