from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import telegram.constants
import telegram.error

from .outbound_types import QueuedCall, SenderSelection, SendReceipt, TelegramArgs, TelegramKwargs, rewind_uploads

if TYPE_CHECKING:
    from .bot_pool import BotPool


QUEUED_OPERATIONS = frozenset(
    {
        "send_message",
        "send_document",
        "send_photo",
        "send_audio",
        "send_video",
        "send_animation",
        "send_voice",
        "send_sticker",
        "send_media_group",
        "forward_message",
        "copy_message",
        "edit_message_text",
        "edit_message_caption",
        "edit_message_media",
        "delete_message",
        "edit_message_reply_markup",
        "send_location",
        "send_venue",
        "create_forum_topic",
        "edit_forum_topic",
        "reopen_forum_topic",
        "set_chat_title",
        "set_chat_photo",
        "pin_chat_message",
        "set_chat_description",
    }
)
_INTERNAL_KWARGS = frozenset({"prefix", "suffix", "_sender_bot_id", "_slave_id", "_force_main_bot"})
_CONTENT_SPECS = {
    "send_message": ("text", 1, int(telegram.constants.MessageLimit.MAX_TEXT_LENGTH)),
    "edit_message_text": ("text", 0, int(telegram.constants.MessageLimit.MAX_TEXT_LENGTH)),
    "send_voice": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "send_audio": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "send_video": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "send_document": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "send_animation": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "send_photo": ("caption", 2, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
    "edit_message_caption": ("caption", 3, int(telegram.constants.MessageLimit.CAPTION_LENGTH)),
}


def stripped_telegram_kwargs(kwargs: Mapping[str, object]) -> TelegramKwargs:
    return {key: value for key, value in kwargs.items() if key not in _INTERNAL_KWARGS}


@dataclass(frozen=True)
class PrimaryExecution:
    receipt: SendReceipt
    attachment: Optional[QueuedCall] = None


class TelegramCallAdapter:
    def __init__(self, bot_pool: Optional[BotPool]) -> None:
        self._bot_pool = bot_pool

    def execute_primary(self, call: QueuedCall, selection: SenderSelection) -> PrimaryExecution:
        method = getattr(selection.sender, call.operation)
        telegram_kwargs = stripped_telegram_kwargs(call.kwargs)
        telegram_args = call.args
        content_spec = _CONTENT_SPECS.get(call.operation)
        attachment: Optional[io.BytesIO] = None
        content_key: Optional[str] = None
        original_parse_mode = str(telegram_kwargs.get("parse_mode", "")).lower()
        if content_spec is not None:
            content_key, content_index, content_limit = content_spec
            full_content, positional = self._content_argument(telegram_args, telegram_kwargs, content_key, content_index)
            if full_content is not None and len(full_content) >= content_limit:
                attachment = io.BytesIO(self._attachment_content(full_content, original_parse_mode).encode("utf-8"))
                truncated = full_content[:100] + "\n...\n" + full_content[-100:]
                if positional:
                    telegram_args = (*telegram_args[:content_index], truncated, *telegram_args[content_index + 1 :])
                else:
                    telegram_kwargs[content_key] = truncated
        try:
            result = method(*telegram_args, **telegram_kwargs)
        except telegram.error.BadRequest as error:
            if not error.message.lower().startswith("can't parse entities") or "parse_mode" not in telegram_kwargs:
                raise
            telegram_kwargs.pop("parse_mode")
            rewind_uploads(telegram_args, telegram_kwargs)
            result = method(*telegram_args, **telegram_kwargs)
        receipt = SendReceipt(result, selection.sender_bot_id)
        return PrimaryExecution(receipt, self._attachment_call(call, attachment, content_key, original_parse_mode, receipt, selection))

    @staticmethod
    def execute_attachment(call: QueuedCall, selection: SenderSelection) -> object:
        return getattr(selection.sender, call.operation)(*call.args, **stripped_telegram_kwargs(call.kwargs))

    def record_successful_send(self, call: QueuedCall, selection: SenderSelection) -> None:
        if selection.sender_bot_id is not None and self._bot_pool and call.slave_id:
            self._bot_pool.record_successful_auxiliary_send(call.slave_id, selection.sender_bot_id)

    @staticmethod
    def _attachment_call(call: QueuedCall, attachment: Optional[io.BytesIO], content_key: Optional[str], parse_mode: str, receipt: SendReceipt, selection: SenderSelection) -> Optional[QueuedCall]:
        message_id = getattr(receipt.message, "message_id", None)
        if attachment is None or content_key is None or message_id is None:
            return None
        extension = ".md" if parse_mode == "markdown" else ".html" if parse_mode == "html" else ".txt"
        label = "Message" if content_key == "text" else "Caption"
        required_sender_bot_id = selection.sender_bot_id if selection.sender_bot_id is not None else "__main__"
        return QueuedCall(
            "send_document",
            (call.telegram_chat_id, attachment),
            {
                "filename": f"{call.telegram_chat_id}_{message_id}{extension}",
                "reply_to_message_id": message_id,
                "caption": f"{label} is truncated due to its length. Full message is sent as attachment.",
            },
            call.telegram_chat_id,
            call.slave_id,
            required_sender_bot_id,
            call.cleanup,
        )

    @staticmethod
    def _content_argument(args: TelegramArgs, kwargs: Mapping[str, object], key: str, index: int) -> tuple[Optional[str], bool]:
        content = args[index] if len(args) > index else kwargs.get(key)
        return content if isinstance(content, str) else None, len(args) > index

    @staticmethod
    def _attachment_content(content: str, parse_mode: str) -> str:
        if parse_mode == "html":
            return "<html><head><meta charset='utf-8'></head><body><pre style='white-space:pre-wrap'>" + content + "</pre></body></html>"
        return content
