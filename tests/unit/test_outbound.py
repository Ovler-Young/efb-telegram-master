from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import sqlite3
import tempfile
import threading

import pytest
from telegram import (
    InputFile,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaLivePhoto,
    InputMediaPhoto,
    InputMediaVideo,
    PhotoSize,
)

from efb_telegram_master.outbound import (
    InvalidQueuedPayloadError,
    OutboundQueue,
    OutboundQueueScheduler,
    QueueEnqueueError,
    QueueRequest,
    SchedulerStoppedError,
    SenderSelection,
    SenderSelectionResult,
)


def send_message(chat_id, text):
    return (chat_id, text)


def edit_message_text(chat_id, message_id, text):
    return (chat_id, message_id, text)


def operation(name):
    return {"send_message": send_message, "edit_message_text": edit_message_text}[name]


def media_operation(chat_id, media=None, **kwargs):
    return chat_id, media, kwargs


def edit_media_operation(media, chat_id, message_id, **kwargs):
    return media, chat_id, message_id, kwargs


def enqueue(queue, *requests):
    return queue.enqueue_many(requests, operation)


def test_queue_schema_wal_and_restart_retention(tmp_path):
    queue = OutboundQueue(tmp_path)
    row_id, _waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 12, "text": "first"}))
    assert queue.path == tmp_path / "outbound-queue.sqlite3"
    assert queue.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert queue.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert queue.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'outbound_queue_destination_priority_id'"
    ).fetchone()[0]
    queue.close()

    restarted = OutboundQueue(tmp_path)
    assert [row.id for row in restarted.heads()] == [row_id]
    restarted.delete(row_id)
    restarted.close()
    assert OutboundQueue(tmp_path).heads() == []


def test_payload_version_round_trip_and_invalid_shapes(tmp_path):
    queue = OutboundQueue(tmp_path)
    payload = queue.encode_payload((1,), {"chat_id": 2, "value": {"x": 1}})
    assert queue.decode_payload(payload) == ((1,), {"chat_id": 2, "value": {"x": 1}})
    for invalid in (b"\x02value", b"\x01not-a-pickle", b"\x01\x80\x05K\x01."):
        with pytest.raises(InvalidQueuedPayloadError):
            queue.decode_payload(invalid)
    with pytest.raises(QueueEnqueueError):
        queue.encode_payload((), {"value": threading.Lock()})


def test_media_snapshot_preserves_caller_stream_lifecycle_and_decodes_fresh_values(tmp_path):
    queue = OutboundQueue(tmp_path)
    caller_stream = io.BufferedReader(io.BytesIO(b"complete media"))
    caller_stream.seek(5)

    snapshot = queue._snapshot_media_value(caller_stream)
    payload = queue.encode_payload((), {"media": snapshot})

    assert caller_stream.tell() == 5
    assert not caller_stream.closed
    caller_stream.close()

    first = queue.decode_payload(payload)[1]["media"]
    second = queue.decode_payload(payload)[1]["media"]
    assert first is not second
    assert first.tell() == second.tell() == 0
    assert first.read() == second.read() == b"complete media"


class _UnreadableStream(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.seek(2)

    def read(self, size=-1):
        raise OSError("read failed")


class _UntellableStream(io.BytesIO):
    def tell(self):
        raise OSError("tell failed")


class _UnrestorableStream(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        super().seek(2)

    def seek(self, offset, whence=0):
        if offset == 2 and whence == 0:
            raise OSError("restore failed")
        return super().seek(offset, whence)


@pytest.mark.parametrize(
    "stream",
    [_UntellableStream(b"media"), _UnreadableStream(b"media"), _UnrestorableStream(b"media")],
)
def test_media_snapshot_classifies_stream_read_and_restore_failures(stream):
    with pytest.raises(QueueEnqueueError, match="Unable to serialize queued Telegram call"):
        OutboundQueue._snapshot_media_value(stream)

    assert not stream.closed


def test_media_snapshot_restores_position_after_read_failure():
    stream = _UnreadableStream(b"media")

    with pytest.raises(QueueEnqueueError):
        OutboundQueue._snapshot_media_value(stream)

    assert stream.tell() == 2


def _buffered_media(content, random_access):
    if random_access:
        stream = tempfile.TemporaryFile()
        stream.write(content)
        stream.seek(0)
        return stream
    return io.BufferedReader(io.BytesIO(content))


@pytest.mark.parametrize("keyword", [False, True], ids=["positional", "keyword"])
@pytest.mark.parametrize(
    ("operation_name", "media_key", "content", "random_access"),
    [
        ("send_photo", "photo", b"small image", False),
        ("send_document", "document", b"large image", True),
        ("send_sticker", "sticker", b"sticker", False),
        ("send_document", "document", b"file", True),
        ("send_video", "video", b"video", False),
        ("send_voice", "voice", b"voice", True),
    ],
    ids=["small-image", "large-image", "sticker", "file", "video", "voice"],
)
def test_initial_send_media_streams_enqueue_as_inline_version_one_snapshots(
    tmp_path, operation_name, media_key, content, random_access, keyword
):
    queue = OutboundQueue(tmp_path)
    caller_stream = _buffered_media(content, random_access)
    caller_stream.seek(2)
    args = () if keyword else (100, caller_stream)
    kwargs = {
        "disable_notification": True,
        **({"chat_id": 100, media_key: caller_stream} if keyword else {}),
    }

    queue.enqueue_many(
        [QueueRequest(operation_name, args, kwargs)],
        lambda _name: media_operation,
    )

    assert caller_stream.tell() == 2
    assert not caller_stream.closed
    caller_stream.close()
    row = queue.heads()[0]
    decoded_args, decoded_kwargs = queue.decode_payload(row.payload)
    decoded_media = decoded_kwargs[media_key] if keyword else decoded_args[1]
    assert row.payload[0] == 1
    assert decoded_media.tell() == 0
    assert decoded_media.read() == content
    assert decoded_kwargs["disable_notification"] is True


def test_direct_media_normalization_failure_and_non_media_stream_commit_zero_rows(tmp_path):
    queue = OutboundQueue(tmp_path)
    requests = [
        QueueRequest("send_photo", (100, _UnreadableStream(b"photo")), {}),
        QueueRequest("send_message", (100, io.BufferedReader(io.BytesIO(b"not text"))), {}),
    ]

    for request in requests:
        resolver = (lambda _name: media_operation) if request.operation == "send_photo" else operation
        with pytest.raises(QueueEnqueueError, match="Unable to serialize queued Telegram call"):
            queue.enqueue_many([request], resolver)

    assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 0
    for request in requests:
        request.args[1].close()


def _open_input_file(content, filename):
    stream = io.BufferedReader(io.BytesIO(content))
    stream.seek(1)
    return InputFile(stream, filename=filename, attach=True, read_file_handle=False), stream


def test_thumbnail_keyword_enqueues_an_inline_snapshot(tmp_path):
    queue = OutboundQueue(tmp_path)
    thumbnail = io.BufferedReader(io.BytesIO(b"thumbnail"))
    thumbnail.seek(3)

    queue.enqueue_many(
        [QueueRequest("send_document", (100, b"document"), {"thumbnail": thumbnail})],
        lambda _name: media_operation,
    )

    assert thumbnail.tell() == 3
    thumbnail.close()
    decoded_thumbnail = queue.decode_payload(queue.heads()[0].payload)[1]["thumbnail"]
    assert decoded_thumbnail.tell() == 0
    assert decoded_thumbnail.read() == b"thumbnail"


@pytest.mark.parametrize(
    ("kind", "expected_filename"),
    [
        ("stream", "stream-cover.jpg"),
        ("bytes", None),
        ("input-file", "input-cover.jpg"),
        ("local-string", "string-cover.jpg"),
        ("local-path", "path-cover.jpg"),
    ],
)
def test_video_cover_enqueues_an_inline_version_one_snapshot(
    tmp_path, kind, expected_filename
):
    queue = OutboundQueue(tmp_path)
    content = f"{kind} cover".encode()
    source = None
    local_path = None
    if kind == "stream":
        local_path = tmp_path / expected_filename
        local_path.write_bytes(content)
        source = local_path.open("rb")
        source.seek(2)
        cover = source
    elif kind == "bytes":
        cover = content
    elif kind == "input-file":
        cover = InputFile(content, filename=expected_filename)
    else:
        local_path = tmp_path / expected_filename
        local_path.write_bytes(content)
        cover = str(local_path) if kind == "local-string" else local_path

    queue.enqueue_many(
        [QueueRequest(
            "send_video",
            (100, b"video"),
            {"cover": cover, "disable_notification": True},
        )],
        lambda _name: media_operation,
    )

    if source is not None:
        assert source.tell() == 2
        assert not source.closed
        source.close()
    if local_path is not None:
        local_path.unlink()
    row = queue.heads()[0]
    decoded_cover = queue.decode_payload(row.payload)[1]["cover"]
    assert row.payload[0] == 1
    assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 1
    if isinstance(decoded_cover, bytes):
        assert decoded_cover == content
    elif isinstance(decoded_cover, InputFile):
        assert decoded_cover.input_file_content == content
        assert decoded_cover.filename == expected_filename
    else:
        assert decoded_cover.tell() == 0
        assert decoded_cover.read() == content
        assert getattr(decoded_cover, "name", None) == expected_filename


def test_malformed_video_cover_commits_zero_rows(tmp_path):
    queue = OutboundQueue(tmp_path)

    with pytest.raises(QueueEnqueueError, match="Unable to serialize queued Telegram call"):
        queue.enqueue_many(
            [QueueRequest("send_video", (100, b"video"), {"cover": object()})],
            lambda _name: media_operation,
        )

    assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 0


@pytest.mark.parametrize("keyword", [False, True], ids=["positional", "keyword"])
@pytest.mark.parametrize(
    ("media_type", "content", "filename"),
    [
        (InputMediaPhoto, b"edited photo", "edited.jpg"),
        (InputMediaDocument, b"edited document", "edited.bin"),
        (InputMediaVideo, b"edited video", "edited.mp4"),
    ],
    ids=["photo", "document", "video"],
)
def test_edit_media_snapshots_nested_input_file_and_attach_name(
    tmp_path, keyword, media_type, content, filename
):
    queue = OutboundQueue(tmp_path)
    uploaded, source = _open_input_file(content, filename)
    media = media_type(uploaded, caption="caption")
    attach_name = uploaded.attach_name
    args = () if keyword else (media, 100, 7)
    kwargs = {"media": media, "chat_id": 100, "message_id": 7} if keyword else {}
    kwargs["_required_sender_bot_id"] = "__main__"

    queue.enqueue_many(
        [QueueRequest("edit_message_media", args, kwargs)],
        lambda _name: edit_media_operation,
    )

    assert source.tell() == 1
    source.close()
    decoded_args, decoded_kwargs = queue.decode_payload(queue.heads()[0].payload)
    decoded = decoded_kwargs["media"] if keyword else decoded_args[0]
    assert decoded.caption == "caption"
    assert decoded.media.attach_name == attach_name
    assert decoded.media.input_file_content == content


@pytest.mark.parametrize("keyword", [False, True], ids=["positional", "keyword"])
def test_media_group_snapshots_nested_files_thumbnails_and_attach_names(tmp_path, keyword):
    queue = OutboundQueue(tmp_path)
    photo, photo_source = _open_input_file(b"photo", "photo.jpg")
    video, video_source = _open_input_file(b"video", "video.mp4")
    thumbnail, thumbnail_source = _open_input_file(b"thumb", "thumb.jpg")
    media = [InputMediaPhoto(photo), InputMediaVideo(video, thumbnail=thumbnail)]
    attach_names = (photo.attach_name, video.attach_name, thumbnail.attach_name)
    args = () if keyword else (100, media)
    kwargs = {"disable_notification": True}
    if keyword:
        kwargs.update(chat_id=100, media=media)

    queue.enqueue_many(
        [QueueRequest("send_media_group", args, kwargs)],
        lambda _name: media_operation,
    )

    assert [stream.tell() for stream in (photo_source, video_source, thumbnail_source)] == [1, 1, 1]
    for stream in (photo_source, video_source, thumbnail_source):
        stream.close()
    decoded_args, decoded_kwargs = queue.decode_payload(queue.heads()[0].payload)
    decoded = decoded_kwargs["media"] if keyword else decoded_args[1]
    assert decoded_kwargs["disable_notification"] is True
    assert (decoded[0].media.attach_name, decoded[1].media.attach_name,
            decoded[1].thumbnail.attach_name) == attach_names
    assert (decoded[0].media.input_file_content, decoded[1].media.input_file_content,
            decoded[1].thumbnail.input_file_content) == (b"photo", b"video", b"thumb")


@pytest.mark.parametrize(
    ("media_type", "attachment_fields"),
    [
        (InputMediaAudio, ("media", "thumbnail")),
        (InputMediaDocument, ("media", "thumbnail")),
        (InputMediaPhoto, ("media",)),
        (InputMediaVideo, ("media", "thumbnail", "cover")),
        (InputMediaLivePhoto, ("media", "photo")),
    ],
    ids=["audio", "document", "photo", "video", "live-photo"],
)
def test_media_group_accepts_exact_supported_subtypes_and_normalizes_upload_fields(
    tmp_path, media_type, attachment_fields
):
    uploads = {}
    sources = []
    expected = {}
    for field in attachment_fields:
        content = f"{media_type.__name__} {field}".encode()
        filename = f"{media_type.__name__}-{field}.bin"
        upload, source = _open_input_file(content, filename)
        uploads[field] = upload
        sources.append(source)
        expected[field] = (content, filename, upload.attach_name)
    constructor_kwargs = {
        field: uploads[field] for field in attachment_fields if field != "media"
    }
    media = media_type(
        uploads["media"], caption="preserved caption", **constructor_kwargs
    )
    queue = OutboundQueue(tmp_path)

    queue.enqueue_many(
        [QueueRequest(
            "send_media_group",
            (100, [media]),
            {"disable_notification": True, "protect_content": True},
        )],
        lambda _name: media_operation,
    )

    assert [source.tell() for source in sources] == [1] * len(sources)
    assert all(not source.closed for source in sources)
    for source in sources:
        source.close()
    decoded_args, decoded_kwargs = queue.decode_payload(queue.heads()[0].payload)
    decoded = decoded_args[1][0]
    assert type(decoded) is media_type
    assert decoded.caption == "preserved caption"
    assert decoded_kwargs == {"disable_notification": True, "protect_content": True}
    for field, (content, filename, attach_name) in expected.items():
        delivered = getattr(decoded, field)
        assert delivered.input_file_content == content
        assert delivered.filename == filename
        assert delivered.attach_name == attach_name


def test_media_group_rejects_input_media_animation_before_insert(tmp_path):
    queue = OutboundQueue(tmp_path)
    animation = InputMediaAnimation(
        InputFile(b"animation", filename="animation.gif", attach=True)
    )

    with pytest.raises(QueueEnqueueError, match="Unable to serialize queued Telegram call"):
        queue.enqueue_many(
            [QueueRequest("send_media_group", (100, [animation]), {})],
            lambda _name: media_operation,
        )

    assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("kind", "expected_filename"),
    [
        ("explicit", "explicit.bin"),
        ("input-file", "input.bin"),
        ("local-basename", "local.bin"),
        ("none", None),
    ],
)
def test_nested_input_media_preserves_bytes_filename_precedence_and_attachment_link(
    tmp_path, kind, expected_filename
):
    content = f"nested {kind}".encode()
    expected_attach_name = None
    local_path = None
    if kind == "explicit":
        media = InputMediaDocument(
            io.BufferedReader(io.BytesIO(content)), filename=expected_filename
        )
        expected_attach_name = media.media.attach_name
    elif kind == "input-file":
        upload = InputFile(content, filename=expected_filename, attach=True)
        expected_attach_name = upload.attach_name
        media = InputMediaDocument(upload)
    elif kind == "local-basename":
        local_path = tmp_path / expected_filename
        local_path.write_bytes(content)
        media = InputMediaDocument(local_path)
    else:
        media = InputMediaDocument("placeholder")
        object.__setattr__(media, "media", io.BufferedReader(io.BytesIO(content)))
    queue = OutboundQueue(tmp_path)

    queue.enqueue_many(
        [QueueRequest("send_media_group", (100, [media]), {})],
        lambda _name: media_operation,
    )

    if local_path is not None:
        local_path.unlink()
    delivered = queue.decode_payload(queue.heads()[0].payload)[0][1][0].media
    if isinstance(delivered, InputFile):
        assert delivered.input_file_content == content
        assert delivered.filename == expected_filename
        assert delivered.attach_name == expected_attach_name
    else:
        assert delivered.tell() == 0
        assert delivered.read() == content
        assert getattr(delivered, "name", None) == expected_filename


def test_nested_local_media_read_failure_commits_zero_rows(tmp_path, monkeypatch):
    local_path = tmp_path / "nested-unreadable.bin"
    local_path.write_bytes(b"nested media")
    media = InputMediaDocument(local_path)
    queue = OutboundQueue(tmp_path)

    def failing_open(_path, *_args, **_kwargs):
        raise OSError("nested read failed")

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(QueueEnqueueError, match="Unable to serialize queued Telegram call"):
        queue.enqueue_many(
            [QueueRequest("send_media_group", (100, [media]), {})],
            lambda _name: media_operation,
        )

    assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 0


def test_inline_media_snapshot_reconstructs_after_queue_reopen(tmp_path):
    queue = OutboundQueue(tmp_path)
    source = io.BufferedReader(io.BytesIO(b"reopened media"))
    row_id, _waiter = queue.enqueue_many(
        [QueueRequest("send_document", (100, source), {})],
        lambda _name: media_operation,
    )
    persisted_payload = queue.heads()[0].payload
    source.close()
    queue.close()

    reopened = OutboundQueue(tmp_path)
    row = reopened.heads()[0]
    media = reopened.decode_payload(row.payload)[0][1]
    assert row.id == row_id
    assert row.payload == persisted_payload
    assert media.tell() == 0
    assert media.read() == b"reopened media"
    reopened.close()


@pytest.mark.parametrize("input_kind", ["string", "path"])
@pytest.mark.parametrize("after_enqueue", ["mutate", "move", "delete"])
def test_local_file_media_is_owned_inline_after_enqueue_and_reopen(
    tmp_path, input_kind, after_enqueue
):
    source_path = tmp_path / f"{input_kind}-{after_enqueue}.bin"
    original_content = f"original {input_kind} {after_enqueue}".encode()
    source_path.write_bytes(original_content)
    media = str(source_path) if input_kind == "string" else source_path
    queue = OutboundQueue(tmp_path)

    row_id, _waiter = queue.enqueue_many(
        [QueueRequest("send_document", (100, media), {})],
        lambda _name: media_operation,
    )

    if after_enqueue == "mutate":
        source_path.write_bytes(b"mutated")
        source_path.unlink()
    elif after_enqueue == "move":
        moved_path = tmp_path / "moved.bin"
        source_path.rename(moved_path)
        moved_path.unlink()
    else:
        source_path.unlink()
    queue.close()
    reopened = OutboundQueue(tmp_path)
    row = reopened.heads()[0]
    decoded_media = reopened.decode_payload(row.payload)[0][1]
    assert row.id == row_id
    assert decoded_media.tell() == 0
    assert decoded_media.read() == original_content
    assert decoded_media.name == source_path.name
    reopened.close()


@pytest.mark.parametrize(
    "remote_value",
    [
        "http://example.com/media.bin",
        "https://example.com/media.bin",
        "BQACAgQAAxkBAAIBQ2telegram-file-id",
    ],
    ids=["http-url", "https-url", "telegram-file-id"],
)
def test_remote_string_media_stays_opaque(remote_value, tmp_path):
    queue = OutboundQueue(tmp_path)

    queue.enqueue_many(
        [QueueRequest("send_document", (100, remote_value), {})],
        lambda _name: media_operation,
    )

    decoded_media = queue.decode_payload(queue.heads()[0].payload)[0][1]
    assert decoded_media == remote_value


@pytest.mark.parametrize("failure", ["open", "read"])
def test_local_file_media_failure_commits_zero_rows(tmp_path, monkeypatch, failure):
    source_path = tmp_path / "unreadable.bin"
    source_path.write_bytes(b"media")
    original_open = Path.open

    def failing_open(path, *args, **kwargs):
        if path == source_path:
            if failure == "open":
                raise OSError("open failed")
            return _UnreadableStream(b"media")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    queue = OutboundQueue(tmp_path)

    with pytest.raises(QueueEnqueueError, match="Unable to serialize queued Telegram call"):
        queue.enqueue_many(
            [QueueRequest("send_document", (100, source_path), {})],
            lambda _name: media_operation,
        )

    assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 0


@pytest.mark.parametrize(
    "queue_request",
    [
        QueueRequest("send_photo", (100, object()), {}),
        QueueRequest("send_document", (100, b"document"), {"thumbnail": object()}),
        QueueRequest("send_media_group", (100, [object()]), {}),
    ],
    ids=["direct", "thumbnail", "media-group"],
)
def test_unsupported_media_values_commit_zero_rows(tmp_path, queue_request):
    queue = OutboundQueue(tmp_path)

    with pytest.raises(QueueEnqueueError, match="Unable to serialize queued Telegram call"):
        queue.enqueue_many([queue_request], lambda _name: media_operation)

    assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 0


@pytest.mark.parametrize("operation_name", ["edit_message_media", "send_media_group"])
@pytest.mark.parametrize("field", ["media", "thumbnail"])
def test_unsupported_nested_media_values_commit_zero_rows(tmp_path, operation_name, field):
    queue = OutboundQueue(tmp_path)
    nested = (
        InputMediaPhoto(object())
        if field == "media"
        else InputMediaVideo("video-id", thumbnail=object())
    )
    if operation_name == "edit_message_media":
        request = QueueRequest(
            operation_name, (nested, 100, 7), {"_required_sender_bot_id": "__main__"}
        )
        resolved_operation = edit_media_operation
    else:
        request = QueueRequest(operation_name, (100, [InputMediaPhoto("photo-id"), nested]), {})
        resolved_operation = media_operation

    with pytest.raises(QueueEnqueueError, match="Unable to serialize queued Telegram call"):
        queue.enqueue_many([request], lambda _name: resolved_operation)

    assert queue.connection.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0] == 0


def test_nested_media_accepts_file_ids_urls_bytes_and_matching_telegram_objects(tmp_path):
    queue = OutboundQueue(tmp_path)
    photo_size = PhotoSize("photo-size-id", "unique-id", 10, 10)
    telegram_media = InputMediaPhoto("placeholder")
    object.__setattr__(telegram_media, "media", photo_size)
    media = [
        InputMediaPhoto("photo-id"),
        InputMediaPhoto("https://example.com/photo.jpg"),
        InputMediaPhoto(b"photo-bytes"),
        telegram_media,
    ]

    queue.enqueue_many(
        [QueueRequest("send_media_group", (100, media), {})],
        lambda _name: media_operation,
    )

    decoded = queue.decode_payload(queue.heads()[0].payload)[0][1]
    assert decoded[0].media == "photo-id"
    assert decoded[1].media == "https://example.com/photo.jpg"
    assert decoded[2].media.input_file_content == b"photo-bytes"
    assert decoded[3].media == photo_size


@pytest.mark.parametrize(
    ("args", "kwargs", "valid"),
    [
        ((7, "text"), {}, True),
        ((), {"chat_id": 7, "text": "text"}, True),
        ((), {"text": "text"}, False),
        ((7,), {"chat_id": 7, "text": "text"}, False),
        ((), {"chat_id": True, "text": "text"}, False),
        ((), {"chat_id": 7.0, "text": "text"}, False),
        ((), {"chat_id": "7", "text": "text"}, False),
    ],
)
def test_chat_id_binds_after_scheduler_metadata_is_removed(tmp_path, args, kwargs, valid):
    queue = OutboundQueue(tmp_path)
    request = QueueRequest("send_message", args, {**kwargs, "_send_mode": "eventual"})
    if valid:
        enqueue(queue, request)
        assert queue.heads()[0].telegram_chat_id == 7
    else:
        with pytest.raises(QueueEnqueueError):
            enqueue(queue, request)


def test_internal_keys_are_validated_and_absent_from_payload(tmp_path):
    queue = OutboundQueue(tmp_path)
    row_id, _waiter = enqueue(queue, QueueRequest("send_message", (), {
        "chat_id": 7, "text": "text", "_send_mode": "blocking", "_slave_id": "slave",
    }))
    row = queue.heads()[0]
    assert row.id == row_id
    assert row.priority == 1
    assert row.slave_id == "slave"
    assert queue.decode_payload(row.payload)[1] == {"chat_id": 7, "text": "text"}
    for kwargs in (
        {"chat_id": 7, "text": "x", "_send_mode": "later"},
        {"chat_id": 7, "text": "x", "_slave_id": ""},
        {"chat_id": 7, "text": "x", "_required_sender_bot_id": "bot"},
    ):
        with pytest.raises(QueueEnqueueError):
            enqueue(queue, QueueRequest("send_message", (), kwargs))
    with pytest.raises(QueueEnqueueError):
        enqueue(queue, QueueRequest("edit_message_text", (), {
            "chat_id": 7, "message_id": 1, "text": "x"
        }))


def test_send_message_main_bot_sentinel_persists_but_other_required_sender_is_rejected(tmp_path):
    queue = OutboundQueue(tmp_path)

    row_id, _waiter = enqueue(queue, QueueRequest("send_message", (), {
        "chat_id": 7,
        "text": "text",
        "_required_sender_bot_id": "__main__",
    }))

    row = queue.heads()[0]
    assert row.id == row_id
    assert row.required_sender_bot_id == "__main__"
    assert queue.decode_payload(row.payload)[1] == {"chat_id": 7, "text": "text"}
    with pytest.raises(QueueEnqueueError):
        enqueue(queue, QueueRequest("send_message", (), {
            "chat_id": 7,
            "text": "text",
            "_required_sender_bot_id": "auxiliary-1",
        }))


def test_multi_call_is_atomic_ordered_and_returns_only_first_waiter(tmp_path):
    queue = OutboundQueue(tmp_path)
    first, waiter = enqueue(queue,
        QueueRequest("send_message", (), {"chat_id": 7, "text": "one"}),
        QueueRequest("send_message", (), {"chat_id": 7, "text": "two"}),
    )
    assert [row.id for row in queue.heads()] == [first]
    assert list(queue.waiters) == [first]
    with pytest.raises(QueueEnqueueError):
        enqueue(queue,
            QueueRequest("send_message", (), {"chat_id": 7, "text": "one"}),
            QueueRequest("send_message", (), {"chat_id": 8, "text": "two"}),
        )
    assert len(queue.connection.execute("SELECT * FROM outbound_queue").fetchall()) == 2
    assert not waiter.done()


class CompletionDecision:
    def __init__(self, kind, retry_at=None):
        self.kind = kind
        self.retry_at = retry_at


class Adapter:
    def __init__(self, block=False, acquire=True):
        self.block = block
        self.acquire = acquire
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def select_sender(self, row, now):
        return SenderSelectionResult(selection=SenderSelection(object(), None))

    def acquire_sender_limits(self, selection, telegram_chat_id):
        return self.acquire

    def execute_queued_call(self, row, args, kwargs, selection):
        self.calls.append((row.id, row.telegram_chat_id, row.operation))
        self.started.set()
        if self.block:
            self.release.wait(2)
        return row.id

    def record_queued_failure(self, row, error, selection):
        raise AssertionError(f"unexpected failure: {error}")

    def record_queued_success(self, row, result, selection):
        return CompletionDecision("success")


def test_scheduler_prioritizes_blocking_and_never_submits_two_destination_rows(tmp_path):
    queue = OutboundQueue(tmp_path)
    normal_id, _normal = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "normal"}))
    blocking_id, _blocking = enqueue(queue, QueueRequest("send_message", (), {
        "chat_id": 7, "text": "blocking", "_send_mode": "blocking"
    }))
    adapter = Adapter(block=True)
    with ThreadPoolExecutor(max_workers=2) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=2)
        scheduler.dispatch_once()
        assert adapter.started.wait(1)
        assert adapter.calls == [(blocking_id, 7, "send_message")]
        scheduler.dispatch_once()
        assert adapter.calls == [(blocking_id, 7, "send_message")]
        adapter.release.set()
        scheduler.in_flight[blocking_id].future.result(timeout=1)
        scheduler.harvest_completed()
        scheduler.dispatch_once()
        scheduler.in_flight[normal_id].future.result(timeout=1)
        assert adapter.calls[-1][0] == normal_id


def test_delete_failure_stops_scheduler_and_fails_waiters(tmp_path, monkeypatch):
    queue = OutboundQueue(tmp_path)
    _row_id, waiter = enqueue(queue, QueueRequest("send_message", (), {
        "chat_id": 7, "text": "text", "_send_mode": "blocking"
    }))
    adapter = Adapter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        monkeypatch.setattr(queue, "delete", lambda _row_id: (_ for _ in ()).throw(sqlite3.OperationalError()))
        scheduler.dispatch_once()
        assert scheduler.stopping
        with pytest.raises(Exception, match="deletion failed"):
            waiter.result()
        assert adapter.calls == []


def test_failed_limit_acquisition_keeps_row_and_schedules_non_busy_wake(tmp_path, monkeypatch):
    queue = OutboundQueue(tmp_path)
    row_id, waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 8, "text": "text"}))
    adapter = Adapter(acquire=False)
    scheduler = OutboundQueueScheduler(queue, adapter, ThreadPoolExecutor(max_workers=1), worker_count=1)
    monkeypatch.setattr("efb_telegram_master.outbound.time.monotonic", lambda: 10.0)

    scheduler.dispatch_once()

    assert [row.id for row in queue.heads()] == [row_id]
    assert not waiter.done()
    assert scheduler.next_deadline == 10.25
    assert adapter.calls == []


def test_shutdown_keeps_queued_row_and_fails_abandoned_waiter(tmp_path):
    queue = OutboundQueue(tmp_path)
    row_id, waiter = enqueue(queue, QueueRequest("send_message", (), {"chat_id": 7, "text": "text"}))
    adapter = Adapter(block=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        scheduler = OutboundQueueScheduler(queue, adapter, executor, worker_count=1)
        scheduler.dispatch_once()
        assert adapter.started.wait(1)
        scheduler.stop_and_drain(timeout=0)
        with pytest.raises(SchedulerStoppedError):
            waiter.result()
        assert row_id not in queue.waiters
        adapter.release.set()
