import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Dict, Optional

import magic
import telegram
from ehforwarderbot import Channel, Chat, Message, MsgType, coordinator
from ehforwarderbot.chat import ChatMember
from ehforwarderbot.message import MessageAttribute, MessageCommands, Substitutions
from ehforwarderbot.types import MessageID, Reactions
from PIL import Image
from telegram.error import BadRequest

from .chat import ETMChatMixin
from .chat_member import ETMChatMember
from .chat_object_cache import ChatObjectCacheManager
from .media import convert_tgs_to_gif, gif_conversion
from .msg_type import TGMsgType

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

FILE_DOWNLOAD_TIMEOUT = 120

__all__ = ["ETMMsg"]


class ETMMsg(Message):
    file_id: Optional[str] = None
    """File ID from Telegram Bot API"""
    file_unique_id: Optional[str] = None
    """Unique file ID from Telegram Bot API"""
    sender_bot_id: Optional[str] = None
    """Telegram bot user ID that sent this message. None means the main bot."""
    type_telegram: TGMsgType
    """Type of message in Telegram Bot API"""
    chat: ETMChatMixin
    author: ETMChatMember

    __file = None
    __path = None
    __filename = None

    def __init__(
        self,
        attributes: Optional[MessageAttribute] = None,
        author: Optional[ChatMember] = None,
        chat: Optional[Chat] = None,
        commands: Optional[MessageCommands] = None,
        deliver_to: Optional[Channel] = None,
        edit: bool = False,
        edit_media: bool = False,
        file: Optional[BinaryIO] = None,
        filename: Optional[str] = None,
        is_system: bool = False,
        mime: Optional[str] = None,
        path: Optional[Path] = None,
        reactions: Optional[Reactions] = None,
        substitutions: Optional[Substitutions] = None,
        target: "Optional[Message]" = None,
        text: str = "",
        type: MsgType = MsgType.Unsupported,
        uid: Optional[MessageID] = None,
        vendor_specific: Optional[Dict[str, Any]] = None,
        type_telegram: TGMsgType = TGMsgType.System,
        file_id: Optional[str] = None,
    ):
        # ehforwarderbot.Message doesn't annotate these as Optional, but handles None at runtime
        super().__init__(
            attributes=attributes,
            chat=chat,  # type: ignore[arg-type]
            author=author,  # type: ignore[arg-type]
            commands=commands,
            deliver_to=deliver_to,  # type: ignore[arg-type]
            edit=edit,
            edit_media=edit_media,
            file=file,
            filename=filename,
            is_system=is_system,
            mime=mime,
            path=path,
            reactions=reactions,  # type: ignore[arg-type]
            substitutions=substitutions,
            target=target,
            text=text,
            type=type,
            uid=uid,
            vendor_specific=vendor_specific,  # type: ignore[arg-type]
        )
        self.__initialized = False
        self.type_telegram = type_telegram
        self.file_id = file_id

    def _load_file(self):
        if self.file_id:
            # noinspection PyUnresolvedReferences
            api = coordinator.master.bot_manager.api
            # Route get_file through the correct bot based on sender_bot_id.
            file_bot = None
            if self.sender_bot_id:
                bot_pool = api.bot_pool
                if bot_pool:
                    aux_bot = bot_pool.get_bot_by_id(self.sender_bot_id)
                    if aux_bot and not aux_bot.disabled:
                        file_bot = aux_bot.bot

            try:
                if file_bot:
                    file_meta = file_bot.get_file(self.file_id)
                else:
                    file_meta = api.get_file(self.file_id)
            except BadRequest as e:
                if file_bot:
                    logger.warning("Auxiliary bot file lookup failed (%s); trying main bot.", type(e).__name__)
                    try:
                        file_meta = api.get_file(self.file_id)
                    except BadRequest as e2:
                        logger.exception("Main bot file lookup failed (%s).", type(e2).__name__)
                        return
                else:
                    logger.exception("File metadata lookup failed (%s).", type(e).__name__)
                    return
            ext = os.path.splitext(file_meta.file_path)[1]
            file = tempfile.NamedTemporaryFile(suffix=ext)
            coordinator.master.telegram_runtime.async_runtime.call(
                file_meta.download_to_memory(out=file),
                timeout=FILE_DOWNLOAD_TIMEOUT,
            )
            if not self.mime:
                mime = mimetypes.guess_type(file_meta.file_path, strict=False)[0]
            else:
                mime = self.mime
            file.seek(0)

            if not mime:
                # Try to deal with restriction from Windows by only providing
                # libmagic with the first 1048176 bytes (1 MiB) of data.
                mime = magic.from_buffer(file.read(1048576), mime=True)
                # mime = mime or magic.from_file(file.name, mime=True)
                if type(mime) is bytes:
                    mime = mime.decode()
            self.mime = mime

            self.__file = file
            self.__path = Path(file.name)
            self.__filename = self.__filename or os.path.basename(file.name)

            if self.type_telegram in (TGMsgType.Animation, TGMsgType.VideoSticker):
                gif_file = gif_conversion(file, self.deliver_to.channel_id)

                self.__file = gif_file
                self.__path = gif_file.name
                self.__filename = self.__filename or os.path.basename(gif_file.name)
                self.mime = "image/gif"
            elif self.type_telegram == TGMsgType.Sticker:
                out_file = tempfile.NamedTemporaryFile(suffix=".png")
                Image.open(file).convert("RGBA").save(out_file, "png")
                file.close()
                out_file.seek(0)
                self.mime = "image/png"
                self.__filename = (self.__filename or os.path.basename(file.name)) + ".png"
                self.__file = out_file
                self.__path = out_file.name
            elif self.type_telegram == TGMsgType.AnimatedSticker:
                out_file = tempfile.NamedTemporaryFile(suffix=".gif")
                if convert_tgs_to_gif(file, out_file):
                    file.close()
                    out_file.seek(0)
                    self.mime = "image/gif"
                    self.__filename = (self.__filename or os.path.basename(file.name)) + ".gif"
                else:
                    # Conversion failed, send file as is.
                    out_file.close()
                    file.seek(0)
                    out_file = file
                    self.mime = "application/json"
                    self.__filename = (self.__filename or os.path.basename(file.name)) + ".json"
                self.__file = out_file
                self.__path = out_file.name

        self.__initialized = True

    def get_file(self) -> Optional[BinaryIO]:
        if not self.__initialized:
            self._load_file()
        return self.__file

    def set_file(self, value: Optional[BinaryIO]):
        # Stop initialization-on-demand as new file info is written
        # This is added for compatibility with middleware behaviors
        self.__initialized = True
        self.__file = value

    def get_path(self) -> Optional[str]:
        if not self.__initialized:
            self._load_file()
        return self.__path

    def set_path(self, value: Optional[str]):
        # Stop initialization-on-demand as new file info is written
        # This is added for compatibility with middleware behaviors
        self.__initialized = True
        self.__path = value

    def get_filename(self) -> Optional[str]:
        if not self.__initialized:
            self._load_file()
        return self.__filename

    def set_filename(self, value: Optional[str]):
        self.__filename = value

    # Override properties
    file: Optional[BinaryIO] = property(get_file, set_file)  # type: ignore
    path: Optional[str] = property(get_path, set_path)  # type: ignore
    filename: Optional[str] = property(get_filename, set_filename)  # type: ignore

    @staticmethod
    def from_efbmsg(source: Message, chat_manager: ChatObjectCacheManager) -> "ETMMsg":
        target = ETMMsg()
        target.__dict__.update(source.__dict__)
        if not isinstance(target.chat, ETMChatMixin):
            target.chat = chat_manager.update_chat_obj(target.chat)
        if not isinstance(target.author, ETMChatMember):
            target.author = target.chat.get_member(target.author.uid)
        if isinstance(target.reactions, dict):
            for i in target.reactions:
                if any(not isinstance(j, ETMChatMember) for j in target.reactions[i]):
                    # noinspection PyTypeChecker
                    target.reactions[i] = list(map(lambda a: target.chat.get_member(a.uid), target.reactions[i]))
        return target

    def put_telegram_file(self, message: telegram.Message):
        is_common_file = False

        # Store media related information to local database
        for tg_media_type in ("animation", "document", "video", "voice"):
            attachment = getattr(message, tg_media_type, None)
            if attachment:
                is_common_file = True
                self.file_id = attachment.file_id
                self.file_unique_id = attachment.file_unique_id
                self.mime = attachment.mime_type
                break

        if not is_common_file:
            if self.type_telegram is TGMsgType.Audio:
                assert message.audio
                self.file_id = message.audio.file_id
                self.file_unique_id = message.audio.file_unique_id
                self.mime = message.audio.mime_type
                self.filename = message.audio.file_name
            elif self.type_telegram is TGMsgType.Sticker:
                assert message.sticker
                self.file_id = message.sticker.file_id
                self.file_unique_id = message.sticker.file_unique_id
                self.mime = "image/webp"
            elif self.type_telegram is TGMsgType.AnimatedSticker:
                assert message.sticker
                self.file_id = message.sticker.file_id
                self.file_unique_id = message.sticker.file_unique_id
                self.mime = "application/json+tgs"
                self.type = MsgType.Animation
            elif self.type_telegram is TGMsgType.VideoSticker:
                assert message.sticker
                self.file_id = message.sticker.file_id
                self.file_unique_id = message.sticker.file_unique_id
                self.mime = "video/webm"
                self.type = MsgType.Animation
            elif getattr(message, "photo", None):
                attachment = message.photo[-1]
                self.file_id = attachment.file_id
                self.file_unique_id = attachment.file_unique_id
                self.mime = "image/jpeg"
            elif self.type_telegram is TGMsgType.VideoNote:
                assert message.video_note
                self.file_id = message.video_note.file_id
                self.file_unique_id = message.video_note.file_unique_id
                self.mime = "video/mpeg"
