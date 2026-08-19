"""Prometheus endpoint configuration and runtime lifecycle wiring."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any

from prometheus_client import CollectorRegistry

from ..config.runtime import RuntimeConfiguration
from ..etm_metrics import Metrics
from ..outbound import OutboundQueue
from .bot_pool import BotPool


def parse_metrics_config(metrics_cfg: object, logger: Any) -> tuple[int, tuple[str, int] | None]:
    """Validate the optional Prometheus endpoint configuration."""
    top_n = 20
    if metrics_cfg is None:
        return top_n, None
    if not isinstance(metrics_cfg, Mapping):
        logger.warning("Invalid metrics config type %s; Prometheus endpoint disabled.", type(metrics_cfg).__name__)
        return top_n, None
    try:
        raw_top_n = metrics_cfg.get("top_n", top_n)
        if isinstance(raw_top_n, bool):
            raise ValueError
        parsed_top_n = int(raw_top_n)
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
        raw_port = metrics_cfg.get("port", 9101)
        if isinstance(raw_port, bool):
            raise ValueError
        port = int(raw_port)
        if not 0 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning("Invalid metrics port type %s; Prometheus endpoint disabled.", type(metrics_cfg.get("port")).__name__)
        return top_n, None
    return top_n, (host, port)


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
    config: RuntimeConfiguration,
    database: Any,
    bot_pool: BotPool | None,
    outbound_queue: OutboundQueue,
    logger: logging.Logger,
) -> tuple[Metrics, MetricsServer | None]:
    """Attach scrape callbacks to the live delivery collaborators."""
    top_n, endpoint = parse_metrics_config(config.metrics, logger)
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
