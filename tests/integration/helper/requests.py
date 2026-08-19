import asyncio
import time
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional, TypeVar

PRIVATE_RESPONSE_WAIT_CAP = 65.0
Response = TypeVar("Response")
_private_response_cursor: ContextVar[Optional[int]] = ContextVar("private_response_cursor", default=None)


async def wait_for_limiter_slot(peek_delay: Callable[[], float], *, cap: float = PRIVATE_RESPONSE_WAIT_CAP) -> None:
    """Wait for one outbound limiter slot, never beyond its 60-second window plus margin."""
    deadline = time.monotonic() + cap
    while True:
        delay = max(0.0, peek_delay())
        if delay == 0.0:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise TimeoutError(f"Outbound limiter did not free a slot within {cap:g} seconds")
        await asyncio.sleep(min(delay, remaining))


async def wait_for_private_response(
    peek_delay: Callable[[], float],
    trigger: Callable[[], Awaitable[object]],
    receive: Callable[[float], Awaitable[Response]],
    *,
    cap: float = PRIVATE_RESPONSE_WAIT_CAP,
    response_cursor: Optional[Callable[[], int]] = None,
) -> Response:
    """Use one deadline for the limiter wait, command, and its response."""

    async def wait() -> Response:
        deadline = time.monotonic() + cap
        await wait_for_limiter_slot(peek_delay, cap=deadline - time.monotonic())
        cursor = response_cursor() if response_cursor else None
        cursor_token = _private_response_cursor.set(cursor)
        try:
            await trigger()
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                raise TimeoutError(f"Private response did not arrive within {cap:g} seconds")
            return await receive(remaining)
        finally:
            _private_response_cursor.reset(cursor_token)

    return await asyncio.wait_for(wait(), timeout=cap)
