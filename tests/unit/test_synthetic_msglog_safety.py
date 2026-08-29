import datetime
from gettext import NullTranslations
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ehforwarderbot.types import ModuleID
from telegram import Chat, Message, Update

from efb_telegram_master import TelegramChannel
from efb_telegram_master.db import SYNTHETIC_MSGLOG_PREFIX
from efb_telegram_master.master_message import MasterMessageProcessor


def _telegram_message(message_id: int, text: str, *, reply_to: Message | None = None) -> Message:
    return Message(
        message_id,
        datetime.datetime.now(datetime.timezone.utc),
        Chat(-100, "supergroup"),
        text=text,
        reply_to_message=reply_to,
    )


def _synthetic_log():
    return SimpleNamespace(
        slave_message_id=f"{SYNTHETIC_MSGLOG_PREFIX}-100.2",
        slave_origin_uid="tests.mocks.slave chat",
        slave_member_uid="tests.mocks.slave author",
        build_etm_msg=Mock(),
    )


def _processor(msg_log) -> MasterMessageProcessor:
    processor = object.__new__(MasterMessageProcessor)
    processor.channel = SimpleNamespace(_=lambda text: text)
    processor.channel_id = ModuleID("blueset.telegram")
    processor.db = SimpleNamespace(
        FAIL_FLAG="__fail__",
        get_msg_log=Mock(return_value=msg_log),
    )
    processor.bot = Mock()
    processor.logger = Mock()
    return processor


def test_edit_does_not_propagate_synthetic_msglog_identity():
    processor = _processor(_synthetic_log())
    edited = _telegram_message(2, "edited")
    update = Update(1, edited_message=edited)

    with patch("efb_telegram_master.master_message.coordinator.send_message") as send_message, \
            patch("efb_telegram_master.master_message.sync_reply_text") as reply:
        processor.msg(update, Mock())

    send_message.assert_not_called()
    reply.assert_not_called()


def test_quote_does_not_attach_synthetic_msglog_identity():
    target_log = _synthetic_log()
    processor = _processor(target_log)
    target = _telegram_message(2, "target")
    reply = _telegram_message(3, "reply", reply_to=target)
    etm_message = SimpleNamespace(target=None)

    result = processor.attach_target_message(reply, etm_message, ModuleID("tests.mocks.slave"))

    assert result is etm_message
    assert etm_message.target is None
    target_log.build_etm_msg.assert_not_called()


def test_removal_rejects_synthetic_msglog_identity():
    target_log = _synthetic_log()
    processor = _processor(target_log)
    target = _telegram_message(2, "target")
    command = _telegram_message(3, "/rm", reply_to=target)

    with patch("efb_telegram_master.master_message.coordinator.send_status") as send_status:
        processor.delete_message(Update(1, message=command), Mock())

    processor.bot.reply_error.assert_called_once()
    target_log.build_etm_msg.assert_not_called()
    send_status.assert_not_called()


def test_reaction_rejects_synthetic_msglog_identity():
    target = _telegram_message(2, "target")
    command = _telegram_message(3, "/react value", reply_to=target)
    channel = object.__new__(TelegramChannel)
    channel.translator = NullTranslations()
    channel.db = SimpleNamespace(get_msg_log=Mock(return_value=_synthetic_log()))
    channel.bot_manager = Mock()

    with patch("efb_telegram_master.sync_reply_text") as reply, \
            patch("efb_telegram_master.coordinator.send_status") as send_status:
        TelegramChannel.react(channel, Update(1, message=command), Mock())

    reply.assert_called_once()
    send_status.assert_not_called()
