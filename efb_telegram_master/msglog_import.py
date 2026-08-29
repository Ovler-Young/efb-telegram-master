"""Import a bounded Telegram recovery artifact into ETM's message log."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ehforwarderbot import coordinator
from ehforwarderbot.constants import MsgType
from ehforwarderbot.types import ModuleID
from ruamel.yaml import YAML

from .db import (
    SYNTHETIC_MSGLOG_PREFIX,
    DatabaseManager,
    MsgLog,
    TopicAssoc,
    msglog_write_transaction,
)
from .msg_type import TGMsgType
from .paths import get_config_path

_CHANNEL_MARK = 1_000_000_000_000
_DECIMAL = re.compile(r"^-?\d+$")
_POSITIVE_DECIMAL = re.compile(r"^[1-9]\d*$")
_EXPORT_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_MANIFEST_KEYS = {"type", "version", "owner", "window", "chats"}
_OWNER_KEYS = {"profile", "telegramUserId", "username", "name"}
_WINDOW_KEYS = {"from", "to", "semantics"}
_CHAT_KEYS = {"topicChatId", "sourceChatId", "title", "type"}
_MESSAGE_KEYS = {
    "type",
    "version",
    "topicChatId",
    "sourceChatId",
    "messageId",
    "senderId",
    "timestamp",
    "text",
    "replyToId",
    "replyToTopId",
    "media",
}
_MEDIA_KEYS = {"type", "mimeType"}
_MEDIA_TYPES = {"photo", "sticker", "document", "webpage", "unknown"}


class ImportValidationError(ValueError):
    """Raised when an input cannot be trusted as a version-1 recovery artifact."""


@dataclass(frozen=True)
class ChatMapping:
    topic_chat_id: str
    source_chat_id: str
    title: str
    chat_type: str


@dataclass(frozen=True)
class RecoveryMessage:
    topic_chat_id: str
    source_chat_id: str
    message_id: str
    sender_id: str
    timestamp: int
    text: str
    reply_to_id: str | None
    reply_to_top_id: str | None
    media_type: str | None
    mime_type: str | None

    @property
    def master_message_id(self) -> str:
        return f"{self.topic_chat_id}.{self.message_id}"

    @property
    def topic_root_id(self) -> str | None:
        return self.reply_to_top_id or self.reply_to_id


@dataclass(frozen=True)
class ValidatedArtifact:
    profile: str
    window_from: datetime
    window_to: datetime
    chats: tuple[ChatMapping, ...]
    selected_chat_ids: tuple[str, ...]
    messages: tuple[RecoveryMessage, ...]


@dataclass
class ImportSummary:
    artifact_messages: int = 0
    selected_messages: int = 0
    imported: int = 0
    existing: int = 0
    skipped_sender: int = 0
    skipped_unknown_chat: int = 0
    skipped_unbound_topic: int = 0
    unknown_chat_ids: list[str] = field(default_factory=list)
    unbound_topic_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _PreparedRow:
    master_message_id: str
    values: dict[str, Any]


@dataclass
class _ImportChannel:
    channel_id: ModuleID
    config: dict[str, Any]


def _canonical_decimal(value: Any, description: str, *, positive: bool = False) -> str:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ImportValidationError(f"{description} must be a decimal string")
    canonical = str(int(value))
    if canonical == "0" or (positive and int(canonical) < 0):
        qualifier = "positive " if positive else "non-zero "
        raise ImportValidationError(
            f"{description} must be a {qualifier}decimal string"
        )
    if value != canonical:
        raise ImportValidationError(f"{description} is not canonical: {value}")
    return canonical


def _topic_source_id(topic_chat_id: str) -> str:
    value = int(topic_chat_id)
    if value <= -(_CHANNEL_MARK + 1):
        return str(-value - _CHANNEL_MARK)
    if value < 0:
        return str(-value)
    return str(value)


def parse_chat_file(content: str) -> tuple[str, ...]:
    """Parse the same signed-decimal chat file accepted by the exporter."""
    chat_ids: dict[str, None] = {}
    source_owners: dict[str, str] = {}
    for line_number, source_line in enumerate(content.splitlines(), start=1):
        value = source_line.partition("#")[0].strip()
        if not value:
            continue
        if not _DECIMAL.fullmatch(value):
            raise ImportValidationError(
                f"Invalid Telegram chat ID on line {line_number}: {value}"
            )
        canonical = str(int(value))
        if canonical == "0":
            raise ImportValidationError(
                f"Telegram chat ID on line {line_number} must not be zero"
            )
        source_id = _topic_source_id(canonical)
        owner = source_owners.get(source_id)
        if owner is not None and owner != canonical:
            raise ImportValidationError(
                f"Chat IDs {owner} and {canonical} resolve to the same source chat {source_id}"
            )
        source_owners[source_id] = canonical
        chat_ids.setdefault(canonical, None)
    if not chat_ids:
        raise ImportValidationError("Chat file does not contain any Telegram chat IDs")
    return tuple(chat_ids)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ImportValidationError(f"JSON object repeats key {key!r}")
        value[key] = item
    return value


def _load_json_line(line: str, line_number: int) -> dict[str, Any]:
    try:
        value = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ImportValidationError) as error:
        raise ImportValidationError(
            f"Invalid JSON on artifact line {line_number}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ImportValidationError(
            f"Artifact line {line_number} must contain a JSON object"
        )
    return value


def _require_keys(
    value: Mapping[str, Any], expected: set[str], description: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ImportValidationError(
            f"{description} has an invalid schema (missing={missing}, extra={extra})"
        )


def _parse_export_datetime(value: Any, description: str) -> datetime:
    if not isinstance(value, str) or not _EXPORT_TIMESTAMP.fullmatch(value):
        raise ImportValidationError(
            f"{description} must be an ISO 8601 UTC timestamp with millisecond precision"
        )
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ImportValidationError(
            f"{description} is not a valid timestamp: {value}"
        ) from error


def _parse_manifest(
    value: dict[str, Any], profile: str, selected_chat_ids: Sequence[str]
) -> tuple[datetime, datetime, tuple[ChatMapping, ...]]:
    _require_keys(value, _MANIFEST_KEYS, "Artifact manifest")
    if (
        value["type"] != "manifest"
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ImportValidationError(
            "Artifact manifest must have type 'manifest' and version 1"
        )

    owner = value["owner"]
    if not isinstance(owner, dict):
        raise ImportValidationError("Artifact owner must be an object")
    _require_keys(owner, _OWNER_KEYS, "Artifact owner")
    if owner["profile"] != profile:
        raise ImportValidationError(
            f"Artifact profile {owner['profile']!r} does not match ETM profile {profile!r}"
        )
    _canonical_decimal(
        owner["telegramUserId"], "Artifact owner Telegram user ID", positive=True
    )
    if (
        not isinstance(owner["profile"], str)
        or not isinstance(owner["name"], str)
        or (
            owner["username"] is not None
            and not isinstance(owner["username"], str)
        )
    ):
        raise ImportValidationError(
            "Artifact owner profile and name must be strings; username must be a string or null"
        )

    window = value["window"]
    if not isinstance(window, dict):
        raise ImportValidationError("Artifact window must be an object")
    _require_keys(window, _WINDOW_KEYS, "Artifact window")
    if window["semantics"] != "[from,to)":
        raise ImportValidationError("Artifact window semantics must be '[from,to)'")
    window_from = _parse_export_datetime(window["from"], "Artifact window from")
    window_to = _parse_export_datetime(window["to"], "Artifact window to")
    if window_from >= window_to:
        raise ImportValidationError("Artifact window from must be earlier than to")

    raw_chats = value["chats"]
    if not isinstance(raw_chats, list) or not raw_chats:
        raise ImportValidationError("Artifact chats must be a non-empty array")
    chats: list[ChatMapping] = []
    topic_ids: set[str] = set()
    source_owners: dict[str, str] = {}
    for index, raw_chat in enumerate(raw_chats):
        if not isinstance(raw_chat, dict):
            raise ImportValidationError(f"Artifact chat {index} must be an object")
        _require_keys(raw_chat, _CHAT_KEYS, f"Artifact chat {index}")
        topic_id = _canonical_decimal(
            raw_chat["topicChatId"], f"Artifact chat {index} topic ID"
        )
        source_id = _canonical_decimal(
            raw_chat["sourceChatId"], f"Artifact chat {index} source ID", positive=True
        )
        if source_id != _topic_source_id(topic_id):
            raise ImportValidationError(
                f"Artifact chat {topic_id} does not map to source chat {source_id}"
            )
        if topic_id in topic_ids:
            raise ImportValidationError(f"Artifact repeats chat mapping {topic_id}")
        source_owner = source_owners.get(source_id)
        if source_owner is not None and source_owner != topic_id:
            raise ImportValidationError(
                f"Artifact chat IDs {source_owner} and {topic_id} map to source chat {source_id}"
            )
        title = raw_chat["title"]
        chat_type = raw_chat["type"]
        if (
            not isinstance(title, str)
            or not isinstance(chat_type, str)
            or chat_type not in {"supergroup", "group"}
        ):
            raise ImportValidationError(
                f"Artifact chat {topic_id} has invalid title or type"
            )
        if int(topic_id) <= -(_CHANNEL_MARK + 1) and chat_type != "supergroup":
            raise ImportValidationError(
                f"Artifact chat {topic_id} must be a supergroup"
            )
        if -(_CHANNEL_MARK + 1) < int(topic_id) < 0 and chat_type != "group":
            raise ImportValidationError(
                f"Artifact chat {topic_id} must be a basic group"
            )
        topic_ids.add(topic_id)
        source_owners[source_id] = topic_id
        chats.append(ChatMapping(topic_id, source_id, title, chat_type))

    missing_selected = [
        chat_id for chat_id in selected_chat_ids if chat_id not in topic_ids
    ]
    if missing_selected:
        raise ImportValidationError(
            f"Selected chat IDs are absent from artifact mappings: {', '.join(missing_selected)}"
        )
    return window_from, window_to, tuple(chats)


def _nullable_message_id(value: Any, description: str) -> str | None:
    if value is None:
        return None
    return _canonical_decimal(value, description, positive=True)


def _parse_message(
    value: dict[str, Any],
    line_number: int,
    mappings: Mapping[str, ChatMapping],
    window_from: datetime,
    window_to: datetime,
) -> RecoveryMessage:
    _require_keys(value, _MESSAGE_KEYS, f"Artifact message on line {line_number}")
    if (
        value["type"] != "message"
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ImportValidationError(
            f"Artifact line {line_number} must have type 'message' and version 1"
        )
    topic_id = _canonical_decimal(value["topicChatId"], f"Line {line_number} topic ID")
    source_id = _canonical_decimal(
        value["sourceChatId"], f"Line {line_number} source ID", positive=True
    )
    mapping = mappings.get(topic_id)
    if mapping is None or mapping.source_chat_id != source_id:
        raise ImportValidationError(
            f"Artifact line {line_number} does not match a manifest chat mapping"
        )
    message_id = _canonical_decimal(
        value["messageId"], f"Line {line_number} message ID", positive=True
    )
    sender_id = _canonical_decimal(
        value["senderId"], f"Line {line_number} sender ID", positive=True
    )
    timestamp = value["timestamp"]
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise ImportValidationError(
            f"Line {line_number} timestamp must be integer seconds"
        )
    try:
        timestamp_value = datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ImportValidationError(
            f"Line {line_number} timestamp is outside the supported range"
        ) from error
    if timestamp_value < window_from or timestamp_value >= window_to:
        raise ImportValidationError(
            f"Artifact line {line_number} timestamp lies outside the declared [from,to) window"
        )
    text = value["text"]
    if not isinstance(text, str):
        raise ImportValidationError(f"Line {line_number} text must be a string")
    reply_to_id = _nullable_message_id(
        value["replyToId"], f"Line {line_number} replyToId"
    )
    reply_to_top_id = _nullable_message_id(
        value["replyToTopId"], f"Line {line_number} replyToTopId"
    )

    media = value["media"]
    media_type: str | None = None
    mime_type: str | None = None
    if media is not None:
        if not isinstance(media, dict):
            raise ImportValidationError(
                f"Line {line_number} media must be an object or null"
            )
        _require_keys(media, _MEDIA_KEYS, f"Artifact media on line {line_number}")
        if not isinstance(media["type"], str) or media["type"] not in _MEDIA_TYPES:
            raise ImportValidationError(f"Line {line_number} has an unknown media type")
        if media["mimeType"] is not None and not isinstance(media["mimeType"], str):
            raise ImportValidationError(
                f"Line {line_number} media MIME type must be a string or null"
            )
        media_type = media["type"]
        mime_type = media["mimeType"]

    return RecoveryMessage(
        topic_id,
        source_id,
        message_id,
        sender_id,
        timestamp,
        text,
        reply_to_id,
        reply_to_top_id,
        media_type,
        mime_type,
    )


def validate_artifact(
    artifact_path: Path, profile: str, selected_chat_ids: Sequence[str]
) -> ValidatedArtifact:
    """Load and validate a complete recovery artifact without touching the database."""
    try:
        content = artifact_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ImportValidationError(
            f"Unable to read recovery artifact {artifact_path}: {error}"
        ) from error
    lines = content.splitlines()
    if not lines:
        raise ImportValidationError("Recovery artifact is empty")
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ImportValidationError(
                f"Recovery artifact contains a blank line at {line_number}"
            )

    manifest = _load_json_line(lines[0], 1)
    window_from, window_to, chats = _parse_manifest(
        manifest, profile, selected_chat_ids
    )
    mappings = {chat.topic_chat_id: chat for chat in chats}
    chat_positions = {chat.topic_chat_id: index for index, chat in enumerate(chats)}
    messages: list[RecoveryMessage] = []
    previous_order: tuple[int, int, int] | None = None
    seen_message_ids: set[tuple[str, str]] = set()
    for line_number, line in enumerate(lines[1:], start=2):
        message = _parse_message(
            _load_json_line(line, line_number),
            line_number,
            mappings,
            window_from,
            window_to,
        )
        identity = (message.topic_chat_id, message.message_id)
        if identity in seen_message_ids:
            raise ImportValidationError(
                f"Artifact repeats message {message.topic_chat_id}.{message.message_id}"
            )
        order = (
            chat_positions[message.topic_chat_id],
            message.timestamp,
            int(message.message_id),
        )
        if previous_order is not None and order <= previous_order:
            raise ImportValidationError(
                f"Artifact message on line {line_number} is out of deterministic order"
            )
        seen_message_ids.add(identity)
        previous_order = order
        messages.append(message)
    return ValidatedArtifact(
        profile,
        window_from,
        window_to,
        chats,
        tuple(selected_chat_ids),
        tuple(messages),
    )


def _bot_id(token: Any, description: str) -> str:
    if not isinstance(token, str):
        raise ImportValidationError(f"{description} token must be a string")
    bot_id, separator, secret = token.partition(":")
    if not separator or not secret or not _POSITIVE_DECIMAL.fullmatch(bot_id):
        raise ImportValidationError(
            f"{description} token does not contain a valid bot user ID"
        )
    return str(int(bot_id))


def sender_bot_ids(config: Mapping[str, Any]) -> dict[str, str | None]:
    """Return sender IDs authorized by the configured main and auxiliary tokens."""
    senders: dict[str, str | None] = {}
    main_id = _bot_id(config.get("token"), "Main bot")
    senders[main_id] = None
    auxiliaries = config.get("auxiliary_bots", []) or []
    if not isinstance(auxiliaries, list):
        raise ImportValidationError("auxiliary_bots must be a list")
    for index, item in enumerate(auxiliaries):
        if not isinstance(item, dict):
            raise ImportValidationError(f"auxiliary_bots[{index}] must be an object")
        auxiliary_id = _bot_id(item.get("token"), f"Auxiliary bot {index}")
        if auxiliary_id in senders:
            raise ImportValidationError(
                f"Bot user ID {auxiliary_id} is configured more than once"
            )
        senders[auxiliary_id] = auxiliary_id
    return senders


def _slave_destination(slave_uid: str) -> str:
    fields = slave_uid.split(" ", 2)
    if len(fields) < 2 or not fields[0] or not fields[1]:
        raise ImportValidationError(
            f"Topic association has invalid slave UID {slave_uid!r}"
        )
    return fields[0]


def _media_fields(message: RecoveryMessage) -> tuple[str, str, str | None]:
    if message.media_type == "photo":
        return TGMsgType.Photo.value, MsgType.Image.name, message.mime_type
    if message.media_type == "sticker":
        return TGMsgType.Sticker.value, MsgType.Sticker.name, message.mime_type
    if message.media_type == "document":
        return TGMsgType.Document.value, MsgType.File.name, message.mime_type
    return TGMsgType.Text.value, MsgType.Text.name, None


def _topic_associations(
    selected_chat_ids: Sequence[str],
) -> tuple[dict[tuple[str, str], str], set[str]]:
    associations: dict[tuple[str, str], str] = {}
    known_chats: set[str] = set()
    query = TopicAssoc.select().where(TopicAssoc.topic_chat_id.in_(selected_chat_ids))
    for association in query:
        chat_id = str(association.topic_chat_id)
        thread_id = str(association.message_thread_id)
        key = (chat_id, thread_id)
        slave_uid = str(association.slave_uid)
        existing = associations.get(key)
        if existing is not None and existing != slave_uid:
            raise ImportValidationError(
                f"Conflicting TopicAssoc rows for chat {chat_id}, topic {thread_id}: "
                f"{existing!r} and {slave_uid!r}"
            )
        _slave_destination(slave_uid)
        associations[key] = slave_uid
        known_chats.add(chat_id)
    return associations, known_chats


def _chunks(
    values: Sequence[_PreparedRow], size: int
) -> Iterable[Sequence[_PreparedRow]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def import_validated_artifact(
    artifact: ValidatedArtifact,
    config: Mapping[str, Any],
    *,
    chunk_size: int = 250,
) -> ImportSummary:
    """Import a validated artifact using the database currently bound to ETM models."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    authorized_senders = sender_bot_ids(config)
    associations, known_chats = _topic_associations(artifact.selected_chat_ids)
    selected = set(artifact.selected_chat_ids)
    summary = ImportSummary(artifact_messages=len(artifact.messages))
    prepared: list[_PreparedRow] = []

    for message in artifact.messages:
        if message.topic_chat_id not in selected:
            continue
        summary.selected_messages += 1
        if message.sender_id not in authorized_senders:
            summary.skipped_sender += 1
            continue
        root_id = message.topic_root_id
        if message.topic_chat_id not in known_chats:
            summary.skipped_unknown_chat += 1
            if message.topic_chat_id not in summary.unknown_chat_ids:
                summary.unknown_chat_ids.append(message.topic_chat_id)
            continue
        if root_id is None or (message.topic_chat_id, root_id) not in associations:
            summary.skipped_unbound_topic += 1
            topic_identity = f"{message.topic_chat_id}.{root_id or '<missing>'}"
            if topic_identity not in summary.unbound_topic_ids:
                summary.unbound_topic_ids.append(topic_identity)
            continue
        slave_uid = associations[(message.topic_chat_id, root_id)]
        media_type, msg_type, mime = _media_fields(message)
        master_message_id = message.master_message_id
        prepared.append(
            _PreparedRow(
                master_message_id,
                {
                    "master_msg_id": master_message_id,
                    "master_msg_id_alt": None,
                    "slave_message_id": f"{SYNTHETIC_MSGLOG_PREFIX}{master_message_id}",
                    "text": message.text,
                    "slave_origin_uid": slave_uid,
                    "slave_origin_display_name": None,
                    "slave_member_uid": slave_uid,
                    "slave_member_display_name": None,
                    "media_type": media_type,
                    "mime": mime,
                    "file_id": None,
                    "file_unique_id": None,
                    "msg_type": msg_type,
                    "pickle": None,
                    "sent_to": _slave_destination(slave_uid),
                    "sender_bot_id": authorized_senders[message.sender_id],
                    "time": datetime.fromtimestamp(message.timestamp, timezone.utc),
                },
            )
        )

    for chunk in _chunks(prepared, chunk_size):
        candidate_ids = [row.master_message_id for row in chunk]
        with msglog_write_transaction():
            existing_rows = MsgLog.select(
                MsgLog.master_msg_id, MsgLog.master_msg_id_alt
            ).where(
                (MsgLog.master_msg_id.in_(candidate_ids))
                | (MsgLog.master_msg_id_alt.in_(candidate_ids))
            )
            existing_ids: set[str] = set()
            for existing in existing_rows:
                if existing.master_msg_id in candidate_ids:
                    existing_ids.add(existing.master_msg_id)
                if existing.master_msg_id_alt in candidate_ids:
                    existing_ids.add(existing.master_msg_id_alt)
            pending = [
                row for row in chunk if row.master_message_id not in existing_ids
            ]
            summary.existing += len(chunk) - len(pending)
            if not pending:
                continue
            MsgLog.insert_many(
                [row.values for row in pending]
            ).on_conflict_ignore().execute()
            inserted_ids = {
                row.master_msg_id
                for row in MsgLog.select(MsgLog.master_msg_id).where(
                    MsgLog.master_msg_id.in_([row.master_message_id for row in pending])
                )
            }
            summary.imported += len(inserted_ids)
            summary.existing += len(pending) - len(inserted_ids)
    return summary


def load_profile_config(profile: str) -> dict[str, Any]:
    coordinator.profile = profile
    config_path = get_config_path(ModuleID("blueset.telegram"))
    if not config_path.exists():
        raise ImportValidationError(f"ETM profile config does not exist: {config_path}")
    with config_path.open(encoding="utf-8") as config_file:
        value = YAML(typ="safe").load(config_file)
    if not isinstance(value, dict):
        raise ImportValidationError(
            f"ETM profile config must contain a mapping: {config_path}"
        )
    return value


def import_recovery_artifact(
    artifact_path: Path,
    chat_file_path: Path,
    profile: str,
    *,
    chunk_size: int = 250,
) -> ImportSummary:
    """Run the profile-aware import used by the command-line entry point."""
    try:
        chat_file = chat_file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ImportValidationError(
            f"Unable to read chat file {chat_file_path}: {error}"
        ) from error
    selected_chat_ids = parse_chat_file(chat_file)
    artifact = validate_artifact(artifact_path, profile, selected_chat_ids)
    config = load_profile_config(profile)
    sender_bot_ids(config)

    manager: DatabaseManager | None = None
    try:
        manager = DatabaseManager(
            _ImportChannel(channel_id=ModuleID("blueset.telegram"), config=config)
        )
        return import_validated_artifact(artifact, config, chunk_size=chunk_size)
    finally:
        if manager is not None:
            manager.stop_worker()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="etm-msglog-import",
        description="Import a validated tg-search recovery JSONL artifact into ETM MsgLog.",
    )
    parser.add_argument(
        "--artifact", type=Path, required=True, help="version-1 recovery JSONL file"
    )
    parser.add_argument(
        "--chat-file",
        type=Path,
        required=True,
        help="mounted numeric Telegram chat-ID file",
    )
    parser.add_argument(
        "--profile", default="default", help="EFB profile name (default: default)"
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=250,
        help="rows per transaction (default: 250)",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(arguments)
    try:
        summary = import_recovery_artifact(
            args.artifact,
            args.chat_file,
            args.profile,
            chunk_size=args.chunk_size,
        )
    except (ImportValidationError, OSError, ValueError) as error:
        build_argument_parser().error(str(error))
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
