import re
import datetime
import time
from types import SimpleNamespace
from io import BytesIO

import pytest
from pytest import raises

from efb_telegram_master.utils import b64de, b64en, message_id_to_str, \
    message_id_str_to_id, chat_id_str_to_id, chat_id_to_str, convert_tgs_to_gif, \
    ExperimentalFlagsManager, format_message_time


def test_flag(channel):
    flag = channel.flag
    with raises(ValueError, match="__unknown_flag__"):
        flag("__unknown_flag__")

    assert flag("chats_per_page") is not None, "Existing flag should return a value"


def test_message_timezone_configuration_and_saved_timestamp_display(monkeypatch):
    default = ExperimentalFlagsManager(SimpleNamespace(config={}))
    configured = ExperimentalFlagsManager(SimpleNamespace(config={"flags": {"timezone": "America/Los_Angeles"}}))
    assert default("timezone") == "Asia/Shanghai"
    aware = datetime.datetime(2026, 10, 8, 14, 39, 38, tzinfo=datetime.timezone.utc)
    assert format_message_time(aware, default.timezone) == "10:39:38"
    assert format_message_time(aware, configured.timezone) == "07:39:38"
    assert format_message_time(aware - datetime.timedelta(hours=12), default.timezone) == "10:39:38"
    with raises(ValueError, match="flags.timezone.*IANA"):
        ExperimentalFlagsManager(SimpleNamespace(config={"flags": {"timezone": "not/a-zone"}}))
    # Production stores naive host-local receive times. Interpret them in the
    # host zone before conversion, without changing the saved datetime.
    naive = aware.replace(tzinfo=None)
    try:
        with monkeypatch.context() as local:
            local.setenv("TZ", "America/Los_Angeles")
            time.tzset()
            assert format_message_time(naive, default.timezone) == "05:39:38"
            assert naive.tzinfo is None
    finally:
        time.tzset()


def test_url_safe_base64():
    data = "信じたものは、\n都合の良い妄想を繰り返し映し出す鏡。"
    assert b64de(b64en(data)) == data
    # Per docs, encoded base64 for startgroup shall only consist of [a-zA-Z0-9_-]+
    # https://core.telegram.org/bots
    encoded = b64en(data)
    assert re.match(r"^[a-zA-Z0-9_-]+$", encoded)


def test_message_id_str_conversion():
    chat_id = 1
    message_id = 2
    assert (chat_id, message_id) == message_id_str_to_id(
        message_id_to_str(chat_id=chat_id, message_id=message_id))


def test_chat_id_str_conversion():
    channel_id = "__channel_id__"
    chat_id = "__chat_id__"
    group_id = "__group_id__"

    assert (channel_id, chat_id, None) == chat_id_str_to_id(
        chat_id_to_str(channel_id=channel_id, chat_uid=chat_id)
    ), "Converting channel-chat ID without group ID"

    assert (channel_id, chat_id, group_id) == chat_id_str_to_id(
        chat_id_to_str(channel_id=channel_id, chat_uid=chat_id, group_id=group_id)
    ), "Converting channel-chat ID with group ID"


def test_convert_tgs_to_gif():
    try:
        from lottie.exporters.cairo import export_png  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment dependent optional backend
        pytest.skip(f"TGS raster backend is unavailable in this environment: {exc}")

    out = BytesIO()
    with open('tests/mocks/AnimatedSticker.tgs', 'rb') as f:
        assert convert_tgs_to_gif(f, out), "conversion outcome"
    assert out.seek(0, 2), "converted TGS file should not be empty"


def test_aggregation_origin_configuration():
    assert ExperimentalFlagsManager(SimpleNamespace(config={}))("text_aggregation_origins") is None
    for invalid in ("tests.source", [1], [""]):
        with raises(ValueError, match="flags.text_aggregation_origins"):
            ExperimentalFlagsManager(SimpleNamespace(config={"flags": {"text_aggregation_origins": invalid}}))
