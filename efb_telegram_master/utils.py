# coding=utf-8

import base64
import json
import logging
import os
import subprocess
from io import BytesIO
from collections.abc import Mapping
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING, BinaryIO, IO, cast
from urllib.parse import quote, urlparse, urlunparse

import ffmpeg
import telegram
from PIL import Image
from ffmpeg._utils import convert_kwargs_to_cmd_line_args
from typing_extensions import NewType

from ehforwarderbot import Channel
from ehforwarderbot.chat import BaseChat, ChatMember
from ehforwarderbot.types import ChatID, ModuleID
from .locale_mixin import LocaleMixin

if TYPE_CHECKING:
    from . import TelegramChannel

FFMPEG_TIMEOUT = 60


TelegramChatID = NewType('TelegramChatID', int)
TelegramTopicID = NewType('TelegramTopicID', int)
TelegramMessageID = NewType('TelegramMessageID', int)
TgChatMsgIDStr = NewType('TgChatMsgIDStr', str)
EFBChannelChatIDStr = NewType('EFBChannelChatIDStr', str)
OldMsgID = Tuple[TelegramChatID, TelegramMessageID]
# TelegramChatID = Union[str, int]
# TelegramMessageID = Union[str, int]
# TgChatMsgIDStr = str
# EFBChannelChatIDStr = str


def normalize_request_kwargs(request_kwargs: Mapping[str, object] | None) -> dict[str, object]:
    """Translate supported legacy request settings to ``HTTPXRequest`` kwargs.

    Only PTB 22 request options are retained. ``proxy`` takes precedence over
    ``proxy_url``; credentials from top-level settings or
    ``urllib3_proxy_kwargs`` are percent-encoded into a proxy without embedded
    credentials.
    """
    if not isinstance(request_kwargs, Mapping):
        return {}
    allowed = {
        "read_timeout", "write_timeout", "connect_timeout", "pool_timeout",
        "media_write_timeout", "http_version", "socket_options", "httpx_kwargs",
        "connection_pool_size",
    }
    normalized = {key: request_kwargs[key] for key in allowed if key in request_kwargs}
    proxy = request_kwargs.get("proxy") or request_kwargs.get("proxy_url")
    proxy_auth = request_kwargs.get("urllib3_proxy_kwargs")
    auth = proxy_auth if isinstance(proxy_auth, Mapping) else {}
    username = request_kwargs.get("username", auth.get("username"))
    password = request_kwargs.get("password", auth.get("password"))
    if proxy:
        parsed = urlparse(str(proxy))
        if username is not None and "@" not in parsed.netloc:
            netloc = quote(str(username), safe="")
            if password is not None:
                netloc += ":" + quote(str(password), safe="")
            netloc += f"@{parsed.hostname or ''}"
            if parsed.port:
                netloc += f":{parsed.port}"
            proxy = urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
        normalized["proxy"] = proxy
    return normalized


class ExperimentalFlagsManager(LocaleMixin):

    DEFAULT_VALUES = {
        "chats_per_page": 10,
        "multiple_slave_chats": True,
        "network_error_prompt_interval": 100,
        "prevent_message_removal": True,
        "auto_locale": True,
        "retry_on_error": False,
        "send_image_as_file": False,
        "message_muted_on_slave": "normal",
        "your_message_on_slave": "silent",
        "animated_stickers": False,
        "send_to_last_chat": "warn",
        "default_media_prompt": "emoji",
        "api_base_url": None,
        "api_base_file_url": None,
        "local_tdlib_api": False,
        "topic_group": None,
    }

    @staticmethod
    def get_temp_dir(channel: 'TelegramChannel') -> Optional[str]:
        """Get the temp directory for creating temporary files.

        When using local TDLIB API with Docker, temp files need to be created
        in a directory that is shared between the host and the container.

        Extracts the directory path from api_base_file_url if configured.
        For example: file:///var/lib/telegram-bot-api -> /var/lib/telegram-bot-api

        Returns:
            The temp directory path if local_tdlib_api and api_base_file_url are configured,
            otherwise None (use system default).
        """
        if channel.flag("local_tdlib_api") and channel.flag("api_base_file_url"):
            from urllib.parse import urlparse
            parsed = urlparse(channel.flag("api_base_file_url"))
            if parsed.scheme == "file" and parsed.path:
                return parsed.path
        return None

    def __init__(self, channel: 'TelegramChannel'):
        self.channel = channel
        self.config: Dict[str, Any] = ExperimentalFlagsManager.DEFAULT_VALUES.copy()
        self.config.update(channel.config.get('flags', dict()) or dict())
        if self.config.get("topic_group") is None and channel.config.get("topic_group") is not None:
            self.config["topic_group"] = channel.config["topic_group"]

    def __call__(self, flag_key: str) -> Any:
        if flag_key not in self.config:
            raise ValueError(self._("{0} is not a valid experimental flag").format(flag_key))
        return self.config[flag_key]


def b64en(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def b64de(s: str) -> str:
    missing_padding = len(s) % 4
    if missing_padding:
        s += '=' * (4 - missing_padding)
    return base64.urlsafe_b64decode(s).decode()


def message_id_to_str(chat_id: Optional[TelegramChatID] = None,
                      message_id: Optional[TelegramMessageID] = None,
                      update: Optional[telegram.Update] = None) -> TgChatMsgIDStr:
    """
    Convert an unique identifier of Telegram message to a string.

    Args:
        update: PTB update object, provide either this or the other 2 below
        chat_id: Chat ID
        message_id: Message ID

    Returns:
        String representation of the message ID
    """
    if update and (chat_id or message_id):
        raise ValueError("update and (chat_id, message_id) is mutual exclusive.")
    if not update and not (chat_id and message_id):
        raise ValueError("Either update or (chat_id, message_id) is to be provided.")
    if update and update.effective_message and update.effective_chat:
        chat_id = TelegramChatID(update.effective_chat.id)
        message_id = TelegramMessageID(update.effective_message.message_id)
    return TgChatMsgIDStr(f"{chat_id}.{message_id}")


def message_id_str_to_id(s: TgChatMsgIDStr) -> Tuple[TelegramChatID, TelegramMessageID]:
    """
    Reverse of message_id_to_str.
    Returns:
        chat_id, message_id
    """
    msg_ids = s.split(".", 1)
    return TelegramChatID(int(msg_ids[0])), TelegramMessageID(int(msg_ids[1]))


def chat_id_to_str(channel_id: Optional[ModuleID] = None, chat_uid: Optional[ChatID] = None,
                   group_id: Optional[ChatID] = None,
                   chat: Optional[BaseChat] = None, channel: Optional[Channel] = None) -> EFBChannelChatIDStr:
    """
    Convert an unique identifier to EFB chat to a string.

    (chat|((channel|channel_id), chat_uid)) must be provided.

    Returns:
        String representation of the chat
    """

    if not chat and not chat_uid:
        raise ValueError("Either chat or the other set must be provided.")
    if chat and chat_uid:
        raise ValueError("Either chat or the other set must be provided, but not both.")
    if chat_uid and channel_id and channel:
        raise ValueError("channel_id and channel is mutual exclusive.")

    if chat:
        channel_id = chat.module_id
        chat_uid = chat.uid
        if isinstance(chat, ChatMember):
            group_id = chat.chat.uid
    if channel:
        channel_id = channel.channel_id
    if group_id is None:
        return EFBChannelChatIDStr(f"{channel_id} {chat_uid}")

    return EFBChannelChatIDStr(f"{channel_id} {chat_uid} {group_id}")


def chat_id_str_to_id(s: EFBChannelChatIDStr) -> Tuple[ModuleID, ChatID, Optional[ChatID]]:
    """
    Reverse of chat_id_to_str.
    Returns:
        channel_id, chat_uid, group_id
    """
    ids = s.split(" ", 2)
    channel_id = ModuleID(ids[0])
    chat_uid = ChatID(ids[1])
    if len(ids) < 3:
        group_id = None
    else:
        group_id = ChatID(ids[2])
    return channel_id, chat_uid, group_id


def _copy_binary_stream(src: BinaryIO, dst: BinaryIO, chunk_size: int = 64 * 1024) -> None:
    while True:
        chunk = src.read(chunk_size)
        if not chunk:
            break
        dst.write(chunk)


def _run_ffmpeg_command(args, *, input_data: Optional[bytes] = None) -> bytes:
    try:
        completed = subprocess.run(
            args,
            input=input_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=FFMPEG_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"ffmpeg command timed out after {FFMPEG_TIMEOUT} seconds") from exc
    if completed.returncode != 0:
        raise ffmpeg.Error(args[0], completed.stdout, completed.stderr)
    return cast(bytes, completed.stdout)


def _write_stream_to_process(stream: IO[bytes], process: subprocess.Popen) -> None:
    assert process.stdin
    try:
        _copy_binary_stream(cast(BinaryIO, stream), cast(BinaryIO, process.stdin))
    except Exception:
        process.kill()
        raise
    finally:
        process.stdin.close()


def _read_process_stream(stream: IO[bytes], output: BytesIO) -> None:
    _copy_binary_stream(cast(BinaryIO, stream), output)


def _run_ffmpeg_stream_command(args, input_stream: IO[bytes]) -> bytes:
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout = BytesIO()
    stderr = BytesIO()
    try:
        from threading import Thread

        assert process.stdout
        assert process.stderr
        writer = Thread(target=_write_stream_to_process, args=(input_stream, process), daemon=True)
        stdout_reader = Thread(target=_read_process_stream, args=(process.stdout, stdout), daemon=True)
        stderr_reader = Thread(target=_read_process_stream, args=(process.stderr, stderr), daemon=True)
        writer.start()
        stdout_reader.start()
        stderr_reader.start()
        process.wait(timeout=FFMPEG_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise TimeoutError(f"ffmpeg command timed out after {FFMPEG_TIMEOUT} seconds") from exc
    writer.join(timeout=1)
    stdout_reader.join(timeout=1)
    stderr_reader.join(timeout=1)
    out = stdout.getvalue()
    err = stderr.getvalue()
    if process.returncode != 0:
        raise ffmpeg.Error(args[0], out, err)
    return out


def export_gif(animation, fp, dpi=96, skip_frames=5):
    """ Fork of lottie.exporters.gif.export_gif
    Adapted from jqqqqqqqqqq/UnifiedMessageRelay
    https://github.com/jqqqqqqqqqq/UnifiedMessageRelay/blob/c920d005714a33fbd50594ef8013ce7ec2f3b240/src/Core/UMRFile.py#L141
    License:
        MIT (Unified Message Relay)
        AGPL 3.0 (Python Lottie)
    """
    # Import only upon calling the method due to added binary dependencies
    # (libcairo)
    from lottie.exporters.cairo import export_png
    from lottie.exporters.gif import _png_gif_prepare

    start = int(animation.in_point)
    end = int(animation.out_point)
    frames = []
    for i in range(start, end+1, skip_frames):
        file = BytesIO()
        export_png(animation, file, i, dpi)
        file.seek(0)
        frames.append(_png_gif_prepare(Image.open(file)))

    duration = 1000 / animation.frame_rate * (1 + skip_frames) / 2
    frames[0].save(
        fp,
        format='GIF',
        append_images=frames[1:],
        save_all=True,
        duration=duration,
        loop=0,
        transparency=255,
        disposal=2,
    )


def convert_tgs_to_gif(tgs_file: BinaryIO, gif_file: BinaryIO) -> bool:
    # Import only upon calling the method due to added binary dependencies
    # (libcairo)
    from lottie.parsers.tgs import parse_tgs

    # noinspection PyBroadException
    try:
        animation = parse_tgs(tgs_file)
        # heavy_strip(animation)
        # heavy_strip(animation)
        # animation.tgs_sanitize()
        export_gif(animation, gif_file, skip_frames=5, dpi=48)
        return True
    except Exception:
        logging.exception("Error occurred while converting TGS to GIF.")
        return False


if os.name == "nt":
    # Workaround for Windows which cannot open the same file as "read" twice.
    # Using stdin/stdout pipe for IO with ffmpeg.
    # Said to be only working with a few encodings. It seems that Telegram GIF
    # (MP4, h264, soundless) luckily felt in that range.
    #
    # See: https://etm.1a23.studio/issues/90

    def ffprobe(stream: IO[bytes], cmd='ffprobe', **kwargs):
        """Run ffprobe on an input stream and return a JSON representation of the output.

        Code adopted from ffmpeg-python by Karl Kroening (Apache License 2.0).
        Copyright 2017 Karl Kroening

        Raises:
            :class:`ffmpeg.Error`: if ffprobe returns a non-zero exit code,
                an :class:`Error` is returned with a generic error message.
                The stderr output can be retrieved by accessing the
                ``stderr`` property of the exception.
        """
        args = [cmd, '-show_format', '-show_streams', '-of', 'json']
        args += convert_kwargs_to_cmd_line_args(kwargs)
        args += ["-"]

        out = _run_ffmpeg_stream_command(args, stream)
        return json.loads(out.decode('utf-8'))


    def gif_conversion(file: IO[bytes], channel_id: str) -> IO[bytes]:
        """Convert Telegram GIF to real GIF, the NT way."""
        gif_file = NamedTemporaryFile(suffix='.gif')
        file.seek(0)

        # Use custom ffprobe command to read from stream
        metadata = ffprobe(file)

        # Set input/output of ffmpeg to stream
        stream = ffmpeg.input("pipe:")
        if channel_id.startswith("blueset.wechat") and metadata.get('width', 0) > 600:
            # Workaround: Compress GIF for slave channel `blueset.wechat`
            # TODO: Move this logic to `blueset.wechat` in the future
            stream = stream.filter("scale", 600, -2)
        # Need to specify file format here as no extension hint presents.
        args = stream.output("pipe:", format="gif").compile()
        file.seek(0)

        # subprocess.Popen would still try to access the file handle instead of
        # using standard IO interface. Not sure if that would work on Windows.
        # Using the most classic buffer and copy via IO interface just to play
        # safe.
        gif_file.write(_run_ffmpeg_stream_command(args, file))
        file.close()
        gif_file.seek(0)
        return gif_file

else:
    def gif_conversion(file: IO[bytes], channel_id: str) -> IO[bytes]:
        """Convert Telegram GIF to real GIF, the non-NT way."""
        gif_file = NamedTemporaryFile(suffix='.gif')
        file.seek(0)
        metadata = ffmpeg.probe(file.name, timeout=FFMPEG_TIMEOUT)
        stream = ffmpeg.input(file.name)
        if channel_id.startswith("blueset.wechat") and metadata.get('width', 0) > 600:
            # Workaround: Compress GIF for slave channel `blueset.wechat`
            # TODO: Move this logic to `blueset.wechat` in the future
            stream = stream.filter("scale", 600, -2)
        args = stream.output(gif_file.name).overwrite_output().compile()
        _run_ffmpeg_command(args)
        file.close()
        gif_file.seek(0)
        return gif_file
