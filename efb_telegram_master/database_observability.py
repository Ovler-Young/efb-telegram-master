import logging
import time
from functools import wraps
from typing import Callable, Optional, Protocol


class DatabaseMetrics(Protocol):
    """Metrics interface injected by the bot manager after construction."""

    def record_database_method_call(self, method: str, seconds: float, outcome: str) -> None: ...


class ObservedRepository:
    logger = logging.getLogger(__name__)
    _metrics: Optional[DatabaseMetrics] = None


def observe_database_method(method: str):
    """Measure one public database operation with a statically bounded method label."""

    def decorate(call: Callable):
        @wraps(call)
        def wrapped(repository: ObservedRepository, *args, **kwargs):
            started = time.perf_counter()
            outcome = "success"
            try:
                return call(repository, *args, **kwargs)
            except Exception:
                outcome = "failure"
                raise
            finally:
                metrics = getattr(repository, "_metrics", None)
                if metrics is not None:
                    try:
                        metrics.record_database_method_call(method, time.perf_counter() - started, outcome)
                    except Exception:
                        repository.logger.exception("Unable to record database method metric: %s", method)

        return wrapped

    return decorate
