# coding=utf-8

import json
import logging
import os
import subprocess
from io import BytesIO
from tempfile import NamedTemporaryFile
from typing import IO, BinaryIO, Optional, cast

import ffmpeg
from ffmpeg._utils import convert_kwargs_to_cmd_line_args
from PIL import Image

FFMPEG_TIMEOUT = 60


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
    """Fork of lottie.exporters.gif.export_gif.

    Adapted from jqqqqqqqqqq/UnifiedMessageRelay:
    https://github.com/jqqqqqqqqqq/UnifiedMessageRelay/blob/c920d005714a33fbd50594ef8013ce7ec2f3b240/src/Core/UMRFile.py#L141

    License: MIT (Unified Message Relay), AGPL 3.0 (Python Lottie).
    """
    from lottie.exporters.cairo import export_png
    from lottie.exporters.gif import _png_gif_prepare

    start = int(animation.in_point)
    end = int(animation.out_point)
    frames = []
    for i in range(start, end + 1, skip_frames):
        file = BytesIO()
        export_png(animation, file, i, dpi)
        file.seek(0)
        frames.append(_png_gif_prepare(Image.open(file)))

    duration = 1000 / animation.frame_rate * (1 + skip_frames) / 2
    frames[0].save(
        fp,
        format="GIF",
        append_images=frames[1:],
        save_all=True,
        duration=duration,
        loop=0,
        transparency=255,
        disposal=2,
    )


def convert_tgs_to_gif(tgs_file: BinaryIO, gif_file: BinaryIO) -> bool:
    from lottie.parsers.tgs import parse_tgs

    try:
        animation = parse_tgs(tgs_file)
        export_gif(animation, gif_file, skip_frames=5, dpi=48)
        return True
    except Exception:
        logging.exception("Error occurred while converting TGS to GIF.")
        return False


def _maybe_scale_wechat_gif(stream, channel_id: str, metadata: dict):
    if channel_id.startswith("blueset.wechat") and metadata.get("width", 0) > 600:
        return stream.filter("scale", 600, -2)
    return stream


if os.name == "nt":

    def ffprobe(stream: IO[bytes], cmd="ffprobe", **kwargs):
        """Run ffprobe on a stream and return its JSON output."""
        args = [cmd, "-show_format", "-show_streams", "-of", "json"]
        args += convert_kwargs_to_cmd_line_args(kwargs)
        args += ["-"]
        out = _run_ffmpeg_stream_command(args, stream)
        return json.loads(out.decode("utf-8"))

    def gif_conversion(file: IO[bytes], channel_id: str) -> IO[bytes]:
        """Convert Telegram GIF to GIF through Windows-compatible stream handles."""
        gif_file = NamedTemporaryFile(suffix=".gif")
        file.seek(0)
        metadata = ffprobe(file)
        stream = _maybe_scale_wechat_gif(ffmpeg.input("pipe:"), channel_id, metadata)
        args = stream.output("pipe:", format="gif").compile()
        file.seek(0)
        gif_file.write(_run_ffmpeg_stream_command(args, file))
        file.close()
        gif_file.seek(0)
        return gif_file

else:

    def gif_conversion(file: IO[bytes], channel_id: str) -> IO[bytes]:
        """Convert Telegram GIF to GIF through its temporary file."""
        gif_file = NamedTemporaryFile(suffix=".gif")
        file.seek(0)
        metadata = ffmpeg.probe(file.name, timeout=FFMPEG_TIMEOUT)
        stream = _maybe_scale_wechat_gif(ffmpeg.input(file.name), channel_id, metadata)
        args = stream.output(gif_file.name).overwrite_output().compile()
        _run_ffmpeg_command(args)
        file.close()
        gif_file.seek(0)
        return gif_file
