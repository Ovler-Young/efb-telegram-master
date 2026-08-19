"""Prepare slave attachments for Telegram uploads."""

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import IO, Callable, Optional, Union

import humanize
import telegram.constants


class SlaveFileTransfer:
    """Validate attachments and copy local-TDLib uploads into its shared directory."""

    def __init__(self, flag, bot, logger, translate, temp_directory: Callable[[], Optional[str]]) -> None:
        self.flag = flag
        self.bot = bot
        self.logger = logger
        self.translate = translate
        self.temp_directory = temp_directory

    def check_size(self, file: Optional[IO[bytes]]) -> Optional[str]:
        if not file or getattr(file, "closed", True):
            return None
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        if not self.flag("local_tdlib_api") and file_size > telegram.constants.FileSizeLimit.FILESIZE_UPLOAD:
            return self.translate("Attachment is too large ({size}). Maximum allowed by Telegram Bot API is {max_size}. (AT02)").format(
                size=humanize.naturalsize(file_size),
                max_size=humanize.naturalsize(telegram.constants.FileSizeLimit.FILESIZE_UPLOAD),
            )
        return None

    @staticmethod
    def _content_suffix(file: IO[bytes]) -> str:
        position = file.tell()
        try:
            file.seek(0)
            header = file.read(12)
        finally:
            file.seek(position)
        for prefix, suffix in ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF87a", ".gif"), (b"GIF89a", ".gif"), (b"OggS", ".ogg"), (b"%PDF-", ".pdf")):
            if header.startswith(prefix):
                return suffix
        return ".webp" if header.startswith(b"RIFF") and header[8:12] == b"WEBP" else ".mp4" if header[4:8] == b"ftyp" else ""

    def prepare(self, file: IO[bytes], path: Union[str, Path], filename: Optional[str] = None) -> Union[IO[bytes], str]:
        if not self.flag("local_tdlib_api"):
            return file
        source = Path(path).absolute()
        temp_dir = self.temp_directory()
        if temp_dir is None:
            return source.as_uri()
        original_position = file.tell()
        try:
            source.relative_to(temp_dir)
        except ValueError:
            suffix = Path(filename).suffix if filename else source.suffix
            suffix = suffix or self._content_suffix(file)
            with tempfile.NamedTemporaryFile(suffix=suffix, dir=temp_dir, delete=False) as destination:
                destination_path = Path(destination.name)
            if filename:
                candidate = Path(temp_dir, os.path.basename(filename)).with_suffix(suffix)
                if not candidate.exists():
                    destination_path.unlink()
                    destination_path = candidate
                else:
                    destination_path = candidate.with_stem(f"{candidate.stem}_{uuid.uuid4().hex[:8]}")
            try:
                file.seek(0)
                with destination_path.open("wb") as destination:
                    shutil.copyfileobj(file, destination)
            finally:
                file.seek(original_position)
            os.chmod(destination_path, 0o644)
            self.bot.register_upload_cleanup(os.fspath(destination_path))
            self.logger.debug("Copied file to shared temporary directory.")
            source = destination_path
        return source.as_uri()
