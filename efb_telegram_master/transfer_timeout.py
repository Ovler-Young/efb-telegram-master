"""Size-based transfer budgets shared by Telegram uploads and downloads."""
import math
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlsplit
from typing import overload

from telegram import InputFile, InputMedia


def parse_transfer_min_mbps(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('transfer_min_mbps must be a positive finite number.')
    rate = float(value)
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError('transfer_min_mbps must be a positive finite number.')
    return rate


@overload
def transfer_timeout(base: float, size: int | None, mbps: float | None) -> float: ...


@overload
def transfer_timeout(base: None, size: int | None, mbps: float | None) -> None: ...


def transfer_timeout(base: float | None, size: int | None, mbps: float | None) -> float | None:
    if base is None or mbps is None or not size:
        return base
    return base + size * 8 / (mbps * 1_000_000)


def media_size(value: object) -> int:
    """Inspect media without reading its contents or changing stream positions."""
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, InputFile):
        return media_size(value.input_file_content)
    if isinstance(value, InputMedia):
        return sum(media_size(getattr(value, field, None))
                   for field in ('media', 'thumbnail', 'cover', 'photo'))
    if isinstance(value, Mapping):
        return sum(media_size(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(media_size(item) for item in value)
    if isinstance(value, (str, Path)):
        if isinstance(value, str):
            try:
                parsed = urlsplit(value)
            except ValueError:
                return 0
            if parsed.scheme and parsed.scheme != 'file':
                return 0
            value = Path(unquote(parsed.path)) if parsed.scheme == 'file' else Path(value)
        try:
            return value.stat().st_size if value.is_file() else 0
        except (OSError, ValueError):
            return 0
    try:
        return os.fstat(getattr(value, 'fileno')()).st_size
    except (AttributeError, OSError, ValueError):
        pass
    try:
        position = getattr(value, 'tell')()
        getattr(value, 'seek')(0, os.SEEK_END)
        try:
            return getattr(value, 'tell')()
        finally:
            getattr(value, 'seek')(position)
    except (AttributeError, OSError, ValueError):
        return 0
