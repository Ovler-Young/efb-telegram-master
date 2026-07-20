"""In-memory correlation between inbound commands and queued bot replies.

The correlation is intentionally process-local.  Queue rows must remain
restartable without test-only metadata, so this module never affects the
SQLite payload or schema.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from typing import Optional


@dataclass(frozen=True)
class InboundCommandKey:
    chat_id: int
    message_id: int


class CommandOutboundState(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    TERMINAL_FAILURE = "terminal_failure"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class CommandOutboundOutcome:
    row_id: int
    operation: str
    target_chat_id: int
    state: CommandOutboundState
    retry_at: Optional[float] = None
    error: Optional[BaseException] = None


@dataclass
class _ObservedRow:
    inbound: InboundCommandKey
    outcome: CommandOutboundOutcome
    expires_at: float


class CommandOutboundObserver:
    """Bounded, thread-safe state for test-only command response observation."""

    def __init__(self, *, ttl: float = 300.0, capacity: int = 256) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._rows: OrderedDict[int, _ObservedRow] = OrderedDict()
        self._condition = threading.Condition()

    def _prune(self, now: float) -> None:
        expired = [row_id for row_id, row in self._rows.items() if row.expires_at <= now]
        for row_id in expired:
            self._rows.pop(row_id, None)
        while len(self._rows) > self._capacity:
            self._rows.popitem(last=False)

    def register(self, inbound: InboundCommandKey, row_id: int, operation: str, target_chat_id: int) -> None:
        with self._condition:
            now = time.monotonic()
            self._prune(now)
            self._rows[row_id] = _ObservedRow(
                inbound=inbound,
                outcome=CommandOutboundOutcome(
                    row_id=row_id,
                    operation=operation,
                    target_chat_id=target_chat_id,
                    state=CommandOutboundState.PENDING,
                ),
                expires_at=now + self._ttl,
            )
            self._prune(now)
            self._condition.notify_all()

    def retry(self, row_id: int, retry_at: float) -> None:
        self._replace(row_id, state=CommandOutboundState.PENDING, retry_at=retry_at)

    def succeed(self, row_id: int) -> None:
        self._replace(row_id, state=CommandOutboundState.SUCCESS)

    def fail(self, row_id: int, error: BaseException) -> None:
        self._replace(row_id, state=CommandOutboundState.TERMINAL_FAILURE, error=error)

    def shutdown(self, error: BaseException) -> None:
        with self._condition:
            now = time.monotonic()
            self._prune(now)
            for row_id, observed in tuple(self._rows.items()):
                if observed.outcome.state is not CommandOutboundState.PENDING:
                    continue
                self._rows[row_id] = _ObservedRow(
                    inbound=observed.inbound,
                    outcome=CommandOutboundOutcome(
                        row_id=row_id,
                        operation=observed.outcome.operation,
                        target_chat_id=observed.outcome.target_chat_id,
                        state=CommandOutboundState.SHUTDOWN,
                        retry_at=observed.outcome.retry_at,
                        error=error,
                    ),
                    expires_at=now + self._ttl,
                )
            self._condition.notify_all()

    def _replace(
        self,
        row_id: int,
        *,
        state: CommandOutboundState,
        retry_at: Optional[float] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        with self._condition:
            observed = self._rows.get(row_id)
            if observed is None:
                return
            outcome = observed.outcome
            if outcome.state is not CommandOutboundState.PENDING:
                return
            self._rows[row_id] = _ObservedRow(
                inbound=observed.inbound,
                outcome=CommandOutboundOutcome(
                    row_id=row_id,
                    operation=outcome.operation,
                    target_chat_id=outcome.target_chat_id,
                    state=state,
                    retry_at=retry_at if retry_at is not None else outcome.retry_at,
                    error=error,
                ),
                expires_at=time.monotonic() + self._ttl,
            )
            self._condition.notify_all()

    def wait_for_completion(
        self, inbound: InboundCommandKey, operation: str, target_chat_id: int, timeout: float
    ) -> CommandOutboundOutcome:
        """Wait for one exact command response without extending its deadline."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                now = time.monotonic()
                self._prune(now)
                matches = [
                    observed.outcome for observed in self._rows.values()
                    if observed.inbound == inbound
                    and observed.outcome.operation == operation
                    and observed.outcome.target_chat_id == target_chat_id
                ]
                for outcome in reversed(matches):
                    if outcome.state is not CommandOutboundState.PENDING:
                        return outcome
                remaining = deadline - now
                if remaining <= 0:
                    raise TimeoutError(
                        "Timed out waiting for the exact outbound command response "
                        f"{operation!r} to chat {target_chat_id}."
                    )
                self._condition.wait(remaining)

    def snapshot(self, inbound: InboundCommandKey, operation: str, target_chat_id: int) -> Optional[CommandOutboundOutcome]:
        """Return the newest exact match.  Intended for focused unit tests."""
        with self._condition:
            self._prune(time.monotonic())
            for observed in reversed(self._rows.values()):
                if (
                    observed.inbound == inbound
                    and observed.outcome.operation == operation
                    and observed.outcome.target_chat_id == target_chat_id
                ):
                    return observed.outcome
        return None
