import re
import datetime
import time
import os
from pathlib import Path
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
    assert format_message_time(aware, default.timezone) == "22:39:38"
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


def test_aggregation_chat_type_configuration():
    assert ExperimentalFlagsManager(SimpleNamespace(config={}))("text_aggregation_chat_types") is None
    for allowed in (None, [], ["group"], ["group", "private", "system", "official_account"]):
        flags = ExperimentalFlagsManager(SimpleNamespace(config={"flags": {"text_aggregation_chat_types": allowed}}))
        assert flags("text_aggregation_chat_types") == allowed
    for invalid in ("group", [1], [""], ["GroupChat"], ["unknown"]):
        with raises(ValueError, match="flags.text_aggregation_chat_types"):
            ExperimentalFlagsManager(SimpleNamespace(config={"flags": {"text_aggregation_chat_types": invalid}}))


def test_aggregation_rule_configuration():
    assert ExperimentalFlagsManager(SimpleNamespace(config={}))("text_aggregation_rules") is None
    for allowed in (None, [], [{"origins": ["tests.qq"], "chat_types": ["group"]}]):
        flags = ExperimentalFlagsManager(SimpleNamespace(config={"flags": {"text_aggregation_rules": allowed}}))
        assert flags("text_aggregation_rules") == allowed
    for invalid in (
        "group", [None], [{"origins": ["tests.qq"]}],
        [{"origins": [], "chat_types": ["group"]}],
        [{"origins": [1], "chat_types": ["group"]}],
        [{"origins": ["tests.qq"], "chat_types": []}],
        [{"origins": ["tests.qq"], "chat_types": ["unknown"]}],
        [{"origins": ["tests.qq"], "chat_types": ["group"], "extra": True}],
    ):
        with raises(ValueError, match="flags.text_aggregation_rules"):
            ExperimentalFlagsManager(SimpleNamespace(config={"flags": {"text_aggregation_rules": invalid}}))


@pytest.mark.skipif(os.name == "nt", reason="Uses the non-Windows file conversion path")
def test_gif_conversion_uses_process_timeout(monkeypatch, tmp_path):
    from efb_telegram_master import utils

    source = tmp_path / "animation.mp4"
    source.write_bytes(b"video input")

    def run(args, **kwargs):
        assert kwargs["timeout"] == 60
        assert "-timeout" not in args
        if args[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=b'{"streams": [], "format": {}}', stderr=b"")
        Path(args[-2]).write_bytes(b"GIF89a")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(utils.subprocess, "run", run)
    with source.open("rb") as input_file:
        with utils.gif_conversion(input_file, "tests.slave") as output:
            assert output.read() == b"GIF89a"
        assert input_file.closed


def _load_windows_utils(monkeypatch):
    """Load a copy of the utils module that defines the Windows variants."""
    import importlib.util

    from efb_telegram_master import utils

    spec = importlib.util.spec_from_file_location("efb_telegram_master._utils_windows", utils.__file__)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as patch:
        patch.setattr(os, "name", "nt")
        spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name == "nt", reason="Uses the non-Windows file conversion path")
@pytest.mark.parametrize("first_size, retried", [(1_000_001, True), (1_000_000, False)])
def test_gif_conversion_retries_once_at_12_fps_only_when_over_limit(monkeypatch, tmp_path, first_size, retried):
    from efb_telegram_master import utils

    source = tmp_path / "animation.mp4"
    source.write_bytes(b"video input")
    conversions = []

    def run(args, **kwargs):
        if args[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=b'{"streams": [], "format": {}}', stderr=b"")
        conversions.append(args)
        size = first_size if len(conversions) == 1 else 10
        Path(args[-2]).write_bytes(b"\0" * size)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(utils.subprocess, "run", run)
    with source.open("rb") as input_file, utils.gif_conversion(input_file, "tests.slave") as output:
        assert len(output.read()) == (10 if retried else first_size)

    assert len(conversions) == (2 if retried else 1)
    assert "fps=fps=12" not in " ".join(conversions[0])
    if retried:
        assert "fps=fps=12" in " ".join(conversions[1])


@pytest.mark.parametrize("first_size, retried", [(1_000_001, True), (1_000_000, False)])
def test_windows_gif_conversion_retries_once_at_12_fps_only_when_over_limit(monkeypatch, first_size, retried):
    utils = _load_windows_utils(monkeypatch)
    conversions = []

    def ffprobe(stream):
        return {}

    def run(args, input_stream):
        conversions.append(args)
        assert input_stream.read() == b"video input"
        return b"\0" * (first_size if len(conversions) == 1 else 10)

    monkeypatch.setattr(utils, "ffprobe", ffprobe)
    monkeypatch.setattr(utils, "_run_ffmpeg_stream_command", run)
    output = utils.gif_conversion(BytesIO(b"video input"), "tests.slave")
    assert len(output.read()) == (10 if retried else first_size)
    output.close()

    assert len(conversions) == (2 if retried else 1)
    assert "fps=fps=12" not in " ".join(conversions[0])
    if retried:
        assert "fps=fps=12" in " ".join(conversions[1])


def test_ffmpeg_stream_command_waits_for_exit_before_reading_the_exit_code(monkeypatch):
    from efb_telegram_master import utils

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdin = BytesIO()
            self.stdout = BytesIO(b"output")
            self.stderr = BytesIO(b"failed")
            self._returncode = None

        @property
        def returncode(self):
            return self._returncode

        def wait(self, timeout=None):
            self._returncode = 1
            return self._returncode

    monkeypatch.setattr(utils.subprocess, "Popen", FakeProcess)
    with raises(utils.ffmpeg.Error):
        utils._run_ffmpeg_stream_command(["ffmpeg"], BytesIO(b"input"))


@pytest.mark.parametrize("flag_name", ["topic_channel_emoji", "topic_chat_type_emoji"])
def test_topic_emoji_flags_default_on_and_reject_non_booleans(flag_name):
    assert ExperimentalFlagsManager(SimpleNamespace(config={}))(flag_name) is True
    assert ExperimentalFlagsManager(SimpleNamespace(config={"flags": {flag_name: False}}))(flag_name) is False
    for bad_value in ("false", 0, None):
        with raises(ValueError, match=flag_name):
            ExperimentalFlagsManager(SimpleNamespace(config={"flags": {flag_name: bad_value}}))


def test_wechat_quote_reply_flag_defaults_off_and_rejects_non_booleans():
    assert ExperimentalFlagsManager(SimpleNamespace(config={}))("wechat_quote_reply") is False
    assert ExperimentalFlagsManager(SimpleNamespace(config={"flags": {"wechat_quote_reply": True}}))("wechat_quote_reply") is True
    for bad_value in ("true", 1, None):
        with raises(ValueError, match="wechat_quote_reply"):
            ExperimentalFlagsManager(SimpleNamespace(config={"flags": {"wechat_quote_reply": bad_value}}))
