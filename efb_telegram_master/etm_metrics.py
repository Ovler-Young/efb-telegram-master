"""Bounded Prometheus instrumentation for ETM's outbound delivery model."""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric

from .bot_pool import BotPool
from .outbound import QUEUED_OPERATIONS, OutboundQueue

_PRIORITIES = frozenset({"blocking", "normal"})
_SENDER_KINDS = frozenset({"main", "auxiliary"})
_REMOVAL_OUTCOMES = frozenset({"submitted", "terminal_discard"})
_COMPLETION_OUTCOMES = frozenset({"success", "failure"})
_DISPATCH_OUTCOMES = frozenset({"submitted", "deferred", "failed"})
_RETRY_REASONS = frozenset({"rate_limit", "membership", "worker_capacity"})
_FAILURE_STAGES = frozenset({"dispatch", "execution", "terminal"})
_AUXILIARY_STATES = frozenset({"enabled", "disabled"})
_MEMBERSHIP_CACHE_STATES = frozenset({"member", "not_member", "unknown_probe_pending"})
_MEMBERSHIP_PROBE_OUTCOMES = frozenset({"ok_member", "ok_not_member", "forbidden", "bad_request", "error", "queue_full"})
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
        "get_history_migration_entries",
        "delete_history_migration_entry",
    }
)
_DATABASE_OUTCOMES = frozenset({"success", "failure"})


def parse_metrics_config(metrics_cfg: object, logger: Any) -> tuple[int, tuple[str, int] | None]:
    """Validate the optional Prometheus endpoint configuration."""
    top_n = 20
    if metrics_cfg is None:
        return top_n, None
    if not isinstance(metrics_cfg, Mapping):
        logger.warning("Invalid metrics config type %s; Prometheus endpoint disabled.", type(metrics_cfg).__name__)
        return top_n, None
    try:
        parsed_top_n = int(metrics_cfg.get("top_n", top_n))
        if parsed_top_n < 0:
            raise ValueError
        top_n = parsed_top_n
    except (TypeError, ValueError):
        logger.warning("Invalid metrics top_n type %s; using default %d.", type(metrics_cfg.get("top_n")).__name__, top_n)
    host = metrics_cfg.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host:
        logger.warning("Invalid metrics host type %s; Prometheus endpoint disabled.", type(host).__name__)
        return top_n, None
    try:
        port = int(metrics_cfg.get("port", 9101))
        if not 0 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning("Invalid metrics port type %s; Prometheus endpoint disabled.", type(metrics_cfg.get("port")).__name__)
        return top_n, None
    return top_n, (host, port)


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


class MetricsServer:
    """Serving-thread handle with the WSGI server's shutdown compatibility methods."""

    def __init__(self, server: Any, thread: threading.Thread) -> None:
        self._server = server
        self.thread = thread

    def shutdown(self) -> None:
        self._server.shutdown()

    def server_close(self) -> None:
        self._server.server_close()

    def stop(self, join_timeout: float) -> None:
        """Stop serving and close the socket before joining its thread."""
        if self.thread.is_alive():
            self.shutdown()
        self.server_close()
        if self.thread.is_alive() and self.thread.ident != threading.get_ident():
            self.thread.join(timeout=join_timeout)

    @property
    def server_address(self) -> tuple[str, int]:
        return self._server.server_address


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

    def record_database_method_call(self, method: str, seconds: float, outcome: str) -> None:
        """Record one DatabaseManager call using only statically bounded method names."""
        labels = (self._bounded(method, _DATABASE_METHODS, "database method"),)
        self.database_method_duration.labels(*labels).observe(self._non_negative(seconds, "database method duration"))
        if self._bounded(outcome, _DATABASE_OUTCOMES, "database method outcome") == "failure":
            self.database_method_failures.labels(*labels).inc()

    def membership_probe(self, _bot_id: object, _username: object, outcome: str) -> None:
        """Compatibility entry point for auxiliary probes; bot identity is intentionally unlabelled."""
        self.record_membership_probe(outcome)

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
        """Register scrape-time process and host resource observations.

        ``cpu_percent(None)`` is sampled once during registration to establish
        psutil's baseline. Each scrape then reports utilization since the prior
        sample as a Prometheus ratio. Unsupported process I/O sources are
        omitted, and a failure from one source leaves the other observations
        available for that scrape.
        """
        if process_factory is None or network_io_counters is None:
            try:
                import psutil
            except ImportError:
                if process_factory is None:
                    return
            else:
                if process_factory is None:
                    process_factory = psutil.Process
                if network_io_counters is None:
                    network_io_counters = psutil.net_io_counters

        try:
            process = process_factory()
        except Exception as error:
            self._record_collector_failure("process_factory", error)
            return
        try:
            process.cpu_percent(interval=None)
        except Exception as error:
            self._record_collector_failure("process_cpu_baseline", error)

        def collect() -> Iterable[Metric]:
            metrics: list[Metric] = []

            try:
                cpu_percent = self._non_negative(process.cpu_percent(interval=None), "process CPU percent")
                cpu = GaugeMetricFamily(
                    f"{self.namespace}_process_cpu_utilization_ratio",
                    "Process CPU utilization since the previous psutil sample, as a ratio.",
                )
                cpu.add_metric([], cpu_percent / 100)
                metrics.append(cpu)
            except Exception as error:
                self._record_collector_failure("process_cpu", error)

            try:
                memory = process.memory_info()
                resident_memory = self._non_negative(memory.rss, "process resident memory")
                resident = GaugeMetricFamily(
                    f"{self.namespace}_process_resident_memory_bytes",
                    "Resident memory currently used by this process in bytes.",
                )
                resident.add_metric([], resident_memory)
                metrics.append(resident)
            except Exception as error:
                self._record_collector_failure("process_memory", error)

            try:
                disk = process.io_counters()
                disk_read = CounterMetricFamily(
                    f"{self.namespace}_process_disk_read_bytes_total",
                    "Cumulative bytes read by this process from disk.",
                )
                disk_read.add_metric([], self._non_negative(disk.read_bytes, "process disk read bytes"))
                disk_write = CounterMetricFamily(
                    f"{self.namespace}_process_disk_write_bytes_total",
                    "Cumulative bytes written by this process to disk.",
                )
                disk_write.add_metric([], self._non_negative(disk.write_bytes, "process disk write bytes"))
                metrics.extend((disk_read, disk_write))
            except Exception as error:
                self._record_collector_failure("process_disk", error)

            if network_io_counters is not None:
                try:
                    network_counters = network_io_counters()
                    network_receive = CounterMetricFamily(
                        f"{self.namespace}_host_network_receive_bytes_total",
                        "Cumulative network bytes received by the host.",
                    )
                    network_receive.add_metric([], self._non_negative(network_counters.bytes_recv, "host network received bytes"))
                    network_transmit = CounterMetricFamily(
                        f"{self.namespace}_host_network_transmit_bytes_total",
                        "Cumulative network bytes transmitted by the host.",
                    )
                    network_transmit.add_metric([], self._non_negative(network_counters.bytes_sent, "host network transmitted bytes"))
                    metrics.extend((network_receive, network_transmit))
                except Exception as error:
                    self._record_collector_failure("host_network", error)

            return tuple(metrics)

        self._register_collector(collect)

    def register_destination_queue_collector(self, snapshot: Callable[[], Iterable[DestinationQueueSnapshot]], top_n: int) -> None:
        """Register a scrape-time top-N destination snapshot with no retained destination state."""
        cap = self._count(top_n, "destination top_n")

        def collect() -> Iterable[GaugeMetricFamily]:
            depth = GaugeMetricFamily(
                f"{self.namespace}_outbound_destination_queue_depth",
                "Current queued rows for the deepest destinations in this scrape.",
                labels=["destination"],
            )
            oldest = GaugeMetricFamily(
                f"{self.namespace}_outbound_destination_oldest_age_seconds",
                "Oldest queued-row age for the deepest destinations in this scrape.",
                labels=["destination"],
            )
            samples = list(snapshot())
            for item in sorted(samples, key=lambda value: value.depth, reverse=True)[:cap]:
                if not isinstance(item.destination, str) or not item.destination:
                    raise ValueError("destination must be a non-empty string")
                depth.add_metric([item.destination], self._count(item.depth, "destination depth"))
                if item.oldest_age_seconds is not None:
                    oldest.add_metric([item.destination], self._non_negative(item.oldest_age_seconds, "destination oldest age"))
            return (depth, oldest)

        self._register_collector(collect)

    def register_outbound_queue_collectors(self, queue: OutboundQueue, top_n: int) -> None:
        """Register the outbound queue's bounded scrape snapshots."""

        def destination_snapshot() -> Iterable[DestinationQueueSnapshot]:
            return (DestinationQueueSnapshot(destination, depth, oldest_age) for destination, depth, oldest_age in queue.destination_snapshot())

        def worker_snapshot() -> WorkerSnapshot:
            healthy, in_flight = queue.worker_snapshot()
            return WorkerSnapshot(healthy, in_flight)

        self.register_destination_queue_collector(destination_snapshot, top_n)
        self.register_worker_collector(worker_snapshot)
        self.register_cooldown_collector(queue.cooldown_snapshot)

    def register_worker_collector(self, snapshot: Callable[[], WorkerSnapshot]) -> None:
        """Register aggregate worker liveness and in-flight state."""

        def collect() -> Iterable[GaugeMetricFamily]:
            health = GaugeMetricFamily(f"{self.namespace}_outbound_worker_healthy", "Whether the outbound worker is alive.")
            in_flight = GaugeMetricFamily(f"{self.namespace}_outbound_worker_in_flight", "Calls currently owned by the outbound worker.")
            value = snapshot()
            health.add_metric([], int(value.healthy))
            in_flight.add_metric([], self._count(value.in_flight, "worker in-flight"))
            return (health, in_flight)

        self._register_collector(collect)

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
        def collect() -> Iterable[GaugeMetricFamily]:
            metric = GaugeMetricFamily(name, documentation, labels=[label])
            for key, value in snapshot().items():
                metric.add_metric([self._bounded(key, allowed, label)], normalize(value))
            return (metric,)

        self._register_collector(collect)


def start_metrics_server(host: str, port: int, registry: CollectorRegistry) -> MetricsServer:
    """Start a daemon WSGI serving thread and return its bounded-shutdown handle."""
    from wsgiref.simple_server import WSGIRequestHandler, make_server

    from prometheus_client import make_wsgi_app

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

    server = make_server(host, port, make_wsgi_app(registry), handler_class=QuietHandler)
    thread = threading.Thread(target=server.serve_forever, name="ETM metrics server", daemon=True)
    thread.start()
    metrics_server = MetricsServer(server, thread)
    logging.getLogger(__name__).info("Metrics endpoint listening on %s", metrics_server.server_address)
    return metrics_server


def configure_runtime_metrics(
    config: Mapping[str, object],
    database: Any,
    bot_pool: BotPool | None,
    outbound_queue: OutboundQueue,
    logger: logging.Logger,
) -> tuple[Metrics, MetricsServer | None]:
    """Attach scrape callbacks to the live delivery collaborators."""
    top_n, endpoint = parse_metrics_config(config.get("metrics"), logger)
    metrics = Metrics(namespace="etm")
    database.set_metrics(metrics)
    if bot_pool:
        for auxiliary in bot_pool.bots:
            auxiliary.bind_metrics(metrics)
    metrics.register_outbound_queue_collectors(outbound_queue, top_n)
    metrics.register_auxiliary_count_collector(bot_pool.auxiliary_count_snapshot if bot_pool else lambda: {"enabled": 0, "disabled": 0})
    metrics.register_membership_cache_collector(bot_pool.membership_cache_snapshot if bot_pool else lambda: {"member": 0, "not_member": 0, "unknown_probe_pending": 0})
    metrics.register_rate_limit_occupancy_collector(outbound_queue.rate_limit_occupancy_snapshot)
    if endpoint is None:
        return metrics, None
    return metrics, start_metrics_server(*endpoint, registry=metrics.registry)
