"""Logical live text batches executed by the existing outbound worker."""

from __future__ import annotations

import copy
import inspect
import logging
import math
import pickle
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Optional

from .aggregate import (
    MemberEvent, MemberStatus, aggregate_fits, apply_member_event, make_aggregate, member_identity, member_message,
    render_members,
)
from .aggregation_states import CloseReason, ContextKind, ContextState, needs_delivered_predecessor
from .outbound import IllegalAggregationTransitionError, QueueRequest, UnconfirmedContainerError, check_receipt
from .queued_log import decode_aggregation, encode_aggregation
from .utils import removal_edit


@dataclass
class _Stream:
    tail: Optional[int] = None
    base: Optional[dict] = None
    recent: list[float] = field(default_factory=list)
    last_new: float = 0
    generation: int = 0
    # The (base, generation) the latest close replaced. Only a close overwrites it
    # and only a reopen consumes it; an append leaves it alone.
    closed: Optional[tuple] = None

    def close(self) -> None:
        self.closed = (self.base, self.generation)
        self.base = None
        self.generation += 1

    def reopen(self) -> None:
        if self.closed is not None:
            self.base, self.generation = self.closed
            self.closed = None


# Rows that end a container: later members of the source start a new one.
_INDEPENDENT_KINDS = frozenset({ContextKind.BOUNDARY, ContextKind.SOURCE, ContextKind.REDIRECT})


class LiveTextAggregation:
    # Members delivered this much later than they arrived are part of a backfill
    # rather than live traffic; their containers are held so the run can join.
    BACKFILL_LATENESS_SECONDS = 5.0
    """Keep append eligibility in memory and every unpublished member durable."""

    def __init__(self, manager):
        self.manager = manager
        self.display_timezone = manager.channel.flag.timezone
        self.queue = manager._outbound_queue
        self.scheduler = manager._outbound_scheduler
        self.streams: dict[tuple, _Stream] = {}
        self.pending_sources: dict[tuple, int] = {}
        self.container_tails: dict[str, int] = {}
        self._source_delivery = ContextVar("aggregate_source_delivery", default=None)
        self._independent: ContextVar[Optional[tuple]] = ContextVar("aggregate_independent", default=None)
        self._close_counts: dict[str, int] = {}
        self._close_logged_at = 0.0
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
            stream.base = logical.get("base") if logical.get("appendable", True) and logical["kind"] not in _INDEPENDENT_KINDS else None
            stream.recent = logical.get("recent", [])
            stream.last_new = logical.get("last_new", row.created_at)
            if logical["kind"] in _INDEPENDENT_KINDS and logical.get("generation", 0) < stream.generation:
                # Earlier split rows cannot reopen a container beyond this boundary.
                stream.generation += 1

    @property
    def admin_id(self):
        admins = self.manager.channel.config.get("admins", [])
        return admins[0] if admins else None

    def fits(self, members, *, updating=False, confirmed=None):
        flag = self.manager.channel.flag
        return aggregate_fits(members, max_members=int(flag("text_aggregation_max_members")),
                              max_payload_bytes=int(flag("text_aggregation_max_payload_bytes")),
                              admin_id=self.admin_id, include_redirected_payload=not updating,
                              display_timezone=self.display_timezone, confirmed=confirmed)

    def _tail_context(self, stream):
        if stream.tail is None:
            return None
        payload = self.queue.aggregation_context(stream.tail)
        return decode_aggregation(payload) if payload else None

    def _existing_tail(self, stream):
        return stream.tail if stream.tail is not None and self.queue.contains(stream.tail) else None

    def close(self, key, reason: str):
        """End the current container for one source.

        Every caller states why (a ``CloseReason``): a silently closed container
        splits the run, and the observable blocks of two or three messages give
        no clue which rule did it.
        """
        stream = self.streams.get(tuple(key))
        if stream is None:
            logging.getLogger(__name__).debug("No live aggregation stream to close for %s (%s).", key, reason)
        else:
            stream.close()
            # One line a minute, with a tally: every container close splits a run,
            # and which rule did it is the only way to tell why. Unthrottled, this
            # would flood an active chat.
            self._close_counts[reason] = self._close_counts.get(reason, 0) + 1
            now = time.monotonic()
            if now - self._close_logged_at >= 60.0:
                self._close_logged_at = now
                logging.getLogger(__name__).warning(
                    "Live aggregation container closes in the last interval: %s (latest %s)",
                    dict(self._close_counts), reason,
                )
                self._close_counts.clear()

    def reopen(self, row):
        """Rejoin the text run a discarded media row had ended.

        An undeliverable media row is removed without ever reaching Telegram, so
        the boundary it created was phantom: restore the container it closed
        instead of starting a new one.
        """
        from .queued_log import decode_aggregation
        context = decode_aggregation(row.log_context) if row.log_context else None
        if not context:
            return
        stream = self.streams.get(tuple(context["key"]))
        if stream is None or stream.tail != row.id:
            # A later row already continued the run, or there is no stream.
            logging.getLogger(__name__).debug("Discarded queue row %s is not a live aggregation tail.", row.id)
            return
        predecessor = row.predecessor_id
        stream.tail = predecessor if predecessor and self.queue.contains(predecessor) else None
        stream.reopen()
        self.scheduler.wake_event.set()

    def close_destination(self, chat_id, topic):
        """New Telegram messages end every source stream in their destination."""
        with self.scheduler._lock:
            self.queue.close_aggregation_destination(chat_id, topic)
            for key in self.streams:
                if int(key[1]) == chat_id and key[2] == topic:
                    self.close(key, CloseReason.TELEGRAM_DESTINATION)
            self.scheduler.wake_event.set()

    def close_source_routes(self, key):
        """End former target chats/topics when normal source delivery changes route."""
        key = tuple(key)
        prior_tail = None
        for old_key, old_stream in self.streams.items():
            if old_key[0] == key[0] and old_key != key:
                prior_tail = self._existing_tail(old_stream) or prior_tail
                self.close(old_key, CloseReason.ROUTE_CHANGED)
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
                self.close(key, CloseReason.IDLE)
            stream.recent = [stamp for stamp in stream.recent if now - stamp < 3][-2:] + [now]
            stream.last_new = now
            context = self._tail_context(stream)
            if (context and context["kind"] == ContextKind.LOGICAL and context["generation"] == stream.generation
                    and not context.get("incoming_boundary")):
                members = context["members"] + [member]
                if self.fits(members):
                    context.update(members=members, recent=stream.recent, last_new=now)
                    if not context.get("waiting") and len(stream.recent) > 2:
                        context.update(waiting=True, due=context["members"][0]["received_time"].timestamp()
                                       + float(self.manager.channel.flag("text_aggregation_window_seconds")))
                    if self.queue.replace_logical_aggregation(stream.tail, encode_aggregation(context)):
                        self.pending_sources[identity] = stream.tail
                        self.queue.observe_metrics(lambda metrics: metrics.record_aggregation_source())
                        self.scheduler.wake_event.set()
                        return True
                # Pending capacity becomes a publication boundary.
                self.close(key, CloseReason.CAPACITY)
            predecessor = self._existing_tail(stream) or prior_tail
            inherit = bool(predecessor and context and context.get("generation") == stream.generation and not context.get("incoming_boundary"))
            # The window applies from the first member: otherwise the container is
            # dispatched before it can absorb the run, and every later member starts
            # a new container instead of joining this one.
            window = float(self.manager.channel.flag("text_aggregation_window_seconds"))
            # A member that is already late when it reaches the aggregator belongs
            # to a backfill: hold its container for the window so the rest of the
            # run can join it. Fresh traffic still publishes immediately.
            waiting = (time.time() - now) >= self.BACKFILL_LATENESS_SECONDS
            context = dict(format_version=1, kind=ContextKind.LOGICAL, key=key, members=[member],
                           base=stream.base, inherit=inherit, generation=stream.generation,
                           recent=stream.recent, last_new=now, waiting=waiting,
                           due=(now + window) if waiting else now,
                           silent=silent)
            request = QueueRequest("send_message", (), {"chat_id": key[1], "text": "", "_slave_id": key[0],
                                                         "_send_mode": "eventual"},
                                   encode_aggregation(context), predecessor_id=predecessor)
            row_id, _ = self.queue.enqueue_many([request], self.manager._queue_operation)
            stream.tail = row_id
            self.pending_sources[identity] = row_id
            self.queue.observe_metrics(lambda metrics: metrics.record_aggregation_source())
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
            self.close(key, CloseReason.INDEPENDENT_DELIVERY)
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
                context = dict(format_version=1, kind=ContextKind.BOUNDARY, key=key, generation=stream.generation,
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
                    if tuple(context["key"]) == key and context["kind"] == ContextKind.LOGICAL:
                        for member in context["members"]:
                            self.pending_sources[member_identity(member)] = row.id
                self.pending_sources.pop(member_identity(source["member"]), None)
                if split["tail"] == split["row_id"]:
                    self.streams[key].tail = max(row.id for row in self.queue.aggregation_rows() if tuple(decode_aggregation(row.log_context)["key"]) == key)
                else:
                    self.streams[key].tail = split["tail"]
                    tail = self._tail_context(self.streams[key])
                    if tail and tail["kind"] in _INDEPENDENT_KINDS:
                        self.close(key, CloseReason.split_tail(str(tail["kind"])))
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
        if context is None:
            return None
        failing = self.queue.failing_predecessor(row.id)
        if failing is not None:
            predecessor = decode_aggregation(failing[1]) if failing[1] else None
            if needs_delivered_predecessor(context, predecessor):
                return math.inf
        return context.get("due") if context["kind"] in {ContextKind.LOGICAL, ContextKind.MEMBER_UPDATE} else None

    def materialize(self, row, selection):
        context = decode_aggregation(row.log_context) if row.log_context else None
        independent = context is not None and context["kind"] in {ContextKind.SOURCE, ContextKind.REDIRECT}
        if independent and context["member"]["status"] == MemberStatus.REMOVED:
            member = context["member"]
            resolved = self.manager.channel.db.resolve_source_member(*member_identity(member),
                str(context["key"][1]), context["key"][2])
            if resolved and not resolved[0].aggregate:
                from .msg_type import TGMsgType
                log = resolved[0]
                operation, kwargs = self._removal_edit(log, member)
                message = member_message(member)
                message.type_telegram = TGMsgType(log.media_type)
                message.file_id, message.file_unique_id = log.file_id, log.file_unique_id
                message.mime, message.file_bot_id = log.mime, log.file_bot_id
                context.update(kind=ContextKind.SOURCE, old_container_id=None, frozen=True,
                               old_message_id=tuple(int(value) for value in log.master_msg_id.rsplit(".", 1)),
                               message=message)
                return self.queue.materialize_aggregation(row.id, operation, kwargs, encode_aggregation(context), selection)
            raise UnconfirmedContainerError(row.id, context["kind"], "materialize",
                                            "A removal edit requires its confirmed standalone message.")
        if independent and not context.get("frozen"):
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
                if log.aggregate:
                    context["kind"] = ContextKind.REDIRECT
                    context["old_container_id"] = log.master_msg_id
                else:
                    context["kind"] = ContextKind.SOURCE
                    context["old_container_id"] = None
                    context["old_message_id"] = tuple(int(value) for value in log.master_msg_id.rsplit(".", 1))
                    if operation == "send_message" and log.media_type == "Text":
                        operation = "edit_message_text"
                        kwargs = {name: value for name, value in kwargs.items()
                                  if name in {"chat_id", "text", "parse_mode", "reply_markup", "link_preview_options"}}
                        kwargs["message_id"] = int((log.master_msg_id_alt or log.master_msg_id).rsplit(".", 1)[1])
            context["frozen"] = True
            return self.queue.materialize_aggregation(row.id, operation, kwargs, encode_aggregation(context), selection)
        if context is None or context["kind"] not in {ContextKind.LOGICAL, ContextKind.MEMBER_UPDATE}:
            # Frozen requests are sent as stored.
            return row
        if context["kind"] == ContextKind.MEMBER_UPDATE:
            if not context.get("container_id"):
                member = context["updates"][0]
                resolved = self.manager.channel.db.resolve_source_member(*member_identity(member), str(context["key"][1]), context["key"][2])
                if not resolved or not resolved[0].aggregate:
                    raise UnconfirmedContainerError(
                        row.id, context["kind"], "materialize",
                        "Deferred source update requires its confirmed container.")
                context["container_id"] = resolved[0].master_msg_id
            log = self.manager.channel.db.get_msg_log(master_msg_id=context["container_id"])
            children = copy.deepcopy(log.aggregate["children"])
            updates = {member_identity(member): member for member in context["updates"]}
            for index, previous in enumerate(children):
                update = updates.get(member_identity(previous))
                if update and update["source_revision"] > previous["source_revision"]:
                    children[index] = apply_member_event(previous, MemberEvent.RECEIPT, update).member
            if not self.fits(children, updating=True, confirmed=log.aggregate):
                raise IllegalAggregationTransitionError(
                    row.id, context["kind"], "materialize",
                    "Source changes require independent output before container update.")
            base = dict(message_id=int(log.master_msg_id.rsplit(".", 1)[1]), owner=log.sender_bot_id,
                        aggregate=log.aggregate)
            members = []
        else:
            base = context.get("base")
            members = context["members"]
            children = None
        owner = base and base["owner"]
        can_edit = (base is not None and owner == selection.sender_bot_id
                    and (context["kind"] == ContextKind.MEMBER_UPDATE
                         or self.fits(base["aggregate"]["children"] + members, confirmed=base["aggregate"])))
        if context["kind"] == ContextKind.MEMBER_UPDATE and not can_edit:
            raise IllegalAggregationTransitionError(row.id, context["kind"], "materialize",
                                                    "Existing aggregate updates require the original owner.")
        if children is None:
            children = base["aggregate"]["children"] + members if can_edit else members
        revision = base["aggregate"]["revision"] + 1 if can_edit else 1
        confirmed = base["aggregate"] if can_edit else None
        aggregate = make_aggregate(children, revision, admin_id=self.admin_id, display_timezone=self.display_timezone,
                                   confirmed=confirmed)
        # The emoji lookup reads only memory; an author whose emoji is not ready keeps the plain header.
        author_emoji = (self.manager.channel.custom_emoji.lookup
                        if self.manager._sender_can_send_custom_emoji(selection) else None)
        kwargs = dict(chat_id=context["key"][1], text=render_members(children, admin_id=self.admin_id, display_timezone=self.display_timezone,
                                                                 confirmed=confirmed, author_emoji=author_emoji).html,
                      parse_mode="HTML", disable_notification=context["silent"], _live_aggregate=True)
        operation = "send_message"
        if can_edit:
            operation = "edit_message_text"
            kwargs.pop("disable_notification")
            kwargs["message_id"] = base["message_id"]
        elif context["key"][2] is not None:
            kwargs["message_thread_id"] = int(context["key"][2])
        frozen = dict(format_version=1, kind=ContextKind.AGGREGATE, key=context["key"], aggregate=aggregate,
                      generation=context["generation"], recent=context["recent"], last_new=context["last_new"],
                      retry_context=copy.deepcopy(context) if context["kind"] == ContextKind.LOGICAL else None,
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
        state = check_receipt(row.id, context)
        if state is ContextState.BOUNDARY:
            raise IllegalAggregationTransitionError(row.id, context["kind"], "receipt",
                                                    "A boundary row is reconciled through its own MsgLog context.")
        if context["kind"] in {ContextKind.SOURCE, ContextKind.REDIRECT}:
            return self._reconcile_source(row, receipt, sender_bot_id, context, file_bot_id)
        saved = self.manager.channel.db.finalize_aggregate_message(receipt, context["aggregate"], sender_bot_id)
        base = dict(message_id=receipt.message_id, owner=sender_bot_id, aggregate=saved.aggregate)

        def update_successor(payload):
            successor = decode_aggregation(payload)
            if successor.get("inherit") and not successor.get("incoming_boundary") and tuple(successor["key"]) == tuple(context["key"]):
                successor["base"] = base
                successor["inherit"] = False
            if successor["kind"] == ContextKind.MEMBER_UPDATE and not successor.get("container_id"):
                successor["container_id"] = saved.master_msg_id
                successor["required_sender"] = sender_bot_id or "__main__"
            return encode_aggregation(successor)

        current_payload = self.queue.aggregation_context(row.id)
        current = decode_aggregation(current_payload) if current_payload else None
        if current and current.get("incoming_boundary"):
            context.update(appendable=False, incoming_boundary=True)
        context["handoff"] = True
        _, changed = self.queue.handoff_aggregation(row.id, encode_aggregation(context), update_successor)
        if changed:
            if row.operation == "send_message":
                self.queue.observe_metrics(lambda metrics: metrics.record_aggregation_container())
            members = context.get("members", [])
            if members:
                self.queue.observe_metrics(lambda metrics: metrics.record_aggregation_batch(len(members)))
                confirmed_at = time.time()
                for member in members:
                    self.queue.observe_metrics(lambda metrics: metrics.record_aggregation_confirmation(
                        max(0.0, confirmed_at - member["received_time"].timestamp())))
            self.queue.observe_metrics(lambda metrics: metrics.record_aggregation_payload(
                len(pickle.dumps(saved.aggregate["children"], protocol=5))))
        stream = self.streams.get(tuple(context["key"]))
        if stream and context.get("appendable", True) and stream.generation == context["generation"]:
            stream.base = base
        # A confirmed container update no longer orders later updates of that container.
        if context.get("container_id") and self.container_tails.get(context["container_id"]) == row.id:
            del self.container_tails[context["container_id"]]
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
        elif member["status"] != MemberStatus.REMOVED:
            message.type_telegram = get_msg_type(receipt)
            message.put_telegram_file(receipt)
        message.sender_bot_id = sender_bot_id
        if member["status"] != MemberStatus.REMOVED:
            message.file_bot_id = file_bot_id or sender_bot_id
        member = copy.deepcopy(member)
        # Telegram file metadata belongs to this output; the complete source stays in its member snapshot.
        source = member_message(member)
        source.file_id, source.file_unique_id = message.file_id, message.file_unique_id
        source.type_telegram, source.mime = message.type_telegram, message.mime
        source.file_bot_id = file_bot_id or sender_bot_id
        from .queued_log import encode
        if member["status"] != MemberStatus.REMOVED:
            member["snapshot"] = encode(source, None)
        db = self.manager.channel.db
        if context["kind"] == ContextKind.REDIRECT:
            saved = db.finalize_member_redirect(context["old_container_id"], member, message, receipt, sender_bot_id)
        else:
            saved = db.finalize_source_message(message, receipt, member, sender_bot_id,
                                               old_message_id=context.get("old_message_id"))
        requests = []
        if context["kind"] == ContextKind.REDIRECT:
            old = db.get_msg_log(master_msg_id=context["old_container_id"])
            redirected = next(child for child in old.aggregate["children"] if member_identity(child) == member_identity(member))
            chat_id, message_id = old.master_msg_id.rsplit(".", 1)
            hint = dict(format_version=1, kind=ContextKind.MEMBER_UPDATE, key=context["key"], container_id=old.master_msg_id,
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
        identifiers, _ = self.queue.handoff_aggregation(row.id, encode_aggregation(context), update_successor,
                requests=requests, operation_resolver=self.manager._queue_operation)
        if identifiers:
            self.container_tails[context["old_container_id"]] = identifiers[-1]
        if message.commands and member["status"] == MemberStatus.ACTIVE:
            from ehforwarderbot import coordinator
            from .commands import ETMCommandMsgStorage
            module = coordinator.get_module_by_id(message.author.module_id)
            self.manager.channel.commands.register_command(receipt,
                ETMCommandMsgStorage(message.commands, module, member.get("display_prefix") or "", message.text))
        return True

    @staticmethod
    def _removal_edit(log, member):
        from ehforwarderbot.message import LinkAttribute
        source = member_message(member)
        prefix = member.get("display_prefix") or ""
        if prefix and not prefix.endswith(":"):
            prefix += ":"
        operation, kwargs = removal_edit(log.media_type, source.text or "", prefix=prefix,
            link=source.attributes if isinstance(source.attributes, LinkAttribute) else None)
        chat_id, message_id = (log.master_msg_id_alt or log.master_msg_id).rsplit(".", 1)
        kwargs.update(chat_id=int(chat_id), message_id=int(message_id))
        return operation, kwargs

    def removal_failed(self, row, sender_bot_id):
        """Keep an uneditable source output and record its removal with a notice."""
        context = decode_aggregation(row.log_context) if row.log_context else None
        if not context or context["kind"] != ContextKind.SOURCE or context["member"]["status"] != MemberStatus.REMOVED:
            return
        _, kwargs = self.queue.decode_payload_raw(row.payload)
        self.manager._enqueue_requests([QueueRequest("send_message", (), dict(
            chat_id=kwargs["chat_id"], text=self.manager._("Message is removed in remote chat."),
            reply_to_message_id=kwargs["message_id"], disable_notification=True, _send_mode="eventual"))])
        self._reconcile_source(row, True, sender_bot_id, context, context["message"].file_bot_id)

    def remove_source_member(self, key, identity):
        """Apply a confirmed source removal to one member, retaining its full saved source."""
        with self.scheduler._lock:
            log, previous, predecessor = self.source_state(key, identity)
            if previous is None:
                return False
            change = apply_member_event(previous, MemberEvent.REMOVE)
            if not change.changed:
                return True
            member = change.member
            if self.update_pending_member(key, member):
                return True
            payload = self.queue.aggregation_context(predecessor) if predecessor else None
            pending = decode_aggregation(payload) if payload else None
            independent_pending = pending and pending["kind"] in {ContextKind.SOURCE, ContextKind.REDIRECT}
            if independent_pending and log is None:
                cancelled = self.queue.cancel_pending_source(tuple(key), member)
                if cancelled:
                    for row_id in cancelled:
                        # A withdrawn output finishes its source's delivery, as a discarded one does.
                        self.manager._run_database_update_callback(
                            self.manager._pop_queued_completion_callback(row_id))
                    for stream in self.streams.values():
                        if stream.tail in cancelled:
                            stream.tail = cancelled[stream.tail]
                    self.scheduler.wake_event.set()
                    return True
            if independent_pending:
                context = dict(format_version=1, kind=ContextKind.SOURCE, key=tuple(key), member=member,
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
                context = dict(format_version=1, kind=ContextKind.SOURCE, key=tuple(key), member=member,
                               message=member_message(member), old_message_id=old_id, old_container_id=None,
                               generation=-1, members=[], handoff=False, frozen=True)
                operation, kwargs = self._removal_edit(log, member)
                kwargs["_required_sender_bot_id"] = log.sender_bot_id or "__main__"
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
            if log is not None and log.aggregate is None and context["kind"] in {
                    ContextKind.AGGREGATE, ContextKind.MEMBER_UPDATE, ContextKind.LOGICAL}:
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
            elif context["kind"] == ContextKind.LOGICAL:
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
            context = dict(format_version=1, kind=ContextKind.MEMBER_UPDATE, key=tuple(key), container_id=None,
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
            context = dict(format_version=1, kind=ContextKind.SOURCE, key=tuple(key), member=member,
                           old_container_id=old_container_id, old_message_id=old_message_id,
                           generation=self.streams[tuple(key)].generation, members=[], handoff=False,
                           recent=self.streams[tuple(key)].recent, last_new=self.streams[tuple(key)].last_new)
            pending_id = self.pending_sources.get(member_identity(member))
            payload = self.queue.aggregation_context(pending_id) if pending_id else None
            pending = decode_aggregation(payload) if payload else None
            if pending and pending["kind"] == ContextKind.LOGICAL and tuple(pending["key"]) == tuple(key):
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
            if context["kind"] != ContextKind.LOGICAL or tuple(context["key"]) != tuple(key):
                return False
            for position, previous in enumerate(context["members"]):
                if member_identity(previous) == member_identity(member):
                    if member["source_revision"] <= previous["source_revision"]:
                        return True
                    if member["status"] == MemberStatus.REMOVED:
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
                        cancelled_member=member if member["status"] == MemberStatus.REMOVED else None)
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
                if context and context["kind"] == ContextKind.MEMBER_UPDATE and context["container_id"] == container_id:
                    merged = {member_identity(member): member for member in context["updates"]}
                    for member in updates:
                        previous = merged.get(member_identity(member))
                        if previous is None or member["source_revision"] > previous["source_revision"]:
                            merged[member_identity(member)] = member
                    context["updates"] = list(merged.values())
                    if self.queue.replace_logical_aggregation(predecessor, encode_aggregation(context)):
                        self.scheduler.wake_event.set()
                        return predecessor
            context = dict(format_version=1, kind=ContextKind.MEMBER_UPDATE, key=key, container_id=container_id,
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
