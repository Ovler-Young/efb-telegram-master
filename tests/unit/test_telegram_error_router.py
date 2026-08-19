import threading
from types import SimpleNamespace
from unittest.mock import Mock

from ehforwarderbot.types import ChatID, ModuleID
from telegram import Update
from telegram.error import ChatMigrated

from efb_telegram_master import utils
from efb_telegram_master.outbound_types import SchedulerStoppedError
from efb_telegram_master.transport.telegram_error_router import TelegramErrorRouter


def _router(*, stopping: threading.Event | None = None) -> TelegramErrorRouter:
    return TelegramErrorRouter(
        Mock(),
        [1],
        Mock(),
        ModuleID("blueset.telegram"),
        lambda: 0,
        lambda text: text,
        lambda single, _plural, _count: single,
        stopping or threading.Event(),
        Mock(),
    )


def test_router_does_not_notify_through_a_stopped_outbound_queue() -> None:
    stopping = threading.Event()
    stopping.set()
    router = _router(stopping=stopping)

    router._handle_error(object(), SchedulerStoppedError("Outbound queue stopped."))

    router.logger.info.assert_called_once_with(
        "Ignoring outbound delivery cancellation during Telegram shutdown.",
        extra={"event": "telegram_channel.outbound_cancelled_during_shutdown"},
    )


def test_router_reports_scheduler_stopped_error_while_running() -> None:
    router = _router()
    router._notify_unhandled_error = Mock()
    update = object()
    error = SchedulerStoppedError("Outbound queue stopped.")

    router._handle_error(update, error)

    router._notify_unhandled_error.assert_called_once_with(update, error)
    router.logger.info.assert_not_called()


def test_router_chat_migration_keeps_multiple_slave_associations() -> None:
    old_chat_id, new_chat_id = -100720, -100721
    router = _router()
    router.chat_associations.get_chat_assoc.return_value = ["tests.slave.one", "tests.slave.two"]
    update = Update.de_json(
        {"update_id": 1, "message": {"message_id": 1, "date": 1, "chat": {"id": old_chat_id, "type": "supergroup"}}},
        SimpleNamespace(defaults=SimpleNamespace(tzinfo=None)),
    )

    router._handle_chat_migration(update, ChatMigrated(new_chat_id))

    new_master_uid = utils.chat_id_to_str(router.channel_id, ChatID(str(new_chat_id)))
    assert router.chat_associations.add_chat_assoc.call_args_list == [
        ((), {"master_uid": new_master_uid, "slave_uid": "tests.slave.one", "multiple_slave": True}),
        ((), {"master_uid": new_master_uid, "slave_uid": "tests.slave.two", "multiple_slave": True}),
    ]
