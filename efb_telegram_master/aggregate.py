"""Saved source members and presentation of live text containers."""

import datetime
import html
import pickle
from dataclasses import dataclass
from typing import List, Optional, Tuple, TypedDict

from ehforwarderbot.chat import Chat, SelfChatMember
from telegram.constants import MessageLimit

from . import queued_log
from .message import ETMMsg
from .utils import chat_id_to_str


class SourceReply(TypedDict):
    origin_uid: str
    source_id: str
    author_name: str
    excerpt: str


class SourceMember(TypedDict):
    origin_uid: str
    source_id: str
    author_uid: str
    author_name: str
    display_prefix: Optional[str]
    snapshot: bytes
    self_mentions: List[Tuple[int, int]]
    source_time: Optional[datetime.datetime]
    received_time: datetime.datetime
    reply: Optional[SourceReply]
    source_revision: int
    status: str
    replacement_master_msg_id: Optional[str]


class MemberRange(TypedDict):
    origin_uid: str
    source_id: str
    start: int
    end: int


class AggregatePayload(TypedDict):
    format_version: int
    revision: int
    children: List[SourceMember]
    confirmed_text: str
    confirmed_ranges: List[MemberRange]


@dataclass(frozen=True)
class RenderedAggregate:
    html: str
    text: str
    ranges: List[MemberRange]


def member_identity(member: SourceMember) -> Tuple[str, str]:
    return member["origin_uid"], member["source_id"]


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def make_source_member(message: ETMMsg, *, received_time: Optional[datetime.datetime] = None,
                       source_time: Optional[datetime.datetime] = None,
                       source_revision: int = 1, display_prefix: Optional[str] = None) -> SourceMember:
    """Capture full source content without media handles or recursive replies."""
    if not message.uid:
        raise ValueError("A source member requires a source message ID.")
    reply = None
    if message.target:
        reply = SourceReply(
            origin_uid=str(chat_id_to_str(chat=message.target.chat)),
            source_id=str(message.target.uid),
            author_name=getattr(message.target.author, "long_name", "") if message.target.author else "",
            excerpt=" ".join((message.target.text or "").split())[:120],
        )
    return SourceMember(
        origin_uid=str(chat_id_to_str(chat=message.chat)), source_id=str(message.uid),
        author_uid=str(chat_id_to_str(chat=message.author)), author_name=message.author.long_name,
        snapshot=queued_log.encode(message, None), source_time=source_time, display_prefix=display_prefix,
        self_mentions=[key for key, chat in (message.substitutions or {}).items()
                       if isinstance(chat, SelfChatMember) or (isinstance(chat, Chat) and chat.has_self)],
        received_time=received_time or datetime.datetime.now(), reply=reply,
        source_revision=source_revision, status="active", replacement_master_msg_id=None,
    )


def member_message(member: SourceMember) -> ETMMsg:
    return queued_log.decode(member["snapshot"])[0]


def _body_html(message: ETMMsg, admin_id: Optional[int], self_mentions: List[Tuple[int, int]]) -> str:
    text = message.text or ""
    previous = 0
    parts = []
    for (start, end), chat in sorted((message.substitutions or {}).items()):
        if start < previous or end < start or end > len(text):
            continue
        parts.append(html.escape(text[previous:start]))
        mention = html.escape(text[start:end])
        if admin_id is not None and (start, end) in self_mentions:
            parts.append(f'<a href="tg://user?id={admin_id}">{mention}</a>')
        else:
            parts.append(f"<code>{mention}</code>")
        previous = end
    parts.append(html.escape(text[previous:]))
    return "".join(parts)


def render_members(members: List[SourceMember], *, admin_id: Optional[int] = None,
                   history: bool = False, _compact_status: bool = False) -> RenderedAggregate:
    """Render every real source separately; ranges refer to parsed UTF-16 text."""
    html_parts: List[str] = []
    text_parts: List[str] = []
    ranges: List[MemberRange] = []
    offset = 0
    for member in members:
        if history and member["status"] == "redirected":
            continue
        name = member.get("display_prefix")
        if name is None:
            name = member["author_name"]
        # Status displays remain bounded even after many source edits.
        if not history and member["status"] != "active":
            name = name[:80]
            body = "[message removed]" if member["status"] == "removed" else "[message moved]"
            if _compact_status:
                name = ""
                body = "[removed]" if member["status"] == "removed" else "[moved]"
            body_html = html.escape(body)
            reply_text = ""
        else:
            message = member_message(member)
            body = message.text or ""
            body_html = _body_html(message, admin_id, member["self_mentions"])
            reply = member["reply"]
            reply_text = ""
            if reply:
                identity = f'{reply["origin_uid"]}/{reply["source_id"]}'
                reply_text = f'↪ {reply["author_name"][:60]} [{identity[:100]}]: {reply["excerpt"][:120]}\n'
        prefix = f"{name}:\n" if name else ""
        visible = prefix + reply_text + body
        if text_parts:
            offset += 2
        ranges.append(MemberRange(origin_uid=member["origin_uid"], source_id=member["source_id"],
                                  start=offset, end=offset + utf16_length(visible)))
        offset += utf16_length(visible)
        text_parts.append(visible)
        html_parts.append(html.escape(prefix + reply_text) + body_html)
    if not text_parts:
        return RenderedAggregate("[message removed]", "[message removed]", [])
    if (not history and not _compact_status and
            len("\n\n".join(text_parts)) > int(MessageLimit.MAX_TEXT_LENGTH) and
            any(member["status"] != "active" for member in members)):
        return render_members(members, admin_id=admin_id, _compact_status=True)
    return RenderedAggregate("\n\n".join(html_parts), "\n\n".join(text_parts), ranges)


def make_aggregate(members: List[SourceMember], revision: int, *,
                   admin_id: Optional[int] = None) -> AggregatePayload:
    rendered = render_members(members, admin_id=admin_id)
    return AggregatePayload(format_version=1, revision=revision, children=members,
                            confirmed_text=rendered.text, confirmed_ranges=rendered.ranges)


def aggregate_fits(members: List[SourceMember], *, max_members: int = 200,
                   max_payload_bytes: int = 256 * 1024, admin_id: Optional[int] = None,
                   include_redirected_payload: bool = True) -> bool:
    """Enforce display/member limits and saved payload capacity.

    Redirected members retain their full snapshots after replacement confirmation;
    fixed-container updates can omit them from payload capacity.
    """
    payload_members = members if include_redirected_payload else [
        member for member in members if member["status"] != "redirected"]
    return (len(members) <= max_members and
            len(render_members(members, admin_id=admin_id).text) <= int(MessageLimit.MAX_TEXT_LENGTH) and
            len(pickle.dumps(payload_members, protocol=5)) <= max_payload_bytes)
