import logging
import time
from contextlib import contextmanager
from functools import wraps
from typing import Callable, Optional, Protocol


class DatabaseMetrics(Protocol):
    """Metrics interface injected by the bot manager after construction."""

    def record_database_method_call(self, method: str, seconds: float, outcome: str) -> None: ...


class ObservedRepository:
    logger = logging.getLogger(__name__)
    _metrics: Optional[DatabaseMetrics] = None

    def __init__(self, database=None) -> None:
        self._bind_models = database is not None
        if database is None:
            from .models import database as configured_database

            database = configured_database
        self._database = database

    @property
    def database(self):
        return getattr(self._database, "obj", self._database)

    @contextmanager
    def _bound_models(self):
        from .models import DATABASE_MODELS

        if not self._bind_models:
            yield
            return
        current_database = self.database
        if current_database is None:
            raise RuntimeError("Repository database has not been initialized")
        previous_databases = tuple(model._meta.database for model in DATABASE_MODELS)
        try:
            with current_database.bind_ctx(DATABASE_MODELS):
                yield
        finally:
            for model, previous_database in zip(DATABASE_MODELS, previous_databases):
                model._meta.set_database(previous_database)


def observe_database_method(method: str):
    """Measure one public database operation with a statically bounded method label."""

    def decorate(call: Callable):
        @wraps(call)
        def wrapped(repository: ObservedRepository, *args, **kwargs):
            started = time.perf_counter()
            outcome = "success"
            try:
                bind_models = getattr(repository, "_bound_models", None)
                if bind_models is None:
                    return call(repository, *args, **kwargs)
                with bind_models():
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


def bind_database(call: Callable):
    """Bind query models to the repository's database for one operation."""

    @wraps(call)
    def wrapped(repository: ObservedRepository, *args, **kwargs):
        with repository._bound_models():
            return call(repository, *args, **kwargs)

    return wrapped
