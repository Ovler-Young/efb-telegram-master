"""Process and host resource Prometheus collector registration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, Metric

if TYPE_CHECKING:
    from .etm_metrics import DestinationQueueSnapshot, Metrics, WorkerSnapshot


def _counter_metric(name: str, documentation: str, value: float) -> CounterMetricFamily:
    metric = CounterMetricFamily(name, documentation)
    metric.add_metric([], value)
    return metric


def register_process_collector(
    metrics: Metrics,
    process_factory: Callable[[], Any] | None = None,
    network_io_counters: Callable[[], Any] | None = None,
) -> None:
    """Register scrape-time process and host resource observations.

    ``cpu_percent(None)`` is sampled once during registration to establish
    psutil's baseline. Each scrape then reports utilization since the prior
    sample as a Prometheus ratio. Unsupported process I/O sources are omitted,
    and a failure from one source leaves the other observations available.
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
        metrics._record_collector_failure("process_factory", error)
        return
    try:
        process.cpu_percent(interval=None)
    except Exception as error:
        metrics._record_collector_failure("process_cpu_baseline", error)

    def collect() -> Iterable[Metric]:
        collected: list[Metric] = []

        try:
            cpu_percent = metrics._non_negative(process.cpu_percent(interval=None), "process CPU percent")
            cpu = GaugeMetricFamily(
                f"{metrics.namespace}_process_cpu_utilization_ratio",
                "Process CPU utilization since the previous psutil sample, as a ratio.",
            )
            cpu.add_metric([], cpu_percent / 100)
            collected.append(cpu)
        except Exception as error:
            metrics._record_collector_failure("process_cpu", error)

        try:
            memory = process.memory_info()
            resident_memory = metrics._non_negative(memory.rss, "process resident memory")
            resident = GaugeMetricFamily(
                f"{metrics.namespace}_process_resident_memory_bytes",
                "Resident memory currently used by this process in bytes.",
            )
            resident.add_metric([], resident_memory)
            collected.append(resident)
        except Exception as error:
            metrics._record_collector_failure("process_memory", error)

        try:
            disk = process.io_counters()
            collected.extend(
                (
                    _counter_metric(
                        f"{metrics.namespace}_process_disk_read_bytes_total",
                        "Cumulative bytes read by this process from disk.",
                        metrics._non_negative(disk.read_bytes, "process disk read bytes"),
                    ),
                    _counter_metric(
                        f"{metrics.namespace}_process_disk_write_bytes_total",
                        "Cumulative bytes written by this process to disk.",
                        metrics._non_negative(disk.write_bytes, "process disk write bytes"),
                    ),
                )
            )
        except Exception as error:
            metrics._record_collector_failure("process_disk", error)

        if network_io_counters is not None:
            try:
                network_counters = network_io_counters()
                collected.extend(
                    (
                        _counter_metric(
                            f"{metrics.namespace}_host_network_receive_bytes_total",
                            "Cumulative network bytes received by the host.",
                            metrics._non_negative(network_counters.bytes_recv, "host network received bytes"),
                        ),
                        _counter_metric(
                            f"{metrics.namespace}_host_network_transmit_bytes_total",
                            "Cumulative network bytes transmitted by the host.",
                            metrics._non_negative(network_counters.bytes_sent, "host network transmitted bytes"),
                        ),
                    )
                )
            except Exception as error:
                metrics._record_collector_failure("host_network", error)

        return tuple(collected)

    metrics._register_collector(collect)


def register_destination_queue_collector(metrics: Metrics, snapshot: Callable[[], Iterable[DestinationQueueSnapshot]], top_n: int) -> None:
    """Register a scrape-time top-N destination snapshot."""
    cap = metrics._count(top_n, "destination top_n")

    def collect() -> Iterable[GaugeMetricFamily]:
        depth = GaugeMetricFamily(
            f"{metrics.namespace}_outbound_destination_queue_depth",
            "Current queued rows for the deepest destinations in this scrape.",
            labels=["destination"],
        )
        oldest = GaugeMetricFamily(
            f"{metrics.namespace}_outbound_destination_oldest_age_seconds",
            "Oldest queued-row age for the deepest destinations in this scrape.",
            labels=["destination"],
        )
        samples = list(snapshot())
        for item in sorted(samples, key=lambda value: value.depth, reverse=True)[:cap]:
            if not isinstance(item.destination, str) or not item.destination:
                raise ValueError("destination must be a non-empty string")
            depth.add_metric([item.destination], metrics._count(item.depth, "destination depth"))
            if item.oldest_age_seconds is not None:
                oldest.add_metric([item.destination], metrics._non_negative(item.oldest_age_seconds, "destination oldest age"))
        return (depth, oldest)

    metrics._register_collector(collect)


def register_worker_collector(metrics: Metrics, snapshot: Callable[[], WorkerSnapshot]) -> None:
    """Register aggregate worker liveness and in-flight state."""

    def collect() -> Iterable[GaugeMetricFamily]:
        health = GaugeMetricFamily(f"{metrics.namespace}_outbound_worker_healthy", "Whether the outbound worker is alive.")
        in_flight = GaugeMetricFamily(f"{metrics.namespace}_outbound_worker_in_flight", "Calls currently owned by the outbound worker.")
        value = snapshot()
        health.add_metric([], int(value.healthy))
        in_flight.add_metric([], metrics._count(value.in_flight, "worker in-flight"))
        return (health, in_flight)

    metrics._register_collector(collect)


def register_bounded_gauge_collector(
    metrics: Metrics,
    name: str,
    documentation: str,
    label: str,
    allowed: frozenset[str],
    snapshot: Callable[[], Mapping[str, Any]],
    normalize: Callable[[Any], float | int],
) -> None:
    """Register a gauge that validates each label against a fixed set."""

    def collect() -> Iterable[GaugeMetricFamily]:
        metric = GaugeMetricFamily(name, documentation, labels=[label])
        for key, value in snapshot().items():
            metric.add_metric([metrics._bounded(key, allowed, label)], normalize(value))
        return (metric,)

    metrics._register_collector(collect)
