"""Logical live text batches executed by the existing outbound worker."""

from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Optional

from .aggregate import aggregate_fits, make_aggregate, member_identity, render_members
from .outbound import QueueRequest
from .queued_log import decode_aggregation, encode_aggregation


@dataclass
class _Stream:
    tail: Optional[int] = None
    base: Optional[dict] = None
    recent: list[float] = field(default_factory=list)
    last_new: float = 0
    generation: int = 0


class LiveTextAggregation:
    """Keep append eligibility in memory and every unpublished member durable."""

    def __init__(self, manager):
        self.manager = manager
        self.queue = manager._outbound_queue
        self.scheduler = manager._outbound_scheduler
        self.streams: dict[tuple, _Stream] = {}
        self.pending_sources: dict[tuple, int] = {}
        self.container_tails: dict[str, int] = {}
        self._independent: ContextVar[Optional[tuple]] = ContextVar("aggregate_independent", default=None)
        for row in self.queue.aggregation_rows():
            context = decode_aggregation(row.log_context)
            logical = context.get("retry_context") or context
            key = tuple(logical["key"])
            stream = self.streams.setdefault(key, _Stream())
            stream.tail = row.id
            stream.base = logical.get("base")
            stream.recent = logical.get("recent", [])
            stream.last_new = logical.get("last_new", row.created_at)
            stream.generation = max(stream.generation, logical.get("generation", 0))
            if logical.get("container_id"):
                self.container_tails[logical["container_id"]] = row.id
            for member in logical.get("members", []):
                self.pending_sources[member_identity(member)] = row.id

    @property
    def admin_id(self):
        admins = self.manager.channel.config.get("admins", [])
        return admins[0] if admins else None

    def fits(self, members):
        flag = self.manager.channel.flag
        return aggregate_fits(members, max_members=int(flag("text_aggregation_max_members")),
                              max_payload_bytes=int(flag("text_aggregation_max_payload_bytes")),
                              admin_id=self.admin_id)

    def _tail_context(self, stream):
        if stream.tail is None:
            return None
        payload = self.queue.aggregation_context(stream.tail)
        return decode_aggregation(payload) if payload else None

    def _existing_tail(self, stream):
        return stream.tail if stream.tail is not None and self.queue.contains(stream.tail) else None

    def close(self, key):
        stream = self.streams.get(tuple(key))
        if stream is not None:
            stream.base = None
            stream.generation += 1

    def append(self, key, member, *, silent=False):
        """Return False only when this single source needs independent delivery."""
        key = tuple(key)
        identity = member_identity(member)
        with self.scheduler._lock:
            if self.scheduler.stopping:
                from .outbound import SchedulerStoppedError
                raise SchedulerStoppedError("Outbound scheduler stopped.")
            if identity in self.pending_sources:
                return True
            if self.manager.channel.db.resolve_source_member(identity[0], identity[1], str(key[1]), key[2]):
                return True
            if not self.fits([member]):
                return False
            now = member["received_time"].timestamp()
            # A destination change ends the former source stream.
            prior_tail = None
            for old_key, old_stream in self.streams.items():
                if old_key[0] == key[0] and old_key != key and old_key[1] != key[1]:
                    prior_tail = self._existing_tail(old_stream) or prior_tail
                    self.close(old_key)
            stream = self.streams.setdefault(key, _Stream())
            if stream.last_new and now - stream.last_new >= float(self.manager.channel.flag("text_aggregation_idle_seconds")):
                self.close(key)
            stream.recent = [stamp for stamp in stream.recent if now - stamp < 3][-2:] + [now]
            stream.last_new = now
            context = self._tail_context(stream)
            if context and context["kind"] == "logical" and context["generation"] == stream.generation:
                members = context["members"] + [member]
                if self.fits(members):
                    context.update(members=members, recent=stream.recent, last_new=now)
                    if not context.get("waiting") and len(stream.recent) > 2:
                        context.update(waiting=True, due=context["members"][0]["received_time"].timestamp()
                                       + float(self.manager.channel.flag("text_aggregation_window_seconds")))
                    if self.queue.replace_logical_aggregation(stream.tail, encode_aggregation(context)):
                        self.pending_sources[identity] = stream.tail
                        self.scheduler.wake_event.set()
                        return True
                # Pending capacity becomes a publication boundary.
                self.close(key)
            predecessor = self._existing_tail(stream) or prior_tail
            inherit = bool(predecessor and context and context.get("generation") == stream.generation)
            waiting = len(stream.recent) > 2
            context = dict(format_version=1, kind="logical", key=key, members=[member],
                           base=stream.base, inherit=inherit, generation=stream.generation,
                           recent=stream.recent, last_new=now, waiting=waiting,
                           due=now + float(self.manager.channel.flag("text_aggregation_window_seconds")) if waiting else now,
                           silent=silent)
            request = QueueRequest("send_message", (), {"chat_id": key[1], "text": "", "_slave_id": key[0],
                                                         "_send_mode": "eventual"},
                                   encode_aggregation(context), predecessor_id=predecessor)
            row_id, _ = self.queue.enqueue_many([request], self.manager._queue_operation)
            stream.tail = row_id
            self.pending_sources[identity] = row_id
            self.scheduler.wake_event.set()
            return True

    @contextmanager
    def independent(self, key):
        """Order the existing independent send between this topic's text batches."""
        key = tuple(key)
        # Reserve the ordering boundary until the first independent request is
        # durable. The enqueue hook releases this lock before any blocking wait.
        self.scheduler._lock.acquire()
        reserved = [True]
        try:
            stream = self.streams.setdefault(key, _Stream())
            self.close(key)
            token = self._independent.set((key, self._existing_tail(stream), reserved))
            try:
                yield
            finally:
                self._independent.reset(token)
        finally:
            if reserved[0]:
                self.scheduler._lock.release()

    def bind_independent_requests(self, requests):
        boundary = self._independent.get()
        if boundary is None:
            return requests
        key, predecessor, _ = boundary
        return [replace(request, predecessor_id=predecessor) for request in requests]

    def note_independent_enqueued(self, row_id):
        boundary = self._independent.get()
        if boundary is not None:
            key, _, reserved = boundary
            self.streams[key].tail = int(row_id)
            # Additional operations in the same independent delivery stay ordered.
            self._independent.set((key, int(row_id), reserved))
            if reserved[0]:
                reserved[0] = False
                self.scheduler._lock.release()

    def deadline(self, row):
        # Scheduler heads deliberately omit contexts until a worker is available.
        if row.log_context is None:
            return None
        payload = self.queue.aggregation_context(row.id)
        context = decode_aggregation(payload) if payload else None
        return context.get("due") if context and context["kind"] in {"logical", "member_update"} else None

    def materialize(self, row, selection):
        context = decode_aggregation(row.log_context) if row.log_context else None
        if context is None or context["kind"] not in {"logical", "member_update"}:
            return row
        if context["kind"] == "member_update":
            log = self.manager.channel.db.get_msg_log(master_msg_id=context["container_id"])
            children = copy.deepcopy(log.aggregate["children"])
            updates = {member_identity(member): member for member in context["updates"]}
            for index, previous in enumerate(children):
                update = updates.get(member_identity(previous))
                if update and update["source_revision"] > previous["source_revision"]:
                    children[index] = update
            if not self.fits(children):
                raise ValueError("Source changes require independent output before container update.")
            base = dict(message_id=int(log.master_msg_id.rsplit(".", 1)[1]), owner=log.sender_bot_id,
                        aggregate=log.aggregate)
            members = []
        else:
            base = context.get("base")
            members = context["members"]
            children = None
        owner = base and base["owner"]
        can_edit = (base is not None and owner == selection.sender_bot_id
                    and self.fits(base["aggregate"]["children"] + members))
        if context["kind"] == "member_update" and not can_edit:
            raise ValueError("Existing aggregate updates require the original owner.")
        if children is None:
            children = base["aggregate"]["children"] + members if can_edit else members
        revision = base["aggregate"]["revision"] + 1 if can_edit else 1
        aggregate = make_aggregate(children, revision, admin_id=self.admin_id)
        kwargs = dict(chat_id=context["key"][1], text=render_members(children, admin_id=self.admin_id).html,
                      parse_mode="HTML", disable_notification=context["silent"], _live_aggregate=True)
        operation = "send_message"
        if can_edit:
            operation = "edit_message_text"
            kwargs.pop("disable_notification")
            kwargs["message_id"] = base["message_id"]
        elif context["key"][2] is not None:
            kwargs["message_thread_id"] = int(context["key"][2])
        frozen = dict(format_version=1, kind="aggregate", key=context["key"], aggregate=aggregate,
                      generation=context["generation"], recent=context["recent"], last_new=context["last_new"],
                      retry_context=copy.deepcopy(context) if context["kind"] == "logical" else None,
                      members=context.get("members", []), appendable=context.get("appendable", True), handoff=False)
        result = self.queue.materialize_aggregation(row.id, operation, kwargs, encode_aggregation(frozen), selection)
        if can_edit:
            self.container_tails[f"{context['key'][1]}.{base['message_id']}"] = row.id
        return result

    def reconcile(self, row, receipt, sender_bot_id):
        context = decode_aggregation(row.log_context)
        if context is None:
            return False
        if context["kind"] != "aggregate":
            raise ValueError("A logical aggregation batch cannot have a Telegram receipt.")
        saved = self.manager.channel.db.finalize_aggregate_message(receipt, context["aggregate"], sender_bot_id)
        base = dict(message_id=receipt.message_id, owner=sender_bot_id, aggregate=saved.aggregate)

        def update_successor(payload):
            successor = decode_aggregation(payload)
            if successor.get("inherit") and tuple(successor["key"]) == tuple(context["key"]):
                successor["base"] = base
                successor["inherit"] = False
            return encode_aggregation(successor)

        context["handoff"] = True
        self.queue.handoff_aggregation(row.id, encode_aggregation(context), update_successor)
        stream = self.streams.get(tuple(context["key"]))
        if stream and context.get("appendable", True) and stream.generation == context["generation"]:
            stream.base = base
        for member in context.get("members", []):
            if self.pending_sources.get(member_identity(member)) == row.id:
                self.pending_sources.pop(member_identity(member))
        return True

    def update_pending_member(self, key, member):
        """Merge a source edit/removal only while its request remains unsubmitted."""
        with self.scheduler._lock:
            row_id = self.pending_sources.get(member_identity(member))
            if row_id is None:
                return False
            payload = self.queue.aggregation_context(row_id)
            context = decode_aggregation(payload) if payload else None
            if context is None:
                return False
            if context["kind"] != "logical" or tuple(context["key"]) != tuple(key):
                return False
            for position, previous in enumerate(context["members"]):
                if member_identity(previous) == member_identity(member):
                    if member["source_revision"] <= previous["source_revision"]:
                        return True
                    context["members"][position] = member
                    if not self.fits(context["members"]):
                        return False
                    return self.queue.replace_logical_aggregation(row_id, encode_aggregation(context))
            return False

    def queue_container_update(self, container_id, updates, *, key=None):
        """Queue merged source changes with the container's fixed Telegram owner."""
        with self.scheduler._lock:
            log = self.manager.channel.db.get_msg_log(master_msg_id=container_id)
            if not log or not log.aggregate:
                raise ValueError("A container update requires an existing aggregate.")
            chat_id, message_id = container_id.rsplit(".", 1)
            if key is None:
                key = (log.aggregate["children"][0]["origin_uid"], int(chat_id), log.master_message_thread_id)
            key = tuple(key)
            stream = self.streams.get(key)
            appendable = bool(stream and stream.base and stream.base["message_id"] == int(message_id))
            predecessor = self._existing_tail(stream) if appendable else self.container_tails.get(container_id)
            if predecessor and not self.queue.contains(predecessor):
                predecessor = None
            if predecessor:
                payload = self.queue.aggregation_context(predecessor)
                context = decode_aggregation(payload) if payload else None
                if context and context["kind"] == "member_update" and context["container_id"] == container_id:
                    merged = {member_identity(member): member for member in context["updates"]}
                    for member in updates:
                        previous = merged.get(member_identity(member))
                        if previous is None or member["source_revision"] > previous["source_revision"]:
                            merged[member_identity(member)] = member
                    context["updates"] = list(merged.values())
                    if self.queue.replace_logical_aggregation(predecessor, encode_aggregation(context)):
                        self.scheduler.wake_event.set()
                        return predecessor
            context = dict(format_version=1, kind="member_update", key=key, container_id=container_id,
                           updates=list(updates), members=[], due=time.time(), appendable=appendable,
                           generation=stream.generation if appendable else -1,
                           recent=stream.recent if stream else [], last_new=stream.last_new if stream else 0,
                           silent=False)
            request = QueueRequest("edit_message_text", (), {"chat_id": int(chat_id), "message_id": int(message_id),
                                   "text": "", "_required_sender_bot_id": log.sender_bot_id or "__main__"},
                                   encode_aggregation(context), predecessor_id=predecessor)
            row_id, _ = self.queue.enqueue_many([request], self.manager._queue_operation)
            self.container_tails[container_id] = row_id
            if appendable:
                stream.tail = row_id
            self.scheduler.wake_event.set()
            return row_id
