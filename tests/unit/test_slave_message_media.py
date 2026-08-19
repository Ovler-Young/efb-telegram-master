from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlparse

import pytest
from ehforwarderbot.constants import MsgType
from ehforwarderbot.types import MessageID
from telegram import InputMediaPhoto
from telegram.error import BadRequest

from efb_telegram_master.delivery.oversized_notice import OversizedNoticeSender
from efb_telegram_master.delivery.slave_delivery_helpers import REMOTE_IMAGE_URL_VENDOR_KEY
from efb_telegram_master.delivery.slave_file_delivery import SlaveFileDelivery
from efb_telegram_master.delivery.slave_file_transfer import SlaveFileTransfer
from efb_telegram_master.delivery.slave_image_delivery import ImageDelivery
from efb_telegram_master.delivery.slave_media_delivery import SlaveMediaDelivery
from efb_telegram_master.delivery.slave_text_delivery import TextDelivery


def _processor():
    bot = Mock()
    flag = Mock(side_effect=lambda name: "emoji" if name == "default_media_prompt" else False)
    logger = Mock()
    temp_directory = lambda: None
    file_transfer = Mock()
    file_transfer.prepare.side_effect = lambda file, *_args: file
    file_transfer.check_size.return_value = None
    oversized_notice_sender = OversizedNoticeSender(bot)
    text_delivery = TextDelivery(1)
    return SimpleNamespace(
        bot=bot,
        file_transfer=file_transfer,
        oversized_notice_sender=oversized_notice_sender,
        image_delivery=ImageDelivery(bot, flag, logger, lambda text: text, text_delivery, file_transfer, oversized_notice_sender, temp_directory),
        media_delivery=SlaveMediaDelivery(bot, logger, text_delivery, file_transfer, oversized_notice_sender, temp_directory),
        file_delivery=SlaveFileDelivery(bot, flag, logger, lambda text: text, text_delivery, file_transfer, oversized_notice_sender, temp_directory),
    )


def _remote_image(url="https://example.com/images/photo.jpg"):
    return SimpleNamespace(
        uid=MessageID("remote"),
        type=MsgType.Image,
        text="remote <image>",
        chat=SimpleNamespace(module_id="tests.slave", uid="chat"),
        file=None,
        path=None,
        mime=None,
        filename=None,
        edit_media=False,
        commands=None,
        substitutions=None,
        vendor_specific={REMOTE_IMAGE_URL_VENDOR_KEY: url},
    )


def test_remote_image_send_and_edit_use_url() -> None:
    processor = _processor()
    processor.bot.send_photo.return_value = "sent"
    assert processor.image_delivery.slave_message_image(_remote_image(), 100, 7, "header", "footer", target_msg_id=9, silent=True) == "sent"
    assert processor.bot.send_photo.call_args.args == (100, "https://example.com/images/photo.jpg")
    assert processor.bot.send_photo.call_args.kwargs["reply_to_message_id"] == 9

    processor = _processor()
    processor.bot.edit_message_media.return_value = "edited"
    message = _remote_image()
    message.text = ""
    message.edit_media = True
    assert processor.image_delivery.slave_message_image(message, 100, None, "", "", old_msg_id=(100, 10)) == "edited"
    assert isinstance(processor.bot.edit_message_media.call_args.kwargs["media"], InputMediaPhoto)


def test_remote_image_and_document_url_failures_send_placeholder() -> None:
    processor = _processor()
    processor.bot.send_photo.side_effect = [BadRequest("failed to get HTTP URL content"), "placeholder"]
    assert processor.image_delivery.slave_message_image(_remote_image(), 100, None, "header", "footer") == "placeholder"
    assert processor.bot.send_photo.call_count == 2
    assert hasattr(processor.bot.send_photo.call_args_list[1].args[1], "read")

    processor = _processor()
    processor.bot.send_document.side_effect = [BadRequest("failed to get HTTP URL content"), "placeholder"]
    assert processor.file_delivery.file(_remote_image(), 100, None, "header", "footer") == "placeholder"
    assert hasattr(processor.bot.send_document.call_args_list[1].args[1], "read")


def test_remote_image_as_document_uses_url_filename() -> None:
    processor = _processor()
    processor.bot.send_document.return_value = "sent"
    assert processor.file_delivery.file(_remote_image("https://example.com/images/photo;1.jpg"), 100, None, "header", "footer") == "sent"
    assert processor.bot.send_document.call_args.args == (100, "https://example.com/images/photo;1.jpg")
    assert processor.bot.send_document.call_args.kwargs["filename"] == "photo 1.jpg"


def test_oversized_file_sends_current_bot_api_notice_for_new_message() -> None:
    processor = _processor()
    processor.bot.send_message.return_value = "sent"
    message = SimpleNamespace(edit_media=True, chat=SimpleNamespace(module_id="tests.slave", uid="chat"))
    with patch("efb_telegram_master.delivery.oversized_notice.sync_reply_text") as reply:
        sent, edit_media = processor.oversized_notice_sender.send(message, "Attachment exceeds the limit.", 100, 7, "header", "footer", "caption", None, 9, None, True)
    assert (sent, edit_media) == ("sent", True)
    assert processor.bot.send_message.call_args.kwargs["text"] == "caption"
    reply.assert_called_once_with(processor.bot, "sent", "Attachment exceeds the limit.", quote=True)


@pytest.mark.parametrize(
    "method_name",
    [
        "media_delivery.animation",
        "media_delivery.sticker",
        "image_delivery.slave_message_image",
        "file_delivery.file",
        "file_delivery.voice",
        "file_delivery.video",
    ],
)
def test_each_oversized_media_branch_uses_the_quoted_notice_helper(method_name: str) -> None:
    processor = _processor()
    processor.file_transfer.check_size.return_value = "Attachment exceeds the limit."
    processor.oversized_notice_sender = Mock(send=Mock(return_value=("notice", False)))
    processor.image_delivery.notice_sender = processor.oversized_notice_sender
    processor.media_delivery.notice_sender = processor.oversized_notice_sender
    processor.file_delivery.notice_sender = processor.oversized_notice_sender
    message = SimpleNamespace(
        uid="oversized",
        text="caption",
        file=BytesIO(b"file"),
        path=None,
        filename="file.bin",
        mime="application/octet-stream",
        edit_media=False,
        vendor_specific=None,
        commands=None,
        substitutions=None,
        type=MsgType.File,
    )

    service, method = method_name.split(".")
    if service == "image_delivery":
        message.type = MsgType.Image
    assert getattr(getattr(processor, service), method)(message, 100, None, "header", "footer") == "notice"
    processor.oversized_notice_sender.send.assert_called_once()


@pytest.mark.parametrize(
    ("payload", "suffix"),
    [
        (b"RIFF\x00\x00\x00\x00WEBP", ".webp"),
        (b"\x89PNG\r\n\x1a\nimage", ".png"),
        (b"\xff\xd8\xffimage", ".jpg"),
        (b"GIF89aimage", ".gif"),
        (b"\x00\x00\x00\x18ftypmp42", ".mp4"),
        (b"OggSimage", ".ogg"),
        (b"%PDF-1.7", ".pdf"),
    ],
)
def test_local_tdlib_extensionless_stream_uses_content_suffix_and_preserves_stream(payload: bytes, suffix: str, tmp_path: Path) -> None:
    bot = Mock()
    transfer = SlaveFileTransfer(lambda _name: True, bot, Mock(), lambda text: text, lambda: str(tmp_path))
    stream = BytesIO(payload)
    stream.seek(2)

    uri = transfer.prepare(stream, tmp_path.parent / "extensionless", "attachment")
    copied = Path(unquote(urlparse(uri).path))

    assert copied.suffix == suffix
    assert copied.read_bytes() == payload
    assert stream.tell() == 2
    assert not stream.closed
    bot.register_upload_cleanup.assert_called_once_with(str(copied))


def test_oversized_file_edit_replies_with_notice_only() -> None:
    processor = _processor()
    message = SimpleNamespace(edit_media=True, chat=SimpleNamespace(module_id="tests.slave", uid="chat"))
    assert processor.oversized_notice_sender.send(message, "Attachment exceeds the limit.", 100, None, "header", "footer", "caption", (200, 10), None, None, False) == (None, False)
    processor.bot.send_message.assert_called_once_with(chat_id=200, reply_to_message_id=10, text="Attachment exceeds the limit.")
