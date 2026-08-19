from pathlib import Path
from typing import Optional

from ehforwarderbot import Chat
from ehforwarderbot import Message as EFBMessage
from ehforwarderbot.message import MsgType
from telethon.tl.custom import Message

from tests.integration.slave_message_factory_base import MessageFactory
from tests.integration.slave_message_text_factories import LinkMessageFactory, LocationMessageFactory, TextMessageFactory
from tests.mocks.slave.channel import MockSlaveChannel


class ImageMessageFactory(MessageFactory):
    def __init__(self, large: bool = False):
        """
        Args:
            large: If the picture to be sent is large in dimension.
        """
        self.large = large

    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        if self.large:
            path = Path("tests/mocks/large_image_0.png")
        else:
            path = Path("tests/mocks/image.png")
        return slave.send_file_like_message(MsgType.Image, path, "image/png", chat, target=target, reactions=True, commands=True, substitution=True)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        if self.large:
            assert tg_msg.file
            assert tg_msg.file.name == efb_msg.filename
            assert efb_msg.path is not None
            size = efb_msg.path.stat().st_size
            assert tg_msg.file.size == size
        else:
            assert tg_msg.photo
            # Cannot do further assertion here as Telegram has compressed the
            # pictures sent out
        assert efb_msg.text in tg_msg.raw_text
        for i in efb_msg.reactions:
            assert i in tg_msg.raw_text
        if efb_msg.commands:
            assert tg_msg.button_count == len(efb_msg.commands)

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message_text(message, reactions=True, commands=True, substitution=True)

    def edit_message_media(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        if self.large:
            path = Path("tests/mocks/large_image_1.png")
        else:
            path = Path("tests/mocks/image_1.png")
        return slave.edit_file_like_message(message, path, mime="image/png", reactions=True, commands=True, substitution=True)

    def finalize_message(self, efb_msg: EFBMessage):
        if efb_msg.file and not efb_msg.file.closed:
            efb_msg.file.close()

    def __str__(self):
        return f"{self.__class__.__name__}(large={self.large})"


class StickerMessageFactory(MessageFactory):
    media_editable = False

    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        return slave.send_file_like_message(MsgType.Sticker, Path("tests/mocks/sticker_0.png"), "image/png", chat, target=target, reactions=True, commands=True, substitution=True)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert tg_msg.sticker is not None
        # Cannot do further assertion here as Telegram has converted the
        # pictures sent out
        self.assert_metadata_in_buttons(tg_msg, efb_msg)

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message_text(message, reactions=True, commands=True, substitution=True)

    def edit_message_media(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message(message, Path("tests/mocks/sticker_1.png"), mime="image/png", reactions=True, commands=True, substitution=True)

    def finalize_message(self, efb_msg: EFBMessage):
        if efb_msg.file and not efb_msg.file.closed:
            efb_msg.file.close()


class FileMessageFactory(MessageFactory):
    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        return slave.send_file_like_message(MsgType.File, Path("tests/mocks/document_0.txt.gz"), "application/gzip", chat, target=target, reactions=True, commands=True, substitution=True)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert tg_msg.file
        assert tg_msg.file.name == efb_msg.filename
        assert efb_msg.path is not None
        size = efb_msg.path.stat().st_size
        assert tg_msg.file.size == size
        assert efb_msg.text in tg_msg.raw_text
        for i in efb_msg.reactions:
            assert i in tg_msg.raw_text
        if efb_msg.commands:
            assert tg_msg.button_count == len(efb_msg.commands)

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message_text(message, reactions=True, commands=True, substitution=True)

    def edit_message_media(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message(message, Path("tests/mocks/document_1.txt.gz"), mime="application/gzip", reactions=True, commands=True, substitution=True)

    def finalize_message(self, efb_msg: EFBMessage):
        if efb_msg.file and not efb_msg.file.closed:
            efb_msg.file.close()


class AnimationMessageFactory(MessageFactory):
    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        return slave.send_file_like_message(MsgType.Animation, Path("tests/mocks/animation_0.gif"), "image/gif", chat, target=target, reactions=True, commands=True, substitution=True)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert tg_msg.gif
        # Cannot do further assertion here as Telegram has converted the GIF
        # to MP4
        assert efb_msg.text in tg_msg.raw_text
        for i in efb_msg.reactions:
            assert i in tg_msg.raw_text
        if efb_msg.commands:
            assert tg_msg.button_count == len(efb_msg.commands)

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message_text(message, reactions=True, commands=True, substitution=True)

    def edit_message_media(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message(message, Path("tests/mocks/animation_1.gif"), "image/gif", reactions=True, commands=True, substitution=True)

    def finalize_message(self, efb_msg: EFBMessage):
        if efb_msg.file and not efb_msg.file.closed:
            efb_msg.file.close()


class VideoMessageFactory(MessageFactory):
    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        return slave.send_file_like_message(MsgType.Video, Path("tests/mocks/video_0.mp4"), "video/mp4", chat, target=target, reactions=True, commands=True, substitution=True)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert tg_msg.video or tg_msg.file
        # Cannot do further assertion here as Telegram has re-encoded the
        # video sent out
        assert efb_msg.text in tg_msg.raw_text
        for i in efb_msg.reactions:
            assert i in tg_msg.raw_text
        if efb_msg.commands:
            assert tg_msg.button_count == len(efb_msg.commands)

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message_text(message, reactions=True, commands=True, substitution=True)

    def edit_message_media(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message(message, Path("tests/mocks/video_1.mp4"), "video/mp4", reactions=True, commands=True, substitution=True)

    def finalize_message(self, efb_msg: EFBMessage):
        if efb_msg.file and not efb_msg.file.closed:
            efb_msg.file.close()


class VoiceMessageFactory(MessageFactory):
    media_editable = False

    def send_message(self, slave: MockSlaveChannel, chat: Chat, target: Optional[Message] = None) -> Message:
        return slave.send_file_like_message(MsgType.Voice, Path("tests/mocks/audio_0.mp3"), "audio/mpeg", chat, target=target, reactions=True, commands=True, substitution=True)

    def compare_message(self, tg_msg: Message, efb_msg: EFBMessage) -> None:
        assert tg_msg.voice
        # Cannot do further assertion here as Telegram has converted the voice
        # file to OGG OPUS
        assert efb_msg.text in tg_msg.raw_text
        for i in efb_msg.reactions:
            assert i in tg_msg.raw_text
        if efb_msg.commands:
            assert tg_msg.button_count == len(efb_msg.commands)

    def edit_message(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message_text(message, reactions=True, commands=True, substitution=True)

    def edit_message_media(self, slave: MockSlaveChannel, message: Message) -> Optional[Message]:
        return slave.edit_file_like_message(message, Path("tests/mocks/audio_0.mp3"), "audio/mpeg", reactions=True, commands=True, substitution=True)

    def finalize_message(self, efb_msg: EFBMessage):
        if efb_msg.file and not efb_msg.file.closed:
            efb_msg.file.close()


def all_message_factories():
    return [
        TextMessageFactory(),
        LinkMessageFactory(),
        LocationMessageFactory(),
        ImageMessageFactory(large=False),
        ImageMessageFactory(large=True),
        StickerMessageFactory(),
        FileMessageFactory(),
        AnimationMessageFactory(),
        VideoMessageFactory(),
        VoiceMessageFactory(),
        TextMessageFactory(unsupported=True),
    ]
