# coding=utf-8
"""Prometheus instrumentation for the ETM outbound send pipeline."""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable, Optional

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily


_SEND_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)
_WAIT_BUCKETS = (0.0, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 15, 30, 60, 120, 300)
_LIFETIME_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)


class _TopNQueueCollector:
    """Expose the N deepest current target queues without remembering labels."""

    def __init__(
        self,
        namespace: str,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, int]]],
        top_n: int = 20,
    ):
        self._namespace = namespace
        self._snapshot_fn = snapshot_fn
        self._top_n = max(0, int(top_n))

    def collect(self):
        family = GaugeMetricFamily(
            f"{self._namespace}_send_queue_target_depth",
            "Current backlog of the deepest per-target FIFOs.",
            labels=["slave_id", "chat_id"],
        )
        try:
            rows = [
                (slave_id, chat_id, depth)
                for slave_id, chat_id, depth in self._snapshot_fn()
                if depth > 0
            ]
        except Exception:
            yield family
            return

        rows.sort(key=lambda row: row[2], reverse=True)
        for slave_id, chat_id, depth in rows[: self._top_n]:
            family.add_metric([str(slave_id), str(chat_id)], depth)
        yield family


class Metrics:
    """All ETM send-pipeline metrics, bound to a private registry."""

    def __init__(self, namespace: str = "etm", top_n: int = 20):
        self.namespace = namespace
        self.top_n = top_n
        self.registry = CollectorRegistry()
        ns = namespace

        self.enqueued = Counter(
            f"{ns}_send_tasks_enqueued_total",
            "Tasks appended to a per-target FIFO queue.",
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
            "Tasks put back on the front of their target FIFO.",
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
            "Total tasks across all per-target FIFOs.",
            registry=self.registry,
        )
        self.queued_targets_g = Gauge(
            f"{ns}_send_queue_targets",
            "Number of distinct targets with a backlog.",
            registry=self.registry,
        )
        self.max_target_depth_g = Gauge(
            f"{ns}_send_queue_max_target_depth",
            "Deepest single-target FIFO.",
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
            "Targets currently deferred by a target retry deadline.",
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

    def register_topn(
        self,
        snapshot_fn: Callable[[], Iterable[tuple[object, object, int]]],
    ) -> None:
        self.registry.register(_TopNQueueCollector(self.namespace, snapshot_fn, self.top_n))

    def task_enqueued(self, priority: bool) -> None:
        self.enqueued.labels(priority="true" if priority else "false").inc()

    def task_dispatched(self, sender: str) -> None:
        self.dispatched.labels(sender=sender).inc()

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
        self.in_flight_g.set(in_flight)
        self.disabled_bot_chats_g.set(disabled_bot_chats)
        self.retry_targets_g.set(retry_targets)
        self.worker_alive_g.set(1 if worker_alive else 0)
        self.aux_pool_size_g.set(aux_pool_size)
        self.aux_disabled_g.set(aux_disabled)

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
