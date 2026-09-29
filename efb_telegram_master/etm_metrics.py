"""Prometheus instrumentation for the dequeue-only outbound queue."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from .outbound import QUEUED_OPERATIONS


_PRIORITIES = frozenset({"blocking", "normal"})
_SENDER_KINDS = frozenset({"main", "auxiliary"})
_REMOVAL_OUTCOMES = frozenset({"submitted", "terminal_discard"})
_COMPLETION_OUTCOMES = frozenset({"success", "failure"})


class Metrics:
    """Expose only bounded queue lifecycle metrics defined by the queue contract."""

    def __init__(self, namespace: str = "etm") -> None:
        self.registry = CollectorRegistry()
        self.namespace = namespace
        self.enqueued = Counter(
            f"{namespace}_outbound_enqueued_total",
            "Queued rows whose insert transaction committed.",
            ["priority", "operation"],
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            f"{namespace}_outbound_queue_depth",
            "Absolute number of rows currently persisted in the outbound queue.",
            registry=self.registry,
        )
        self.queue_residence = Histogram(
            f"{namespace}_outbound_queue_residence_seconds",
            "Seconds from enqueue until a known queue-row removal commit.",
            ["priority", "operation", "outcome"],
            registry=self.registry,
        )
        self.removals = Counter(
            f"{namespace}_outbound_queue_removals_total",
            "Queued rows whose removal transaction committed.",
            ["priority", "operation", "outcome"],
            registry=self.registry,
        )
        self.dequeued = Counter(
            f"{namespace}_outbound_dequeued_total",
            "Rows deleted before executor submission.",
            ["priority", "operation"],
            registry=self.registry,
        )
        self.dispatch_failures = Counter(
            f"{namespace}_outbound_dispatch_failures_total",
            "Executor submission failures after a queue-row deletion commit.",
            ["priority", "operation"],
            registry=self.registry,
        )
        self.in_flight = Gauge(
            f"{namespace}_outbound_in_flight",
            "Dequeued calls currently owned by the scheduler.",
            ["priority", "operation", "sender_kind"],
            registry=self.registry,
        )
        self.completions = Counter(
            f"{namespace}_outbound_completions_total",
            "Harvested executor completions.",
            ["priority", "operation", "sender_kind", "outcome"],
            registry=self.registry,
        )

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
    def _removal_outcome(outcome: str) -> str:
        if outcome not in _REMOVAL_OUTCOMES:
            raise ValueError("removal outcome must be submitted or terminal_discard")
        return outcome

    @staticmethod
    def _completion_outcome(outcome: str) -> str:
        if outcome not in _COMPLETION_OUTCOMES:
            raise ValueError("completion outcome must be success or failure")
        return outcome

    def record_enqueued(self, priority: str | bool | int, operation: str) -> None:
        self.enqueued.labels(self._priority(priority), self._operation(operation)).inc()

    def set_queue_depth(self, depth: int) -> None:
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
            raise ValueError("queue depth must be a non-negative integer")
        self.queue_depth.set(depth)

    def record_removal(
        self, priority: str | bool | int, operation: str, outcome: str, residence_seconds: float
    ) -> None:
        normalized_priority = self._priority(priority)
        normalized_operation = self._operation(operation)
        normalized_outcome = self._removal_outcome(outcome)
        if residence_seconds < 0:
            raise ValueError("queue residence must not be negative")
        labels = (normalized_priority, normalized_operation, normalized_outcome)
        self.queue_residence.labels(*labels).observe(residence_seconds)
        self.removals.labels(*labels).inc()

    def record_dequeued(self, priority: str | bool | int, operation: str) -> None:
        self.dequeued.labels(self._priority(priority), self._operation(operation)).inc()

    def record_dispatch_failure(self, priority: str | bool | int, operation: str) -> None:
        self.dispatch_failures.labels(self._priority(priority), self._operation(operation)).inc()

    def increment_in_flight(
        self, priority: str | bool | int, operation: str, sender_kind: str
    ) -> None:
        self.in_flight.labels(
            self._priority(priority), self._operation(operation), self._sender_kind(sender_kind)
        ).inc()

    def decrement_in_flight(
        self, priority: str | bool | int, operation: str, sender_kind: str
    ) -> None:
        self.in_flight.labels(
            self._priority(priority), self._operation(operation), self._sender_kind(sender_kind)
        ).dec()

    def record_completion(
        self, priority: str | bool | int, operation: str, sender_kind: str, outcome: str
    ) -> None:
        self.completions.labels(
            self._priority(priority),
            self._operation(operation),
            self._sender_kind(sender_kind),
            self._completion_outcome(outcome),
        ).inc()


def start_metrics_server(host: str, port: int, registry) -> object:
    """Start a daemon WSGI server exposing the supplied registry."""
    from prometheus_client import make_wsgi_app
    from wsgiref.simple_server import WSGIRequestHandler, make_server

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *_args):
            return

    return make_server(host, port, make_wsgi_app(registry), handler_class=QuietHandler)
