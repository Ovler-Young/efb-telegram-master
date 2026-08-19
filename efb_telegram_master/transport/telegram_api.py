"""Synchronous Telegram Bot API lifecycle facade."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from ..auxiliary_bot import MembershipProbeShutdownTimeout
from ..bot_pool import BotPool
from ..outbound import OutboundQueue
from .telegram_api_operations import SyncBotProtocol, TelegramAPIOperations

if TYPE_CHECKING:
    from .. import TelegramChannel
    from ..metrics_runtime import MetricsServer


class MetricsServerShutdownTimeout(RuntimeError):
    """The metrics serving thread remained alive after its shutdown deadline."""


class TelegramAPI(TelegramAPIOperations):
    """Own delivery-resource lifecycle and expose Telegram operation routing."""

    def __init__(
        self,
        channel: "TelegramChannel",
        bot: SyncBotProtocol,
        outbound_queue: OutboundQueue,
        bot_pool: BotPool | None,
    ) -> None:
        self._channel = channel
        self._bot = bot
        self._outbound_queue = outbound_queue
        self.bot_pool = bot_pool
        self._cleanup_tls = threading.local()
        self._metrics_server: MetricsServer | None = None
        self._delivery_stop_lock = threading.Lock()
        self._outbound_stopped = False
        self._membership_shutdown_started = False
        self._membership_shutdown_complete = bot_pool is None
        self._delivery_resources_stopped = False

    def bind_metrics_server(self, metrics_server: "MetricsServer | None") -> None:
        self._metrics_server = metrics_server

    def begin_delivery_shutdown(self, deadline: float) -> tuple[BaseException, ...]:
        """Stop outbound work and cancel membership probes before runtime teardown."""
        with self._delivery_stop_lock:
            outbound_stopped = self._outbound_stopped
            metrics_server = self._metrics_server
            membership_shutdown_complete = self._membership_shutdown_complete
            bot_pool = self.bot_pool
        errors: list[BaseException] = []
        if not outbound_stopped:
            try:
                self._outbound_queue.stop(deadline)
            except BaseException as error:
                errors.append(error)
            else:
                with self._delivery_stop_lock:
                    self._outbound_stopped = True
        if metrics_server is not None:
            try:
                metrics_server.stop(max(0.0, deadline - time.monotonic()))
                thread = getattr(metrics_server, "thread", None)
                if thread is not None and thread.is_alive():
                    raise MetricsServerShutdownTimeout("Metrics server did not stop before the shutdown deadline.")
            except BaseException as error:
                errors.append(error)
            else:
                with self._delivery_stop_lock:
                    if self._metrics_server is metrics_server:
                        self._metrics_server = None
        if bot_pool and not membership_shutdown_complete:
            try:
                membership_errors = bot_pool.begin_shutdown()
            except BaseException as error:
                membership_errors = (error,)
            with self._delivery_stop_lock:
                self._membership_shutdown_started = True
                if not membership_errors:
                    self._membership_shutdown_complete = True
            errors.extend(membership_errors)
        return tuple(errors)

    def finish_delivery_shutdown(self, deadline: float) -> tuple[BaseException, ...]:
        """Join membership probe workers after the runtime has cancelled their calls."""
        with self._delivery_stop_lock:
            if self._delivery_resources_stopped:
                return ()
            bot_pool = self.bot_pool
            membership_shutdown_started = self._membership_shutdown_started
        errors: list[BaseException] = []
        if bot_pool and membership_shutdown_started:
            try:
                incomplete = bot_pool.wait_for_shutdown(deadline)
                if incomplete:
                    joined = ", ".join(map(str, incomplete))
                    errors.append(MembershipProbeShutdownTimeout(f"Auxiliary membership probes did not stop before the final deadline for bot IDs: {joined}"))
            except BaseException as error:
                errors.append(error)
        with self._delivery_stop_lock:
            delivery_complete = self._outbound_stopped and self._metrics_server is None and self._membership_shutdown_complete
        if not errors and delivery_complete:
            with self._delivery_stop_lock:
                self._delivery_resources_stopped = True
        return tuple(errors)

    def stop_delivery_resources(self, deadline: float) -> tuple[BaseException, ...]:
        """Stop delivery resources without a polling runtime owner by one deadline."""
        errors = list(self.begin_delivery_shutdown(deadline))
        errors.extend(self.finish_delivery_shutdown(deadline))
        return tuple(errors)
