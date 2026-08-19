from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ehforwarderbot.constants import MsgType
from ehforwarderbot.exceptions import EFBChatNotFound, EFBException, EFBMessageTypeNotSupported, EFBOperationNotSupported
from telegram import Update
from telegram.constants import FileSizeLimit

from efb_telegram_master.master_delivery import MasterMessageDelivery
from efb_telegram_master.utils import EFBChannelChatIDStr


def _delivery_for_error_reply() -> tuple[MasterMessageDelivery, Mock, Mock]:
    bot = Mock()
    logger = Mock()
    delivery = MasterMessageDelivery(
        bot,
        Mock(),
        Mock(),
        Mock(),
        lambda text: text,
        lambda _: False,
        Mock(),
        logger,
    )
    return delivery, bot, logger


@pytest.mark.parametrize(
    ("error", "expected_reply"),
    [
        (EFBChatNotFound("SENTINEL-SECRET"), "Chat is not found."),
        (EFBMessageTypeNotSupported("SENTINEL-SECRET"), "Message type is not supported."),
        (EFBOperationNotSupported("SENTINEL-SECRET"), "Message editing is not supported."),
        (EFBException("SENTINEL-SECRET"), "Message is not sent."),
        (RuntimeError("SENTINEL-SECRET"), "Message is not sent."),
    ],
)
def test_master_delivery_keeps_exception_text_out_of_replies_and_logs_context(monkeypatch, error, expected_reply) -> None:
    message = SimpleNamespace(message_id=2, chat_id=1, chat=SimpleNamespace(id=1))
    update = Update(update_id=1, message=message)
    delivery, bot, logger = _delivery_for_error_reply()
    monkeypatch.setattr("efb_telegram_master.master_delivery.coordinator.slaves", {"slave": Mock()})
    monkeypatch.setattr("efb_telegram_master.master_delivery.get_msg_type", Mock(side_effect=error))

    delivery.deliver(update, None, EFBChannelChatIDStr("slave chat"))

    reply = bot.reply_error.call_args.args[1]
    assert reply == expected_reply
    assert "SENTINEL-SECRET" not in reply
    log_args = logger.exception.call_args.args
    assert log_args[0] == "Message %s is not sent (%s)."
    assert log_args[-1] == type(error).__name__


@pytest.mark.parametrize(
    ("attachment_name", "message_type"),
    [("photo", MsgType.Image), ("document", MsgType.File), ("video", MsgType.Video)],
)
def test_edited_oversized_attachment_removal_skips_download_eligibility(monkeypatch, attachment_name, message_type) -> None:
    attachment = SimpleNamespace(file_id="file-id", file_unique_id="file-unique-id", file_size=FileSizeLimit.FILESIZE_DOWNLOAD + 1, file_name="attachment.bin", mime_type="application/octet-stream")
    message = SimpleNamespace(
        message_id=2,
        chat_id=1,
        chat=SimpleNamespace(id=1),
        text=None,
        text_markdown_v2=None,
        caption="rm`remove",
        caption_markdown_v2="rm`remove",
        animation=None,
        audio=None,
        document=attachment if attachment_name == "document" else None,
        photo=[attachment] if attachment_name == "photo" else None,
        sticker=None,
        video=attachment if attachment_name == "video" else None,
        voice=None,
        contact=None,
        location=None,
        venue=None,
        game=None,
        video_note=None,
        poll=None,
        dice=None,
    )
    update = Update(update_id=1, edited_message=message)
    slave = SimpleNamespace(supported_message_types={message_type})
    chat = SimpleNamespace(self=None, add_self=Mock(return_value=SimpleNamespace()))
    removals = []
    bot = Mock()
    msglogs = Mock()
    delivery = MasterMessageDelivery(
        bot,
        msglogs,
        SimpleNamespace(get_chat=Mock(return_value=chat)),
        Mock(),
        lambda text: text,
        lambda _: False,
        lambda destination, etm_message: removals.append((destination, etm_message)),
        Mock(),
    )
    monkeypatch.setattr("efb_telegram_master.master_delivery.coordinator.slaves", {"slave": slave})

    delivery.deliver(update, None, EFBChannelChatIDStr("slave chat"), edited=SimpleNamespace(slave_message_id="old-message", file_unique_id="old-file"))

    assert len(removals) == 1
    assert removals[0][0] is slave
    assert removals[0][1].edit
    assert removals[0][1].uid == "old-message"
    msglogs.add_or_update_message_log.assert_not_called()
