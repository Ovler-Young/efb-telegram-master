"""Saved source members and presentation of live text containers."""

import copy
import datetime
import html
import pickle
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo
from typing import List, NamedTuple, Optional, Tuple, TypedDict

from ehforwarderbot.chat import Chat, SelfChatMember
from telegram.constants import MessageLimit

from . import queued_log
from .custom_emoji import AuthorEmojiHeader, UserEmoji
from .message import ETMMsg
from .utils import (
    DEFAULT_TIMEZONE, EFBChannelChatIDStr, chat_id_str_to_id, chat_id_to_str, format_message_time, removed_text,
)


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


class MemberStatus:
    """Persisted spellings of ``SourceMember["status"]``; never change them."""
    ACTIVE = "active"
    REMOVED = "removed"
    REDIRECTED = "redirected"


class MemberEvent:
    """What can happen to one source member.

    ``NEW_REVISION``    a source version was created or edited (a removed member becomes active again).
    ``REMOVE``          the source was removed; the member keeps its saved content.
    ``REDIRECT``        a replacement output was confirmed for a member inside a container.
    ``RECEIPT``         a confirmed container presentation is saved over an earlier saved copy.
    ``SOURCE_RECEIPT``  a confirmed independent output is saved over an earlier saved copy.
    ``STALE_RECEIPT``   reported by ``MemberChange.event`` when a receipt was older than what is saved.
    """
    NEW_REVISION = "new_revision"
    REMOVE = "remove"
    REDIRECT = "redirect"
    RECEIPT = "receipt"
    SOURCE_RECEIPT = "source_receipt"
    STALE_RECEIPT = "stale_receipt"


class MemberTransitionError(ValueError):
    """A member event that the revision order does not allow."""


class MemberChange(NamedTuple):
    """Outcome of one event: the member to keep, what was applied, and whether it differs from before."""
    member: SourceMember
    event: str
    changed: bool


def apply_member_event(previous: SourceMember | None, event: str,
                       incoming: SourceMember | None = None) -> MemberChange:
    """Apply one event to a saved member and return the member to keep.

    ``incoming`` is the newer version for ``NEW_REVISION``, ``REDIRECT`` and both receipts.
    ``REMOVE`` needs only ``previous``. A receipt older than ``previous`` is reported as
    ``STALE_RECEIPT`` with ``changed`` False and ``previous`` returned; ``REDIRECT`` of a newer
    source version and ``NEW_REVISION`` that does not advance the revision raise
    ``MemberTransitionError``. The input members are never modified.
    """
    before = previous["status"] if previous is not None else None
    known = previous if previous is not None else incoming
    identity = member_identity(known) if known is not None else None
    if event == MemberEvent.REMOVE:
        assert previous is not None
        if before == MemberStatus.REMOVED:
            return MemberChange(previous, event, False)
        member = copy.deepcopy(previous)
        member["status"] = MemberStatus.REMOVED
        member["source_revision"] = previous["source_revision"] + 1
        return MemberChange(member, event, True)
    if incoming is None:
        raise MemberTransitionError(f"Source member {identity} event {event!r} requires a source version.")
    if event == MemberEvent.NEW_REVISION:
        if previous is not None and incoming["source_revision"] <= previous["source_revision"]:
            raise MemberTransitionError(
                f"Source member {identity} revision {incoming['source_revision']} does not follow "
                f"{previous['source_revision']}.")
        member = copy.deepcopy(incoming)
    elif event == MemberEvent.REDIRECT:
        assert previous is not None
        if previous["source_revision"] > incoming["source_revision"] or (
                previous["source_revision"] == incoming["source_revision"] and before == MemberStatus.REMOVED):
            raise MemberTransitionError("A stale receipt cannot redirect a newer source version.")
        if before == MemberStatus.REDIRECTED and previous["source_revision"] >= incoming["source_revision"]:
            return MemberChange(previous, event, False)
        member = copy.deepcopy(incoming)
        member["status"] = MemberStatus.REDIRECTED
    else:
        if previous is not None and (
                previous["source_revision"] > incoming["source_revision"] or
                (previous["source_revision"] == incoming["source_revision"] and
                 (event == MemberEvent.SOURCE_RECEIPT or before != MemberStatus.ACTIVE))):
            return MemberChange(previous, MemberEvent.STALE_RECEIPT, False)
        member = copy.deepcopy(incoming)
        if previous is not None and before == MemberStatus.REDIRECTED and event == MemberEvent.RECEIPT:
            member["status"] = MemberStatus.REDIRECTED
            member["replacement_master_msg_id"] = previous["replacement_master_msg_id"]
    return MemberChange(member, event, True)


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
        source_revision=source_revision, status=MemberStatus.ACTIVE, replacement_master_msg_id=None,
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


def _author_html(header: str, member: SourceMember,
                 author_emoji: Callable[[str], UserEmoji | None] | None) -> str:
    """The author header as HTML, with the author's custom emoji where the header shows its fallback emoji.

    The header got the fallback emoji when the source was received, if the author's emoji was ready then.
    """
    channel_id, chat_uid, _group = chat_id_str_to_id(EFBChannelChatIDStr(member["author_uid"]))
    emoji = author_emoji(chat_id_to_str(channel_id=channel_id, chat_uid=chat_uid)) if author_emoji else None
    offset = header.find(f"{emoji.fallback} {member['author_name']}") if emoji else -1
    if emoji is None or offset < 0:
        return html.escape(header)
    return AuthorEmojiHeader(header, emoji, offset).html_with_emoji()


def render_members(members: List[SourceMember], *, admin_id: Optional[int] = None,
                   history: bool = False, display_timezone: ZoneInfo = DEFAULT_TIMEZONE,
                   confirmed: Optional[AggregatePayload] = None,
                   author_emoji: Callable[[str], UserEmoji | None] | None = None,
                   _compact_status: bool = False) -> RenderedAggregate:
    """Render every real source separately; ranges refer to parsed UTF-16 text.

    ``author_emoji`` looks up an author's custom emoji in memory; the HTML then carries it in that
    author's header. The text and ranges are the same either way.
    """
    html_parts: List[str] = []
    text_parts: List[str] = []
    ranges: List[MemberRange] = []
    removed_displays = {}
    if confirmed and not history:
        removed = {member_identity(child) for child in confirmed["children"] if child["status"] == MemberStatus.REMOVED}
        encoded = confirmed["confirmed_text"].encode("utf-16-le")
        for member_range in confirmed["confirmed_ranges"]:
            range_identity = member_range["origin_uid"], member_range["source_id"]
            if range_identity in removed:
                visible = encoded[member_range["start"] * 2:member_range["end"] * 2].decode("utf-16-le")
                if visible.endswith(("[message removed]", "[removed]")):
                    removed_displays[range_identity] = visible
    offset = 0
    for member in members:
        if history and member["status"] == MemberStatus.REDIRECTED:
            continue
        name = member.get("display_prefix")
        if name is None:
            name = member["author_name"]
        # Status displays remain bounded even after many source edits.
        if not history and member["status"] == MemberStatus.REDIRECTED:
            name = name[:80]
            body = "[message moved]"
            if _compact_status:
                name = ""
                body = "[moved]"
            body_html = html.escape(body)
            reply_text = ""
        else:
            message = member_message(member)
            body = message.text or ""
            body_html = _body_html(message, admin_id, member["self_mentions"])
            if not history and member["status"] == MemberStatus.REMOVED:
                body_html = removed_text(body)
                if not body:
                    body = "[message removed]"
            reply = member["reply"]
            reply_text = ""
            if reply:
                identity = f'{reply["origin_uid"]}/{reply["source_id"]}'
                reply_text = f'↪ {reply["author_name"][:60]} [{identity[:100]}]: {reply["excerpt"][:120]}\n'
        if history:
            prefix = f"{name}:\n" if name else ""
            prefix_html = html.escape(prefix)
        else:
            display_time = member["source_time"] or member["received_time"]
            timestamp = format_message_time(display_time, display_timezone)
            if _compact_status and member["status"] == MemberStatus.REDIRECTED:
                prefix = timestamp + " "
                prefix_html = f"<code>{timestamp}</code> "
            else:
                author_prefix = name if name.endswith(":") else f"{name}:"
                prefix = f"{author_prefix} {timestamp}\n" if name else f"{timestamp}\n"
                author_html = f"{_author_html(author_prefix, member, author_emoji)} " if name else ""
                prefix_html = author_html + f"<code>{timestamp}</code>\n"
        visible = prefix + reply_text + body
        visible_html = prefix_html + html.escape(reply_text) + body_html
        previous_display = removed_displays.get(member_identity(member))
        if member["status"] == MemberStatus.REMOVED and previous_display is not None and previous_display != visible:
            # Older containers already replaced these sources with a removal marker.
            visible, visible_html = previous_display, html.escape(previous_display)
        if text_parts:
            offset += 2
        ranges.append(MemberRange(origin_uid=member["origin_uid"], source_id=member["source_id"],
                                  start=offset, end=offset + utf16_length(visible)))
        offset += utf16_length(visible)
        text_parts.append(visible)
        html_parts.append(visible_html)
    if not text_parts:
        return RenderedAggregate("[message removed]", "[message removed]", [])
    if (not history and not _compact_status and
            len("\n\n".join(text_parts)) > int(MessageLimit.MAX_TEXT_LENGTH) and
            any(member["status"] == MemberStatus.REDIRECTED for member in members)):
        return render_members(members, admin_id=admin_id, display_timezone=display_timezone,
                              confirmed=confirmed, author_emoji=author_emoji, _compact_status=True)
    return RenderedAggregate("\n\n".join(html_parts), "\n\n".join(text_parts), ranges)


def make_aggregate(members: List[SourceMember], revision: int, *,
                   admin_id: Optional[int] = None, display_timezone: ZoneInfo = DEFAULT_TIMEZONE,
                   confirmed: Optional[AggregatePayload] = None) -> AggregatePayload:
    rendered = render_members(members, admin_id=admin_id, display_timezone=display_timezone, confirmed=confirmed)
    return AggregatePayload(format_version=1, revision=revision, children=members,
                            confirmed_text=rendered.text, confirmed_ranges=rendered.ranges)


def aggregate_fits(members: List[SourceMember], *, max_members: int = 200,
                   max_payload_bytes: int = 256 * 1024, admin_id: Optional[int] = None,
                   include_redirected_payload: bool = True, display_timezone: ZoneInfo = DEFAULT_TIMEZONE,
                   confirmed: Optional[AggregatePayload] = None) -> bool:
    """Enforce display/member limits and saved payload capacity.

    Redirected members retain their full snapshots after replacement confirmation;
    fixed-container updates can omit them from payload capacity.
    """
    payload_members = members if include_redirected_payload else [
        member for member in members if member["status"] != MemberStatus.REDIRECTED]
    return (len(members) <= max_members and
            len(render_members(members, admin_id=admin_id, display_timezone=display_timezone,
                               confirmed=confirmed).text) <= int(MessageLimit.MAX_TEXT_LENGTH) and
            len(pickle.dumps(payload_members, protocol=5)) <= max_payload_bytes)
