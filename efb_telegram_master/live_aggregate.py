"""Logical live text batches executed by the existing outbound worker."""

from __future__ import annotations

import copy
import inspect
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Optional

from .aggregate import aggregate_fits, make_aggregate, member_identity, member_message, render_members
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
        self._source_delivery = ContextVar("aggregate_source_delivery", default=None)
        self._independent: ContextVar[Optional[tuple]] = ContextVar("aggregate_independent", default=None)
        rows = self.queue.aggregation_rows()
        publication_rows = {}
        predecessors = {}
        for row in rows:
            context = decode_aggregation(row.log_context)
            logical = context.get("retry_context") or context
            if context.get("routing_hint"):
                self.container_tails[context["container_id"]] = row.id
                continue
            key = tuple(logical["key"])
            publication_rows.setdefault(key, []).append((row, logical))
            if row.predecessor_id is not None:
                predecessors.setdefault(key, set()).add(row.predecessor_id)
            stream = self.streams.setdefault(key, _Stream())
            stream.generation = max(stream.generation, logical.get("generation", 0))
            container_id = context.get("container_id") or logical.get("container_id")
            if container_id:
                self.container_tails[container_id] = row.id
            for member in logical.get("members", []):
                self.pending_sources[member_identity(member)] = row.id
        # Splitting an older batch inserts rows ahead of already queued media.
        # The terminal dependency, rather than its insertion ID, ends the stream.
        for key, candidates in publication_rows.items():
            terminal = [(row, context) for row, context in candidates if row.id not in predecessors.get(key, set())]
            row, logical = max(terminal or candidates, key=lambda item: item[0].id)
            stream = self.streams[key]
            stream.tail = row.id
            stream.base = logical.get("base") if logical["kind"] not in {"boundary", "source", "redirect"} else None
            stream.recent = logical.get("recent", [])
            stream.last_new = logical.get("last_new", row.created_at)
            if logical["kind"] in {"boundary", "source", "redirect"} and logical.get("generation", 0) < stream.generation:
                # Earlier split rows cannot reopen a container beyond this boundary.
                stream.generation += 1

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

    def close_source_routes(self, key):
        """End former target chats/topics when normal source delivery changes route."""
        key = tuple(key)
        prior_tail = None
        for old_key, old_stream in self.streams.items():
            if old_key[0] == key[0] and old_key != key:
                prior_tail = self._existing_tail(old_stream) or prior_tail
                self.close(old_key)
        return prior_tail

    def append(self, key, member, *, silent=False):
        """Return False only when this single source needs independent delivery."""
        key = tuple(key)
        identity = member_identity(member)
        with self.scheduler._lock:
            if self.scheduler.stopping:
                from .outbound import SchedulerStoppedError
                raise SchedulerStoppedError("Outbound scheduler stopped.")
            if self.queue.cancelled_source(identity):
                return True
            if identity in self.pending_sources:
                pending = self.queue.aggregation_context(self.pending_sources[identity])
                if pending and tuple(decode_aggregation(pending)["key"]) == key:
                    return True
            if self.manager.channel.db.resolve_source_member(identity[0], identity[1], str(key[1]), key[2]):
                return True
            if not self.fits([member]):
                return False
            now = member["received_time"].timestamp()
            prior_tail = self.close_source_routes(key)
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
        source = self._source_delivery.get()
        stream = self.streams[key]
        contexts = []
        for request in requests:
            if source is not None:
                context = copy.deepcopy(source)
                context.pop("split", None)
                if request.log_context:
                    from .queued_log import decode
                    context["message"] = decode(request.log_context)[0]
                context.setdefault("message", member_message(context["member"]))
            else:
                context = dict(format_version=1, kind="boundary", key=key, generation=stream.generation,
                               recent=stream.recent, last_new=stream.last_new,
                               members=[], legacy_context=request.log_context)
            contexts.append(replace(request, predecessor_id=predecessor, log_context=encode_aggregation(context)))
        return contexts

    def note_independent_enqueued(self, row_id):
        boundary = self._independent.get()
        if boundary is not None:
            key, _, reserved = boundary
            source = self._source_delivery.get()
            split = source and source.get("split")
            if split:
                for row in self.queue.aggregation_rows():
                    context = decode_aggregation(row.log_context)
                    if tuple(context["key"]) == key and context["kind"] == "logical":
                        for member in context["members"]:
                            self.pending_sources[member_identity(member)] = row.id
                self.pending_sources.pop(member_identity(source["member"]), None)
                if split["tail"] == split["row_id"]:
                    self.streams[key].tail = max(row.id for row in self.queue.aggregation_rows() if tuple(decode_aggregation(row.log_context)["key"]) == key)
                else:
                    self.streams[key].tail = split["tail"]
                    tail = self._tail_context(self.streams[key])
                    if tail and tail["kind"] in {"boundary", "source", "redirect"}:
                        self.close(key)
            else:
                self.streams[key].tail = int(row_id)
            if source is not None:
                source.pop("split", None)
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
        if context and context["kind"] in {"source", "redirect"} and not context.get("frozen"):
            member = context["member"]
            resolved = self.manager.channel.db.resolve_source_member(*member_identity(member),
                str(context["key"][1]), context["key"][2])
            operation = row.operation
            args, kwargs = self.queue.decode_payload_raw(row.payload)
            from telegram import Bot
            kwargs = dict(inspect.signature(getattr(Bot, operation)).bind(None, *args, **kwargs).arguments)
            kwargs.pop("self")
            if resolved:
                log = resolved[0]
                if member["status"] == "removed" and not log.aggregate:
                    operation = "edit_message_text" if log.media_type == "Text" else "delete_message"
                    kwargs = dict(chat_id=context["key"][1], message_id=int((log.master_msg_id_alt or log.master_msg_id).rsplit(".", 1)[1]))
                    if operation == "edit_message_text":
                        kwargs["text"] = "[message removed]"
                if log.aggregate:
                    context["kind"] = "redirect"
                    context["old_container_id"] = log.master_msg_id
                else:
                    context["kind"] = "source"
                    context["old_container_id"] = None
                    context["old_message_id"] = tuple(int(value) for value in log.master_msg_id.rsplit(".", 1))
                    if operation == "send_message" and log.media_type == "Text":
                        operation = "edit_message_text"
                        kwargs = {name: value for name, value in kwargs.items()
                                  if name in {"chat_id", "text", "parse_mode", "reply_markup", "link_preview_options"}}
                        kwargs["message_id"] = int((log.master_msg_id_alt or log.master_msg_id).rsplit(".", 1)[1])
            context["frozen"] = True
            return self.queue.materialize_aggregation(row.id, operation, kwargs, encode_aggregation(context), selection)
        if context is None or context["kind"] not in {"logical", "member_update"}:
            return row
        if context["kind"] == "member_update":
            if not context.get("container_id"):
                member = context["updates"][0]
                resolved = self.manager.channel.db.resolve_source_member(*member_identity(member), str(context["key"][1]), context["key"][2])
                if not resolved or not resolved[0].aggregate:
                    raise ValueError("Deferred source update requires its confirmed container.")
                context["container_id"] = resolved[0].master_msg_id
            log = self.manager.channel.db.get_msg_log(master_msg_id=context["container_id"])
            children = copy.deepcopy(log.aggregate["children"])
            updates = {member_identity(member): member for member in context["updates"]}
            for index, previous in enumerate(children):
                update = updates.get(member_identity(previous))
                if update and update["source_revision"] > previous["source_revision"]:
                    children[index] = copy.deepcopy(update)
                    if previous["status"] == "redirected":
                        children[index]["status"] = "redirected"
                        children[index]["replacement_master_msg_id"] = previous["replacement_master_msg_id"]
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
                      container_id=f"{context['key'][1]}.{base['message_id']}" if can_edit else None,
                      routing_hint=context.get("routing_hint", False),
                      members=context.get("members", []), appendable=context.get("appendable", True), handoff=False)
        result = self.queue.materialize_aggregation(row.id, operation, kwargs, encode_aggregation(frozen), selection)
        if can_edit:
            self.container_tails[f"{context['key'][1]}.{base['message_id']}"] = row.id
        return result

    def reconcile(self, row, receipt, sender_bot_id, file_bot_id=None):
        context = decode_aggregation(row.log_context)
        if context is None:
            return False
        if context["kind"] in {"source", "redirect"}:
            return self._reconcile_source(row, receipt, sender_bot_id, context, file_bot_id)
        if context["kind"] != "aggregate":
            raise ValueError("A logical aggregation batch cannot have a Telegram receipt.")
        saved = self.manager.channel.db.finalize_aggregate_message(receipt, context["aggregate"], sender_bot_id)
        base = dict(message_id=receipt.message_id, owner=sender_bot_id, aggregate=saved.aggregate)

        def update_successor(payload):
            successor = decode_aggregation(payload)
            if successor.get("inherit") and tuple(successor["key"]) == tuple(context["key"]):
                successor["base"] = base
                successor["inherit"] = False
            if successor["kind"] == "member_update" and not successor.get("container_id"):
                successor["container_id"] = saved.master_msg_id
                successor["required_sender"] = sender_bot_id or "__main__"
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

    def _reconcile_source(self, row, receipt, sender_bot_id, context, file_bot_id):
        from .msg_type import get_msg_type
        member = context["member"]
        message = context["message"]
        if isinstance(receipt, bool):
            from types import SimpleNamespace
            old_id = context["old_message_id"]
            receipt = SimpleNamespace(chat_id=old_id[0], message_id=old_id[1],
                                      message_thread_id=context["key"][2])
        else:
            message.type_telegram = get_msg_type(receipt)
            message.put_telegram_file(receipt)
        message.sender_bot_id = sender_bot_id
        message.file_bot_id = file_bot_id or sender_bot_id
        member = copy.deepcopy(member)
        # Telegram file metadata belongs to this output; the complete source stays in its member snapshot.
        source = member_message(member)
        source.file_id, source.file_unique_id = message.file_id, message.file_unique_id
        source.type_telegram, source.mime = message.type_telegram, message.mime
        source.file_bot_id = file_bot_id or sender_bot_id
        from .queued_log import encode
        member["snapshot"] = encode(source, None)
        db = self.manager.channel.db
        if context["kind"] == "redirect":
            saved = db.finalize_member_redirect(context["old_container_id"], member, message, receipt, sender_bot_id)
        else:
            saved = db.finalize_source_message(message, receipt, member, sender_bot_id,
                                               old_message_id=context.get("old_message_id"))
        requests = []
        if context["kind"] == "redirect":
            old = db.get_msg_log(master_msg_id=context["old_container_id"])
            redirected = next(child for child in old.aggregate["children"] if member_identity(child) == member_identity(member))
            chat_id, message_id = old.master_msg_id.rsplit(".", 1)
            hint = dict(format_version=1, kind="member_update", key=context["key"], container_id=old.master_msg_id,
                        updates=[redirected], members=[], due=time.time(), appendable=False,
                        generation=-1, recent=[], last_new=0, silent=False, routing_hint=True)
            requests.append(QueueRequest("edit_message_text", (), {"chat_id": int(chat_id), "message_id": int(message_id),
                "text": "", "_required_sender_bot_id": old.sender_bot_id or "__main__"}, encode_aggregation(hint),
                predecessor_id=self.container_tails.get(old.master_msg_id)
                    if self.container_tails.get(old.master_msg_id) and self.queue.contains(self.container_tails[old.master_msg_id]) else None))
        context["handoff"] = True
        def update_successor(payload):
            successor = decode_aggregation(payload)
            if successor.get("member") and member_identity(successor["member"]) == member_identity(member):
                successor["old_message_id"] = tuple(int(value) for value in saved.master_msg_id.rsplit(".", 1))
                successor["required_sender"] = sender_bot_id or "__main__"
            return encode_aggregation(successor)
        identifiers = self.queue.handoff_aggregation(row.id, encode_aggregation(context), update_successor,
                requests=requests, operation_resolver=self.manager._queue_operation)
        if identifiers:
            self.container_tails[context["old_container_id"]] = identifiers[-1]
        if message.commands and member["status"] == "active":
            from ehforwarderbot import coordinator
            from .commands import ETMCommandMsgStorage
            module = coordinator.get_module_by_id(message.author.module_id)
            self.manager.channel.commands.register_command(receipt,
                ETMCommandMsgStorage(message.commands, module, member.get("display_prefix") or "", message.text))
        return True

    def remove_source_member(self, key, identity):
        """Apply a confirmed source removal to one member, retaining its full saved source."""
        with self.scheduler._lock:
            log, previous, predecessor = self.source_state(key, identity)
            if previous is None:
                return False
            if previous["status"] == "removed":
                return True
            member = copy.deepcopy(previous)
            member.update(status="removed", source_revision=previous["source_revision"] + 1)
            if self.update_pending_member(key, member):
                return True
            payload = self.queue.aggregation_context(predecessor) if predecessor else None
            pending = decode_aggregation(payload) if payload else None
            independent_pending = pending and pending["kind"] in {"source", "redirect"}
            if independent_pending and log is None:
                cancelled = self.queue.cancel_pending_source(tuple(key), member)
                if cancelled:
                    for stream in self.streams.values():
                        if stream.tail in cancelled:
                            stream.tail = cancelled[stream.tail]
                    self.scheduler.wake_event.set()
                    return True
            if independent_pending:
                context = dict(format_version=1, kind="source", key=tuple(key), member=member,
                               message=member_message(member), old_message_id=None, old_container_id=None,
                               generation=-1, members=[], handoff=False)
                request = QueueRequest("send_message", (), {"chat_id": key[1], "text": "[message removed]"},
                                       encode_aggregation(context), predecessor_id=predecessor)
                row_id, _ = self.queue.enqueue_many([request], self.manager._queue_operation)
            elif log and log.aggregate:
                self.queue_container_update(log.master_msg_id, [member], key=key)
            elif log is None:
                self.queue_deferred_update(key, member, predecessor)
            else:
                old_id = tuple(int(value) for value in (log.master_msg_id_alt or log.master_msg_id).rsplit(".", 1))
                context = dict(format_version=1, kind="source", key=tuple(key), member=member,
                               message=member_message(member), old_message_id=old_id, old_container_id=None,
                               generation=-1, members=[], handoff=False, frozen=True)
                operation = "edit_message_text" if log.media_type == "Text" else "delete_message"
                kwargs = dict(chat_id=old_id[0], message_id=old_id[1], _required_sender_bot_id=log.sender_bot_id or "__main__")
                if operation == "edit_message_text":
                    kwargs["text"] = "[message removed]"
                request = QueueRequest(operation, (), kwargs, encode_aggregation(context), predecessor_id=predecessor)
                row_id, _ = self.queue.enqueue_many([request], self.manager._queue_operation)
            self.scheduler.wake_event.set()
            return True

    def source_state(self, key, identity):
        """Resolve the newest saved source version in this destination, including queued changes."""
        key = tuple(key)
        resolved = self.manager.channel.db.resolve_source_member(*identity, str(key[1]), key[2])
        log, member = resolved if resolved else (None, None)
        cancellation = self.queue.cancelled_source(identity)
        if cancellation and (member is None or cancellation["member"]["source_revision"] > member["source_revision"]):
            member = cancellation["member"]
        predecessor = None
        for row in self.queue.aggregation_rows():
            context = decode_aggregation(row.log_context)
            if tuple(context["key"]) != key:
                continue
            if log is not None and log.aggregate is None and context["kind"] in {"aggregate", "member_update", "logical"}:
                continue
            candidates = list(context.get("members", [])) + list(context.get("updates", []))
            if context.get("member"):
                candidates.append(context["member"])
            if context.get("aggregate"):
                candidates += context["aggregate"]["children"]
            for candidate in candidates:
                if member_identity(candidate) == identity:
                    if member is None or candidate["source_revision"] >= member["source_revision"]:
                        predecessor = row.id
                        member = candidate
        return log, member, predecessor

    def prospective_members(self, container_id, key):
        """Account for predecessor snapshots when checking whether a source edit still fits."""
        children = copy.deepcopy(self.manager.channel.db.get_container_members(container_id))
        indexed = {member_identity(member): member for member in children}
        message_id = int(container_id.rsplit(".", 1)[1])
        stream = self.streams.get(tuple(key))
        for row in self.queue.aggregation_rows():
            context = decode_aggregation(row.log_context)
            if tuple(context["key"]) != tuple(key):
                continue
            candidates = []
            if context.get("container_id") == container_id:
                candidates = context.get("updates", [])
                if context.get("aggregate"):
                    candidates = context["aggregate"]["children"]
            elif context["kind"] == "logical":
                base = context.get("base") or (stream.base if stream and context.get("inherit") else None)
                if base and base["message_id"] == message_id:
                    candidates = context["members"]
            for candidate in candidates:
                identity = member_identity(candidate)
                previous = indexed.get(identity)
                if previous is None:
                    previous = copy.deepcopy(candidate)
                    children.append(previous)
                    indexed[identity] = previous
                elif candidate["source_revision"] > previous["source_revision"]:
                    previous.update(copy.deepcopy(candidate))
        return children

    def queue_deferred_update(self, key, member, predecessor):
        """Save an edit of a first-send member until its parent has a real receipt."""
        with self.scheduler._lock:
            context = dict(format_version=1, kind="member_update", key=tuple(key), container_id=None,
                           updates=[member], members=[], due=time.time(), appendable=False,
                           generation=-1, recent=[], last_new=0, silent=False)
            request = QueueRequest("edit_message_text", (), {"chat_id": key[1], "message_id": 0, "text": "",
                                   "_required_sender_bot_id": "__main__"},
                                   encode_aggregation(context), predecessor_id=predecessor)
            row_id, _ = self.queue.enqueue_many([request], self.manager._queue_operation)
            self.scheduler.wake_event.set()
            return row_id

    @contextmanager
    def source_delivery(self, key, member, *, old_container_id=None, old_message_id=None, predecessor=None):
        """Attach durable source finalization to the existing independent dispatch path."""
        with self.independent(key):
            boundary_key, stream_predecessor, reserved = self._independent.get()
            if old_message_id and stream_predecessor:
                payload = self.queue.aggregation_context(stream_predecessor)
                tail = decode_aggregation(payload) if payload else None
                if tail and tail.get("routing_hint"):
                    stream_predecessor = None
            parents = [identifier for identifier in (predecessor, stream_predecessor) if identifier is not None and self.queue.contains(identifier)]
            self._independent.set((boundary_key, max(parents) if parents else None, reserved))
            context = dict(format_version=1, kind="source", key=tuple(key), member=member,
                           old_container_id=old_container_id, old_message_id=old_message_id,
                           generation=self.streams[tuple(key)].generation, members=[], handoff=False,
                           recent=self.streams[tuple(key)].recent, last_new=self.streams[tuple(key)].last_new)
            pending_id = self.pending_sources.get(member_identity(member))
            payload = self.queue.aggregation_context(pending_id) if pending_id else None
            pending = decode_aggregation(payload) if payload else None
            if pending and pending["kind"] == "logical" and tuple(pending["key"]) == tuple(key):
                index = next(index for index, child in enumerate(pending["members"]) if member_identity(child) == member_identity(member))
                before, after = copy.deepcopy(pending), copy.deepcopy(pending)
                before["members"] = pending["members"][:index]
                after.update(members=pending["members"][index + 1:], base=None, inherit=False,
                             generation=self.streams[tuple(key)].generation)
                context["split"] = dict(row_id=pending_id, before=before if before["members"] else None,
                                        after=after if after["members"] else None, tail=self.streams[tuple(key)].tail)
            token = self._source_delivery.set(context)
            try:
                yield
            finally:
                self._source_delivery.reset(token)

    def source_split(self):
        source = self._source_delivery.get()
        return source.get("split") if source else None

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
                    if member["status"] == "removed":
                        context["members"].pop(position)
                        self.pending_sources.pop(member_identity(member), None)
                        if not context["members"]:
                            predecessor = self.queue.cancel_logical_aggregation(row_id, cancelled_member=member)
                            for stream in self.streams.values():
                                if stream.tail == row_id:
                                    stream.tail = predecessor
                            self.scheduler.wake_event.set()
                            return True
                    else:
                        context["members"][position] = member
                    if not self.fits(context["members"]):
                        return False
                    return self.queue.replace_logical_aggregation(row_id, encode_aggregation(context),
                        cancelled_member=member if member["status"] == "removed" else None)
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
