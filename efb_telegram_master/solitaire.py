# coding=utf-8

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import List, Optional, Sequence, Tuple

from typing_extensions import Literal


ActionType = Literal["EDIT", "SEND", "DROP"]


@dataclass(frozen=True)
class ActionPlan:
    action_type: ActionType
    canonical_master_msg_id: Optional[str] = None
    editable_master_msg_id: Optional[str] = None
    replacement_text: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class SolitaireItem:
    index: int
    content: str


@dataclass(frozen=True)
class SolitaireParse:
    layout: str
    items: Tuple[SolitaireItem, ...]


@dataclass(frozen=True)
class SolitaireCandidate:
    slave_message_id: str
    canonical_master_msg_id: str
    editable_master_msg_id: str
    text: str


_HEADER_VALUES = ("#接龙", "#接龍")
_ITEM_RE = re.compile(r"^\s*(\d+)([.])\s+(.+?)\s*$")


def has_solitaire_header(text: Optional[str]) -> bool:
    if not text:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped in _HEADER_VALUES
    return False


def parse_solitaire(text: Optional[str]) -> Optional[SolitaireParse]:
    if not has_solitaire_header(text):
        return None
    assert text is not None

    items: List[SolitaireItem] = []
    layout: Optional[str] = None
    for line in text.splitlines():
        match = _ITEM_RE.match(line)
        if not match:
            continue
        item_layout = match.group(2)
        if layout is None:
            layout = item_layout
        elif item_layout != layout:
            return None
        items.append(SolitaireItem(int(match.group(1)), match.group(3).rstrip()))

    if not items:
        return None
    if [i.index for i in items] != list(range(1, len(items) + 1)):
        return None
    return SolitaireParse(layout=layout or ".", items=tuple(items))


def build_command_text(base_text: str, payload: str) -> Optional[str]:
    parsed = parse_solitaire(base_text)
    payload = payload.strip()
    if parsed is None or not payload:
        return None

    replacement = base_text.rstrip() + f"\n{len(parsed.items) + 1}. {payload}"
    if parse_solitaire(replacement) is None:
        return None
    return replacement


def resolve_solitaire_action(
        text: str,
        msg_uid: Optional[str],
        candidates: Sequence[SolitaireCandidate],
        *,
        command: str = "jl`",
        command_base: Optional[SolitaireCandidate] = None,
) -> ActionPlan:
    if text.startswith(command):
        base = command_base or next(iter(candidates), None)
        if base is None:
            return ActionPlan("DROP", reason="command_without_candidate")
        replacement = build_command_text(base.text, text[len(command):])
        if replacement is None:
            return ActionPlan("DROP", reason="invalid_command_append")
        return ActionPlan(
            "EDIT",
            canonical_master_msg_id=base.canonical_master_msg_id,
            editable_master_msg_id=base.editable_master_msg_id,
            replacement_text=replacement,
            reason="command_append",
        )

    new_parsed = parse_solitaire(text)
    if new_parsed is None:
        return ActionPlan("SEND", reason="not_solitaire")

    scored: List[Tuple[float, SolitaireCandidate, str]] = []
    for candidate in candidates:
        if msg_uid and candidate.slave_message_id == msg_uid:
            continue
        old_parsed = parse_solitaire(candidate.text)
        if old_parsed is None or old_parsed.layout != new_parsed.layout:
            continue
        score, reason = _score_match(old_parsed, new_parsed)
        if score > 0:
            scored.append((score, candidate, reason))

    if not scored:
        return ActionPlan("SEND", reason="no_match")

    scored.sort(key=lambda i: i[0], reverse=True)
    best_score, best_candidate, reason = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= 0.9 and best_score - second_score >= 0.15:
        return ActionPlan(
            "EDIT",
            canonical_master_msg_id=best_candidate.canonical_master_msg_id,
            editable_master_msg_id=best_candidate.editable_master_msg_id,
            reason=reason,
        )
    return ActionPlan("SEND", reason="ambiguous_match")


def _score_match(old: SolitaireParse, new: SolitaireParse) -> Tuple[float, str]:
    old_items = [i.content for i in old.items]
    new_items = [i.content for i in new.items]

    if len(new_items) > len(old_items):
        changed = _changed_count(old_items, new_items[:len(old_items)])
        if len(old_items) < 5 and changed > 0:
            return 0.0, "continuation_prefix_changed_short"
        if len(old_items) >= 5 and changed > 1:
            return 0.0, "continuation_prefix_changed"
        if len(new_items) - len(old_items) > 20:
            return 0.0, "continuation_growth_too_large"
        return max(0.9, 1.0 - changed * 0.05), "continuation"

    if len(new_items) == len(old_items):
        if len(old_items) < 5:
            return 0.0, "correction_too_short"
        changed = _changed_count(old_items, new_items)
        if changed <= 2:
            return 0.96 - changed * 0.02, "correction"
        return 0.0, "correction_too_many_changes"

    if len(old_items) >= 3 and len(new_items) == len(old_items) - 1:
        if _is_single_deletion(old_items, new_items):
            return 0.94, "deletion"
        return 0.0, "deletion_with_changes"

    return 0.0, "unsupported_change"


def _changed_count(old_items: Sequence[str], new_items: Sequence[str]) -> int:
    return sum(1 for old, new in zip(old_items, new_items) if old != new)


def _is_single_deletion(old_items: Sequence[str], new_items: Sequence[str]) -> bool:
    matcher = SequenceMatcher(a=list(old_items), b=list(new_items), autojunk=False)
    deleted = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete" and (i2 - i1) == 1 and j1 == j2:
            deleted += 1
            continue
        return False
    return deleted == 1
