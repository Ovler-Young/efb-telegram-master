"""Telegram notice delivery for Bot API attachment limits."""

from typing import Optional

import telegram
from ehforwarderbot import Message
from telegram._utils.types import ReplyMarkup

from efb_telegram_master.core.ptb_compat import sync_reply_text
from efb_telegram_master.core.utils import OldMsgID, TelegramChatID, TelegramMessageID, TelegramTopicID, chat_id_to_str


class OversizedNoticeSender:
    def __init__(self, bot) -> None:
        self.bot = bot

    def send(
        self,
        msg: Message,
        notice: str,
        destination: TelegramChatID,
        thread_id: Optional[TelegramTopicID],
        template: str,
        reactions: str,
        text: str,
        old_message_id: Optional[OldMsgID],
        reply_to: Optional[TelegramMessageID],
        reply_markup: Optional[ReplyMarkup],
        silent: Optional[bool],
    ) -> tuple[Optional[telegram.Message], bool]:
        if old_message_id:
            self.bot.send_message(chat_id=old_message_id[0], reply_to_message_id=old_message_id[1], text=notice)
            return None, False
        message = self.bot.send_message(
            chat_id=destination,
            reply_to_message_id=reply_to,
            message_thread_id=thread_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_notification=silent,
            prefix=template,
            suffix=reactions,
            _slave_id=chat_id_to_str(chat=msg.chat),
        )
        sync_reply_text(self.bot, message, notice, quote=True)
        return message, msg.edit_media
