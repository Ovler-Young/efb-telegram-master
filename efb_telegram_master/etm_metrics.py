"""Bounded Prometheus instrumentation for ETM's outbound delivery model."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import Metric

from .outbound import OutboundQueue
from .runtime.metrics_process import register_bounded_gauge_collector, register_destination_queue_collector, register_process_collector, register_worker_collector
from .transport.telegram_calls import QUEUED_OPERATIONS

_PRIORITIES = frozenset({"blocking", "normal"})
_SENDER_KINDS = frozenset({"main", "auxiliary"})
_REMOVAL_OUTCOMES = frozenset({"submitted", "terminal_discard"})
_COMPLETION_OUTCOMES = frozenset({"success", "failure"})
_DISPATCH_OUTCOMES = frozenset({"submitted", "deferred", "failed"})
_RETRY_REASONS = frozenset({"rate_limit", "membership", "worker_capacity"})
_OUTBOUND_OUTCOMES = frozenset({"enqueued", "success", "failure", "attachment_failure", "cancelled", "rejected"})
_OUTBOUND_RETRY_REASONS = frozenset({"migration", "rate_limit", "transport"})
_OUTBOUND_SATURATION_REASONS = frozenset({"pending_capacity"})
_FAILURE_STAGES = frozenset({"dispatch", "execution", "terminal"})
_AUXILIARY_STATES = frozenset({"enabled", "disabled"})
_MEMBERSHIP_CACHE_STATES = frozenset({"member", "not_member", "unknown_probe_pending"})
_MEMBERSHIP_PROBE_OUTCOMES = frozenset({"ok_member", "ok_not_member", "forbidden", "bad_request", "timeout", "error", "queue_full", "stale"})
_RATE_LIMIT_SCOPES = frozenset({"global", "chat"})
_DATABASE_METHODS = frozenset(
    {
        "stop_worker",
        "add_chat_assoc",
        "remove_chat_assoc",
        "get_master_msg_id",
        "get_chat_assoc",
        "add_topic_assoc",
        "get_topic_thread_id",
        "get_topic_slave",
        "get_topic_slaves",
        "remove_topic_assoc",
        "add_or_update_message_log",
        "get_msg_log",
        "delete_msg_log",
        "get_slave_chat_info",
        "set_slave_chat_info",
        "delete_slave_chat_info",
        "get_recent_slave_chats",
        "get_last_message",
        "get_recent_messages",
        "replace_history_migration_entries",
        "has_pending_history_migrations",
        "get_next_history_migration_target",
        "get_history_migration_entry_page",
        "delete_history_migration_entry",
        "get_recent_msglog_page",
        "claim_slave_message_delivery",
        "complete_slave_message_delivery",
        "renew_slave_message_delivery",
        "release_slave_message_delivery",
        "get_resumable_msglog_ingestion_scans",
    }
)
_DATABASE_OUTCOMES = frozenset({"success", "failure"})


@dataclass(frozen=True)
class DestinationQueueSnapshot:
    """One destination's current queue state, supplied by a scrape callback."""

    destination: str
    depth: int
    oldest_age_seconds: float | None


@dataclass(frozen=True)
class WorkerSnapshot:
    """Aggregate liveness and work ownership supplied by a scrape callback."""

    healthy: bool
    in_flight: int


class _CallbackCollector:
    """Expose a bounded callback snapshot without retaining application objects."""

    def __init__(self, collect: Callable[[], Iterable[Metric]]) -> None:
        self._collect = collect

    def collect(self) -> Iterable[Metric]:
        return self._collect()


class Metrics:
    """Expose bounded queue lifecycle metrics and pull-based runtime snapshots."""

    def __init__(
        self,
        namespace: str = "etm",
        process_factory: Callable[[], Any] | None = None,
        network_io_counters: Callable[[], Any] | None = None,
    ) -> None:
        self.registry = CollectorRegistry()
        self.namespace = namespace
        self._collectors: list[_CallbackCollector] = []
        self._logged_collector_failures: set[str] = set()
        self.membership_probes = Counter(
            f"{namespace}_auxiliary_membership_probes_total",
            "Auxiliary membership probes by bounded outcome.",
            ["outcome"],
            registry=self.registry,
        )
        self.outbound_outcomes = Counter(
            f"{namespace}_outbound_outcomes_total",
            "Outbound queue outcomes by operation.",
            ["operation", "outcome"],
            registry=self.registry,
        )
        self.outbound_retries = Counter(
            f"{namespace}_outbound_retries_total",
            "Outbound queue retries by operation and bounded reason.",
            ["operation", "reason"],
            registry=self.registry,
        )
        self.outbound_saturation = Counter(
            f"{namespace}_outbound_saturation_total",
            "Outbound queue admission rejections by bounded reason.",
            ["reason"],
            registry=self.registry,
        )
        self.outbound_latency = Histogram(
            f"{namespace}_outbound_latency_seconds",
            "Elapsed outbound queue ownership time by operation and outcome.",
            ["operation", "outcome"],
            registry=self.registry,
        )
        self.database_method_duration = Histogram(
            f"{namespace}_database_method_duration_seconds",
            "Elapsed seconds for a DatabaseManager method call.",
            ["method"],
            registry=self.registry,
        )
        self.database_method_failures = Counter(
            f"{namespace}_database_method_failures_total",
            "DatabaseManager method calls that raised an exception.",
            ["method"],
            registry=self.registry,
        )
        self.register_process_collector(process_factory, network_io_counters)

    @staticmethod
    def _priority(priority: str | bool | int) -> str:
        if priority == "blocking" or priority is True or (type(priority) is int and priority == 1):
            return "blocking"
        if priority == "normal" or priority is False or (type(priority) is int and priority == 0):
            return "normal"
        raise ValueError("priority must be blocking or normal")

    @staticmethod
    def _operation(operation: str) -> str:
        if operation not in QUEUED_OPERATIONS:
            raise ValueError("operation is not queueable")
        return operation

    @staticmethod
    def _sender_kind(sender_kind: str) -> str:
        if sender_kind not in _SENDER_KINDS:
            raise ValueError("sender_kind must be main or auxiliary")
        return sender_kind

    @staticmethod
    def _non_negative(value: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite non-negative number")
        return float(value)

    @classmethod
    def _count(cls, value: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _bounded(value: str, allowed: frozenset[str], name: str) -> str:
        if value not in allowed:
            raise ValueError(f"{name} is invalid")
        return value

    def record_membership_probe(self, outcome: str) -> None:
        self.membership_probes.labels(self._bounded(outcome, _MEMBERSHIP_PROBE_OUTCOMES, "membership probe outcome")).inc()

    def record_outbound_outcome(self, operation: str, outcome: str, seconds: float) -> None:
        labels = (self._operation(operation), self._bounded(outcome, _OUTBOUND_OUTCOMES, "outbound outcome"))
        self.outbound_outcomes.labels(*labels).inc()
        self.outbound_latency.labels(*labels).observe(self._non_negative(seconds, "outbound latency"))

    def record_outbound_retry(self, operation: str, reason: str) -> None:
        self.outbound_retries.labels(self._operation(operation), self._bounded(reason, _OUTBOUND_RETRY_REASONS, "outbound retry reason")).inc()

    def record_outbound_saturation(self, reason: str) -> None:
        self.outbound_saturation.labels(self._bounded(reason, _OUTBOUND_SATURATION_REASONS, "outbound saturation reason")).inc()

    def record_database_method_call(self, method: str, seconds: float, outcome: str) -> None:
        """Record one DatabaseManager call using only statically bounded method names."""
        labels = (self._bounded(method, _DATABASE_METHODS, "database method"),)
        self.database_method_duration.labels(*labels).observe(self._non_negative(seconds, "database method duration"))
        if self._bounded(outcome, _DATABASE_OUTCOMES, "database method outcome") == "failure":
            self.database_method_failures.labels(*labels).inc()

    def _register_collector(self, collect: Callable[[], Iterable[Metric]]) -> None:
        collector = _CallbackCollector(collect)
        self.registry.register(collector)
        self._collectors.append(collector)

    def _record_collector_failure(self, collector: str, error: Exception) -> None:
        if collector not in self._logged_collector_failures:
            self._logged_collector_failures.add(collector)
            logging.getLogger(__name__).warning("Metrics collector %s failed (%s).", collector, type(error).__name__)

    def register_process_collector(
        self,
        process_factory: Callable[[], Any] | None = None,
        network_io_counters: Callable[[], Any] | None = None,
    ) -> None:
        register_process_collector(self, process_factory, network_io_counters)

    def register_destination_queue_collector(self, snapshot: Callable[[], Iterable[DestinationQueueSnapshot]], top_n: int) -> None:
        register_destination_queue_collector(self, snapshot, top_n)

    def register_outbound_queue_collectors(self, queue: OutboundQueue, top_n: int) -> None:
        """Register the outbound queue's bounded scrape snapshots."""
        queue.bind_metrics(self)

        def destination_snapshot() -> Iterable[DestinationQueueSnapshot]:
            return (DestinationQueueSnapshot(destination, depth, oldest_age) for destination, depth, oldest_age in queue.destination_snapshot())

        def worker_snapshot() -> WorkerSnapshot:
            healthy, in_flight = queue.worker_snapshot()
            return WorkerSnapshot(healthy, in_flight)

        self.register_destination_queue_collector(destination_snapshot, top_n)
        self.register_worker_collector(worker_snapshot)
        self.register_cooldown_collector(queue.cooldown_snapshot)

    def register_worker_collector(self, snapshot: Callable[[], WorkerSnapshot]) -> None:
        register_worker_collector(self, snapshot)

    def register_cooldown_collector(self, snapshot: Callable[[], Mapping[str, float]]) -> None:
        self._register_bounded_gauge_collector(
            f"{self.namespace}_outbound_cooldown_seconds",
            "Remaining sender cooldown, aggregated by sender kind.",
            "sender_kind",
            _SENDER_KINDS,
            snapshot,
            lambda value: self._non_negative(value, "cooldown"),
        )

    def register_auxiliary_count_collector(self, snapshot: Callable[[], Mapping[str, int]]) -> None:
        self._register_bounded_gauge_collector(
            f"{self.namespace}_auxiliary_bots",
            "Configured auxiliary bots by enabled state.",
            "state",
            _AUXILIARY_STATES,
            snapshot,
            lambda value: self._count(value, "auxiliary count"),
        )

    def register_membership_cache_collector(self, snapshot: Callable[[], Mapping[str, int]]) -> None:
        self._register_bounded_gauge_collector(
            f"{self.namespace}_auxiliary_membership_cache_entries",
            "Aggregate auxiliary membership-cache entries by state.",
            "state",
            _MEMBERSHIP_CACHE_STATES,
            snapshot,
            lambda value: self._count(value, "membership cache count"),
        )

    def register_rate_limit_occupancy_collector(self, snapshot: Callable[[], Mapping[str, float]]) -> None:
        self._register_bounded_gauge_collector(
            f"{self.namespace}_rate_limit_occupancy",
            "Current rate-limit occupancy ratio by aggregate scope.",
            "scope",
            _RATE_LIMIT_SCOPES,
            snapshot,
            self._rate_limit_occupancy_value,
        )

    def _rate_limit_occupancy_value(self, value: float) -> float:
        value = self._non_negative(value, "rate-limit occupancy")
        if value > 1:
            raise ValueError("rate-limit occupancy must not exceed 1")
        return value

    def _register_bounded_gauge_collector(
        self,
        name: str,
        documentation: str,
        label: str,
        allowed: frozenset[str],
        snapshot: Callable[[], Mapping[str, Any]],
        normalize: Callable[[Any], float | int],
    ) -> None:
        register_bounded_gauge_collector(self, name, documentation, label, allowed, snapshot, normalize)
