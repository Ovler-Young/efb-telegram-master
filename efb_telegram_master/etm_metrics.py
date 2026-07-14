# coding=utf-8
"""Prometheus instrumentation for the ETM outbound send pipeline."""

from __future__ import annotations

import collections
import datetime
import re
import threading
import time
from typing import Callable, Iterable, Optional

import telegram.error
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily


_SEND_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)
_WAIT_BUCKETS = (0.0, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 15, 30, 60, 120, 300)
_LIFETIME_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)


def metrics_method_name(function: Callable) -> str:
    name = getattr(function, "__name__", "") or type(function).__name__
    name = re.sub(r"[^A-Za-z0-9_:.]", "_", str(name))
    return (name or "unknown")[:80]


def telegram_error_type(error: Exception) -> str:
    if isinstance(error, telegram.error.RetryAfter):
        return "retry_after"
    if isinstance(error, telegram.error.BadRequest):
        return "bad_request"
    if isinstance(error, telegram.error.TimedOut):
        return "timed_out"
    if isinstance(error, telegram.error.NetworkError):
        return "network"
    if isinstance(error, telegram.error.Forbidden):
        return "forbidden"
    return "other"


def bad_request_reason_class(error: Exception) -> str:
    message = f"{getattr(error, 'message', '')} {error}".lower()
    if re.search(r"message (?:is )?too long|text (?:is )?too long|caption (?:is )?too long", message):
        return "message_too_long"
    if re.search(r"chat not found|chat_id|invalid chat|group chat was upgraded", message):
        return "invalid_chat"
    if re.search(r"reply|replied|message to reply|quote", message):
        return "reply_target_missing"
    if re.search(r"markup|keyboard|button|entities|parse|entity", message):
        return "invalid_markup"
    if re.search(r"media|photo|video|document|animation|audio|voice|sticker|file", message):
        return "media_invalid"
    if re.search(r"thread|topic", message):
        return "thread_invalid"
    return "unknown"


class _TopNQueueCollector:
    """Expose the N deepest current source lanes without remembering labels."""

    def __init__(
        self,
        namespace: str,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, int]]],
        top_n: int = 20,
    ):
        self._namespace = namespace
        self._snapshot_fn = snapshot_fn
        self._top_n = max(0, int(top_n))

    def collect(self):
        family = GaugeMetricFamily(
            f"{self._namespace}_send_queue_target_depth",
            "Current durable backlog within the deepest source submission lanes.",
            labels=["source_key", "chat_id", "priority"],
        )
        try:
            rows = [
                (source_key, chat_id, priority, depth)
                for source_key, chat_id, priority, depth in self._snapshot_fn()
                if depth > 0
            ]
        except Exception:
            yield family
            return

        rows.sort(key=lambda row: row[3], reverse=True)
        for source_key, chat_id, priority, depth in rows[: self._top_n]:
            family.add_metric([str(source_key), str(chat_id), str(priority)], depth)
        yield family


class _TopNQueueAgeCollector:
    """Expose the N oldest current source lanes without remembering labels."""

    def __init__(
        self,
        namespace: str,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, float]]],
        top_n: int = 20,
    ):
        self._namespace = namespace
        self._snapshot_fn = snapshot_fn
        self._top_n = max(0, int(top_n))

    def collect(self):
        family = GaugeMetricFamily(
            f"{self._namespace}_send_queue_target_oldest_age_seconds",
            "Oldest queued task age by source and Telegram chat.",
            labels=["source_key", "chat_id", "priority"],
        )
        try:
            rows = [
                (source_key, chat_id, priority, age)
                for source_key, chat_id, priority, age in self._snapshot_fn()
                if age > 0
            ]
        except Exception:
            yield family
            return

        rows.sort(key=lambda row: row[3], reverse=True)
        for source_key, chat_id, priority, age in rows[: self._top_n]:
            family.add_metric([str(source_key), str(chat_id), str(priority)], age)
        yield family


class _BotChatOccupancyCollector:
    """Expose bot-level chat counts grouped by per-chat rate-limit occupancy."""

    def __init__(
        self,
        namespace: str,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, int, int, str, int]]],
    ):
        self._namespace = namespace
        self._snapshot_fn = snapshot_fn

    def collect(self):
        family = GaugeMetricFamily(
            f"{self._namespace}_bot_chat_rate_limit_occupancy_chats",
            "Known chats per bot grouped by current per-chat rate-limit occupancy.",
            labels=["sender", "bot_id", "username", "used", "limit", "state"],
        )
        try:
            rows = list(self._snapshot_fn())
        except Exception:
            yield family
            return

        for sender, bot_id, username, used, limit, state, chat_count in rows:
            if chat_count <= 0:
                continue
            family.add_metric(
                [str(sender), str(bot_id), str(username), str(used), str(limit), str(state)],
                chat_count,
            )
        yield family


class _BotCooldownCollector:
    """Expose current Telegram cooldowns grouped per bot."""

    def __init__(
        self,
        namespace: str,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, int, float]]],
    ):
        self._namespace = namespace
        self._snapshot_fn = snapshot_fn

    def collect(self):
        chats_family = GaugeMetricFamily(
            f"{self._namespace}_bot_chat_cooldown_chats",
            "Chat count currently frozen by Telegram RetryAfter per bot.",
            labels=["sender", "bot_id", "username"],
        )
        max_family = GaugeMetricFamily(
            f"{self._namespace}_bot_chat_cooldown_max_seconds",
            "Longest remaining Telegram RetryAfter cooldown per bot.",
            labels=["sender", "bot_id", "username"],
        )
        try:
            rows = list(self._snapshot_fn())
        except Exception:
            yield chats_family
            yield max_family
            return

        for sender, bot_id, username, chat_count, max_seconds in rows:
            if chat_count <= 0:
                continue
            labels = [str(sender), str(bot_id), str(username)]
            chats_family.add_metric(labels, chat_count)
            max_family.add_metric(labels, max(0.0, float(max_seconds)))
        yield chats_family
        yield max_family


class _MembershipCacheCollector:
    """Expose auxiliary bot membership cache counts."""

    def __init__(
        self,
        namespace: str,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, str, int]]],
    ):
        self._namespace = namespace
        self._snapshot_fn = snapshot_fn

    def collect(self):
        family = GaugeMetricFamily(
            f"{self._namespace}_membership_cache_chats",
            "Auxiliary bot membership cache chat counts.",
            labels=["bot_id", "username", "status"],
        )
        try:
            rows = list(self._snapshot_fn())
        except Exception:
            yield family
            return

        for bot_id, username, status, chat_count in rows:
            if chat_count <= 0:
                continue
            family.add_metric([str(bot_id), str(username), str(status)], chat_count)
        yield family


class _ReservedSlotsCollector:
    """Expose current sliding-window slot reservations per bot."""

    def __init__(
        self,
        namespace: str,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, int]]],
    ):
        self._namespace = namespace
        self._snapshot_fn = snapshot_fn

    def collect(self):
        family = GaugeMetricFamily(
            f"{self._namespace}_reserved_slots",
            "Current global sliding-window send slot reservations per bot.",
            labels=["sender", "bot_id", "username"],
        )
        try:
            rows = list(self._snapshot_fn())
        except Exception:
            yield family
            return

        for sender, bot_id, username, slot_count in rows:
            family.add_metric([str(sender), str(bot_id), str(username)], max(0, int(slot_count)))
        yield family


class _ManagerStateExporter:
    """Build dynamic metrics rows from TelegramBotManager-owned runtime state."""

    def __init__(self, manager):
        self.manager = manager

    def queue_depth_rows(self):
        from .db import OutboundTask
        from .outbound import TaskState

        depths: collections.Counter[tuple[str, int, str]] = collections.Counter(
            (task.source_key, task.target_chat_id, "blocking" if task.priority else "normal")
            for task in OutboundTask.select().where(OutboundTask.state.in_(TaskState.UNSUBMITTED))
        )
        return [
            (source_key, chat_id, priority, depth)
            for (source_key, chat_id, priority), depth in depths.items()
        ]

    def queue_oldest_age_rows(self):
        from .db import OutboundTask
        from .outbound import TaskState, utc_now

        now = utc_now()
        oldest_by_source: dict[tuple[str, int, str], datetime.datetime] = {}
        for task in OutboundTask.select().where(OutboundTask.state.in_(TaskState.UNSUBMITTED)):
            if task.accepted_at is None:
                continue
            key = (
                task.source_key,
                task.target_chat_id,
                "blocking" if task.priority else "normal",
            )
            oldest_by_source[key] = min(
                task.accepted_at,
                oldest_by_source.get(key, task.accepted_at),
            )
        return [
            (source_key, chat_id, priority, max(0.0, (now - accepted_at).total_seconds()))
            for (source_key, chat_id, priority), accepted_at in oldest_by_source.items()
        ]

    def queue_summary(self):
        from .db import OutboundTask
        from .outbound import TaskState, utc_now

        now = utc_now()
        rows = list(OutboundTask.select().where(OutboundTask.state.in_(TaskState.UNSUBMITTED)))
        depths_by_source: collections.Counter[tuple[str, bool]] = collections.Counter(
            (task.source_key, task.priority) for task in rows
        )
        depths = list(depths_by_source.values())
        oldest_ages = [
            max(0.0, (now - task.accepted_at).total_seconds())
            for task in rows if task.accepted_at is not None
        ]
        retry_targets = len({
            (task.source_key, task.priority) for task in rows
            if task.available_at is not None and task.available_at > now
        })

        return {
            "queued_tasks": sum(depths),
            "queued_targets": len(depths),
            "max_target_depth": max(depths) if depths else 0,
            "queue_oldest_age": max(oldest_ages) if oldest_ages else 0.0,
            "retry_targets": retry_targets,
        }

    def bot_identity(self, sender_bot_id: Optional[str]) -> tuple[str, object, str]:
        if sender_bot_id is None:
            me = getattr(self.manager, 'me', None)
            return "main", getattr(me, 'id', 'main'), getattr(me, 'username', '') or ''

        bot_pool = getattr(self.manager, 'bot_pool', None)
        aux_bot = bot_pool.get_bot_by_id(sender_bot_id) if bot_pool else None
        if aux_bot is not None:
            return "aux", aux_bot.bot_id, aux_bot.username
        return "aux", sender_bot_id, ""

    @staticmethod
    def append_bot_chat_occupancy_rows(
        rows: list[tuple[str, object, str, int, int, str, int]],
        *,
        sender: str,
        bot_id: object,
        username: str,
        chat_counts: dict[int, int],
        known_chat_ids: Iterable[int],
        effective_limit: int,
    ) -> None:
        distribution: collections.Counter[tuple[int, str]] = collections.Counter()
        for chat_id in set(known_chat_ids) | set(chat_counts):
            used = int(chat_counts.get(chat_id, 0))
            state = "cooling" if effective_limit > 0 and used >= effective_limit else "available"
            distribution[(used, state)] += 1

        for (used, state), chat_count in sorted(distribution.items()):
            rows.append((sender, bot_id, username, used, effective_limit, state, chat_count))

    def bot_chat_occupancy_rows(self):
        rows: list[tuple[str, object, str, int, int, str, int]] = []

        main_counts, main_limit = self.manager._rate_limiter.get_chat_count_snapshot()
        _sender, bot_id, username = self.bot_identity(None)
        self.append_bot_chat_occupancy_rows(
            rows,
            sender="main",
            bot_id=bot_id,
            username=username,
            chat_counts=main_counts,
            known_chat_ids=(),
            effective_limit=main_limit,
        )

        for aux_bot in (self.manager.bot_pool.bots if self.manager.bot_pool else []):
            chat_counts, effective_limit = aux_bot.get_chat_count_snapshot()
            self.append_bot_chat_occupancy_rows(
                rows,
                sender="aux",
                bot_id=aux_bot.bot_id,
                username=aux_bot.username,
                chat_counts=chat_counts,
                known_chat_ids=aux_bot.get_known_member_chat_ids(),
                effective_limit=effective_limit,
            )

        return rows

    def bot_chat_cooldown_rows(self):
        now = time.time()
        cooldowns: dict[Optional[str], list[float]] = collections.defaultdict(list)
        for (sender_bot_id, _chat_id), deadline in list(self.manager._bot_chat_disabled_until.items()):
            remaining = float(deadline) - now
            if remaining > 0:
                cooldowns[sender_bot_id].append(remaining)

        rows = []
        for sender_bot_id, remaining_values in cooldowns.items():
            sender, bot_id, username = self.bot_identity(sender_bot_id)
            rows.append((sender, bot_id, username, len(remaining_values), max(remaining_values)))
        return rows

    def membership_cache_rows(self):
        rows = []
        for aux_bot in (self.manager.bot_pool.bots if self.manager.bot_pool else []):
            for status, chat_count in aux_bot.get_membership_cache_snapshot().items():
                rows.append((aux_bot.bot_id, aux_bot.username, status, chat_count))
        return rows

    def reserved_slots_rows(self):
        rows = []
        sender, bot_id, username = self.bot_identity(None)
        rows.append((sender, bot_id, username, self.manager._rate_limiter.get_reserved_slot_count()))

        for aux_bot in (self.manager.bot_pool.bots if self.manager.bot_pool else []):
            rows.append(("aux", aux_bot.bot_id, aux_bot.username, aux_bot.get_reserved_slot_count()))
        return rows


class Metrics:
    """All ETM send-pipeline metrics, bound to a private registry."""

    def __init__(self, namespace: str = "etm", top_n: int = 20):
        self.namespace = namespace
        self.top_n = top_n
        self.registry = CollectorRegistry()
        self._manager_state: Optional[_ManagerStateExporter] = None
        ns = namespace

        self.enqueued = Counter(
            f"{ns}_send_tasks_enqueued_total",
            "Tasks accepted into durable source submission lanes.",
            ["priority"],
            registry=self.registry,
        )
        self.dispatched = Counter(
            f"{ns}_send_tasks_dispatched_total",
            "Tasks handed to the send thread pool.",
            ["sender"],
            registry=self.registry,
        )
        self.completed = Counter(
            f"{ns}_send_tasks_completed_total",
            "Tasks that reached a terminal outcome.",
            ["sender", "outcome"],
            registry=self.registry,
        )
        self.requeued = Counter(
            f"{ns}_send_tasks_requeued_total",
            "Tasks returned to their durable source lane.",
            ["reason"],
            registry=self.registry,
        )
        self.dropped = Counter(
            f"{ns}_send_tasks_dropped_total",
            "Tasks abandoned without delivery.",
            ["reason"],
            registry=self.registry,
        )
        self.rate_limit_hits = Counter(
            f"{ns}_telegram_rate_limit_hits_total",
            "RetryAfter or 429 responses from Telegram.",
            ["sender"],
            registry=self.registry,
        )
        self.send_failures_c = Counter(
            f"{ns}_telegram_send_failures_total",
            "Telegram send call failures by bounded error class and method.",
            ["sender", "error_type", "method"],
            registry=self.registry,
        )
        self.bad_requests_c = Counter(
            f"{ns}_bad_request_total",
            "Telegram BadRequest failures by bounded reason class and method.",
            ["method", "reason_class"],
            registry=self.registry,
        )
        self.membership_probe_c = Counter(
            f"{ns}_membership_probe_total",
            "Auxiliary bot membership probe outcomes.",
            ["bot_id", "username", "outcome"],
            registry=self.registry,
        )
        self.worker_loops = Counter(
            f"{ns}_send_worker_loops_total",
            "Iterations of the queued send worker loop.",
            registry=self.registry,
        )
        self.worker_loop_errors = Counter(
            f"{ns}_send_worker_loop_errors_total",
            "Uncaught exceptions in the queued send worker loop.",
            registry=self.registry,
        )

        self.queue_wait = Histogram(
            f"{ns}_send_queue_wait_seconds",
            "Time from enqueue to dispatch.",
            buckets=_WAIT_BUCKETS,
            registry=self.registry,
        )
        self.send_latency = Histogram(
            f"{ns}_telegram_send_seconds",
            "Duration of a successful Telegram send call.",
            ["sender"],
            buckets=_SEND_BUCKETS,
            registry=self.registry,
        )
        self.task_lifetime = Histogram(
            f"{ns}_send_task_lifetime_seconds",
            "Time from enqueue to successful terminal outcome.",
            buckets=_LIFETIME_BUCKETS,
            registry=self.registry,
        )

        self.queued_tasks_g = Gauge(
            f"{ns}_send_queue_depth",
            "Total tasks awaiting submission across durable source lanes.",
            registry=self.registry,
        )
        self.queued_targets_g = Gauge(
            f"{ns}_send_queue_targets",
            "Number of distinct source lanes with a backlog.",
            registry=self.registry,
        )
        self.max_target_depth_g = Gauge(
            f"{ns}_send_queue_max_target_depth",
            "Deepest durable source submission lane.",
            registry=self.registry,
        )
        self.queue_oldest_age_g = Gauge(
            f"{ns}_send_queue_oldest_age_seconds",
            "Age of the oldest currently queued send task.",
            registry=self.registry,
        )
        self.in_flight_g = Gauge(
            f"{ns}_send_in_flight",
            "Sends currently executing in the thread pool.",
            registry=self.registry,
        )
        self.disabled_bot_chats_g = Gauge(
            f"{ns}_disabled_bot_chats",
            "Bot/chat pairs currently frozen by Telegram RetryAfter.",
            registry=self.registry,
        )
        self.retry_targets_g = Gauge(
            f"{ns}_retry_targets",
            "Source lanes waiting for their next scheduling attempt.",
            registry=self.registry,
        )
        self.worker_alive_g = Gauge(
            f"{ns}_send_worker_alive",
            "1 if the queued send worker is alive, else 0.",
            registry=self.registry,
        )
        self.last_loop_ts_g = Gauge(
            f"{ns}_send_worker_last_loop_timestamp_seconds",
            "Unix time of the worker's last loop tick.",
            registry=self.registry,
        )
        self.aux_pool_size_g = Gauge(
            f"{ns}_aux_bot_pool_size",
            "Number of auxiliary bots in the pool.",
            registry=self.registry,
        )
        self.aux_disabled_g = Gauge(
            f"{ns}_aux_bots_disabled",
            "Auxiliary bots currently disabled.",
            registry=self.registry,
        )
        self.recovery_c = Counter(
            f"{ns}_outbound_recovery_total",
            "Durable outbound rows handled during process recovery.",
            ["outcome"],
            registry=self.registry,
        )
        self.waiter_timeout_c = Counter(
            f"{ns}_outbound_waiter_timeouts_total",
            "Same-process blocking callers that stopped waiting for durable work.",
            ["operation"],
            registry=self.registry,
        )
        self.workflow_terminal_c = Counter(
            f"{ns}_outbound_workflows_terminal_total",
            "Durable outbound workflows reaching a terminal state.",
            ["state"],
            registry=self.registry,
        )
        self.lease_heartbeat_c = Counter(
            f"{ns}_outbound_lease_heartbeats_total",
            "Live in-flight task leases extended by the worker.",
            registry=self.registry,
        )

    def register_topn(
        self,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, int]]],
    ) -> None:
        self.registry.register(_TopNQueueCollector(self.namespace, snapshot_fn, self.top_n))

    def register_queue_oldest_age_topn(
        self,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, float]]],
    ) -> None:
        self.registry.register(_TopNQueueAgeCollector(self.namespace, snapshot_fn, self.top_n))

    def register_bot_chat_occupancy(
        self,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, int, int, str, int]]],
    ) -> None:
        self.registry.register(_BotChatOccupancyCollector(self.namespace, snapshot_fn))

    def register_bot_cooldowns(
        self,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, int, float]]],
    ) -> None:
        self.registry.register(_BotCooldownCollector(self.namespace, snapshot_fn))

    def register_membership_cache(
        self,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, str, int]]],
    ) -> None:
        self.registry.register(_MembershipCacheCollector(self.namespace, snapshot_fn))

    def register_reserved_slots(
        self,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, object, int]]],
    ) -> None:
        self.registry.register(_ReservedSlotsCollector(self.namespace, snapshot_fn))

    def register_manager_state(self, manager) -> None:
        state = _ManagerStateExporter(manager)
        self._manager_state = state

        for aux_bot in (manager.bot_pool.bots if manager.bot_pool else []):
            if hasattr(aux_bot, "bind_metrics"):
                aux_bot.bind_metrics(self)

        self.register_topn(state.queue_depth_rows)
        self.register_queue_oldest_age_topn(state.queue_oldest_age_rows)
        self.register_bot_chat_occupancy(state.bot_chat_occupancy_rows)
        self.register_bot_cooldowns(state.bot_chat_cooldown_rows)
        self.register_membership_cache(state.membership_cache_rows)
        self.register_reserved_slots(state.reserved_slots_rows)

    def task_enqueued(self, priority: bool) -> None:
        self.enqueued.labels(priority="blocking" if priority else "normal").inc()

    def task_dispatched(self, sender: str) -> None:
        self.dispatched.labels(sender=sender).inc()

    def recovered(self, outcome: str, count: int = 1) -> None:
        if count > 0:
            self.recovery_c.labels(outcome=outcome).inc(count)

    def waiter_timed_out(self, operation: str) -> None:
        self.waiter_timeout_c.labels(operation=operation).inc()

    def workflow_terminal(self, state: str) -> None:
        self.workflow_terminal_c.labels(state=state).inc()

    def lease_heartbeat(self, count: int) -> None:
        if count > 0:
            self.lease_heartbeat_c.inc(count)

    def observe_queue_wait(self, seconds: float) -> None:
        if seconds >= 0:
            self.queue_wait.observe(seconds)

    def observe_send_latency(self, sender: str, seconds: float) -> None:
        if seconds >= 0:
            self.send_latency.labels(sender=sender).observe(seconds)

    def send_completed(
        self,
        sender: str,
        outcome: str,
        total_seconds: Optional[float] = None,
    ) -> None:
        self.completed.labels(sender=sender, outcome=outcome).inc()
        if outcome == "ok" and total_seconds is not None and total_seconds >= 0:
            self.task_lifetime.observe(total_seconds)

    def task_requeued(self, reason: str) -> None:
        self.requeued.labels(reason=reason).inc()

    def task_dropped(self, reason: str) -> None:
        self.dropped.labels(reason=reason).inc()

    def rate_limited(self, sender: str) -> None:
        self.rate_limit_hits.labels(sender=sender).inc()

    def send_failure(self, sender: str, error_type: str, method: str) -> None:
        self.send_failures_c.labels(sender=sender, error_type=error_type, method=method).inc()

    def send_failure_from_exception(self, sender: str, function: Callable, error: Exception) -> None:
        self.send_failure(sender, telegram_error_type(error), metrics_method_name(function))

    def bad_request(self, method: str, reason_class: str) -> None:
        self.bad_requests_c.labels(method=method, reason_class=reason_class).inc()

    def bad_request_from_exception(self, function: Callable, error: Exception) -> None:
        self.bad_request(metrics_method_name(function), bad_request_reason_class(error))

    def membership_probe(self, bot_id: object, username: str, outcome: str) -> None:
        self.membership_probe_c.labels(bot_id=str(bot_id), username=str(username), outcome=outcome).inc()

    def loop_tick(self) -> None:
        self.worker_loops.inc()
        self.last_loop_ts_g.set(time.time())

    def loop_error(self) -> None:
        self.worker_loop_errors.inc()

    def snapshot(
        self,
        *,
        queued_tasks: int,
        queued_targets: int,
        max_target_depth: int,
        queue_oldest_age: float,
        in_flight: int,
        disabled_bot_chats: int,
        retry_targets: int,
        worker_alive: bool,
        aux_pool_size: int = 0,
        aux_disabled: int = 0,
    ) -> None:
        self.queued_tasks_g.set(queued_tasks)
        self.queued_targets_g.set(queued_targets)
        self.max_target_depth_g.set(max_target_depth)
        self.queue_oldest_age_g.set(max(0.0, queue_oldest_age))
        self.in_flight_g.set(in_flight)
        self.disabled_bot_chats_g.set(disabled_bot_chats)
        self.retry_targets_g.set(retry_targets)
        self.worker_alive_g.set(1 if worker_alive else 0)
        self.aux_pool_size_g.set(aux_pool_size)
        self.aux_disabled_g.set(aux_disabled)

    def snapshot_manager_state(self, manager, *, worker_alive: bool) -> None:
        state = self._manager_state
        if state is None or state.manager is not manager:
            state = _ManagerStateExporter(manager)

        aux_bots = manager.bot_pool.bots if manager.bot_pool else []
        queue_summary = state.queue_summary()
        self.snapshot(
            queued_tasks=queue_summary["queued_tasks"],
            queued_targets=queue_summary["queued_targets"],
            max_target_depth=queue_summary["max_target_depth"],
            queue_oldest_age=queue_summary["queue_oldest_age"],
            in_flight=len(manager._outbound_scheduler.in_flight_snapshot()),
            disabled_bot_chats=len(manager._bot_chat_disabled_until),
            retry_targets=queue_summary["retry_targets"],
            worker_alive=worker_alive,
            aux_pool_size=len(aux_bots),
            aux_disabled=sum(1 for bot in aux_bots if getattr(bot, 'disabled', False)),
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)


def start_metrics_server(host: str, port: int, registry) -> object:
    """Start a daemon WSGI server exposing the registry."""
    from prometheus_client import make_wsgi_app
    from wsgiref.simple_server import WSGIRequestHandler, make_server

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *_args):
            pass

    httpd = make_server(host, port, make_wsgi_app(registry), handler_class=QuietHandler)
    threading.Thread(target=httpd.serve_forever, name="ETM metrics server", daemon=True).start()
    return httpd
