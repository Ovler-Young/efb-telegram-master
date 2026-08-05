from types import SimpleNamespace
from unittest.mock import Mock, patch

from efb_telegram_master.slave_message import SlaveMessageProcessor


def _processor() -> SlaveMessageProcessor:
    processor = object.__new__(SlaveMessageProcessor)
    processor.bot = Mock()
    processor._make_send_kwargs = Mock(return_value={"_slave_id": "tests.slave.chat"})
    return processor


def test_oversized_file_new_send_replies_with_limit_message() -> None:
    processor = _processor()
    msg = SimpleNamespace(edit_media=True)
    sent = Mock()
    processor.bot.send_message.return_value = sent

    with patch("efb_telegram_master.slave_message.sync_reply_text") as sync_reply:
        result, edit_media = processor._send_oversized_file_notice(
            msg,
            "Attachment exceeds the limit.",
            100,
            7,
            "header",
            "footer",
            "caption",
            None,
            9,
            None,
            True,
            None,
        )

    assert result is sent
    assert edit_media is True
    processor.bot.send_message.assert_called_once_with(
        chat_id=100,
        reply_to_message_id=9,
        message_thread_id=7,
        text="caption",
        parse_mode="HTML",
        reply_markup=None,
        disable_notification=True,
        prefix="header",
        suffix="footer",
        _slave_id="tests.slave.chat",
    )
    sync_reply.assert_called_once_with(processor.bot, sent, "Attachment exceeds the limit.", quote=True)


def test_oversized_file_edit_keeps_caption_only() -> None:
    processor = _processor()
    msg = SimpleNamespace(edit_media=True)

    result, edit_media = processor._send_oversized_file_notice(
        msg,
        "Attachment exceeds the limit.",
        100,
        None,
        "header",
        "footer",
        "caption",
        ("200", 10),
        None,
        None,
        False,
        None,
    )

    assert result is None
    assert edit_media is False
    processor.bot.send_message.assert_called_once_with(chat_id="200", reply_to_message_id=10, text="Attachment exceeds the limit.")
