"""Transport retry — the single layer that owns it.

The upstream stack had three: the SDK's own default of two extra attempts, a caller-layer helper
with three more, and a middleware on top. That is roughly twelve HTTP attempts per logical call
and forty-eight with the middleware, per turn of an agentic loop — so a twenty-turn agent could
reach several hundred requests without anything looking wrong.

So: SDK retries are disabled at construction, the action-boundary middleware refuses to retry
anything that spends budget, and this is where a retry actually happens. One HTTP attempt is one
HTTP attempt.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from ..llm.interface import LLMError, RateLimitError

T = TypeVar("T")


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 30.0
    # A retry that sleeps past the run's deadline has not retried, it has just spent the remaining
    # budget waiting. Honour Retry-After only while it fits.
    remaining_wall_seconds: float | None = None

    async def run(
        self,
        call: Callable[[], Awaitable[T]],
        *,
        label: str = "",
        remaining_wall_seconds: float | None = None,
    ) -> T:
        """Run `call`, retrying transient failures within the time that is actually left.

        `remaining_wall_seconds` is a per-call argument and not only a field, because the field
        cannot be right: an `AiInvoker` constructs one `RetryPolicy` and then makes a call per
        turn, so a value fixed at construction describes the budget at turn zero forever. It was
        never passed at all, which left the field `None` on every live path — so a provider
        answering `Retry-After: 3600` slept for an hour inside a job with a twenty-minute timeout,
        and the deadline could not interrupt it because deadlines are checked *between* turns.
        """
        budget = remaining_wall_seconds if remaining_wall_seconds is not None else self.remaining_wall_seconds
        last: BaseException | None = None
        for attempt in range(self.attempts):
            try:
                return await call()
            except LLMError as e:
                if not e.retryable:
                    raise
                last = e
                if attempt == self.attempts - 1:
                    raise
                budget = await self._sleep(e, attempt, budget)
        assert last is not None  # pragma: no cover - loop always raises or returns
        raise last

    async def _sleep(self, error: LLMError, attempt: int, remaining: float | None) -> float | None:
        """Sleep the backoff, and return what is left of the budget afterwards.

        Returning the remainder rather than re-reading a field is what makes a *sequence* of
        retries bounded: two 30-second sleeps under a 40-second deadline must not both be allowed
        just because each one fits on its own.
        """
        delay = min(self.base_delay * (2**attempt), self.max_delay)
        retry_after = getattr(error, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            delay = float(retry_after)
        if remaining is not None and delay > remaining:
            # Sleeping past the deadline is not a retry, it is spending what is left waiting.
            raise error
        jittered = delay + random.uniform(0, delay * 0.1)
        await asyncio.sleep(jittered)
        return None if remaining is None else remaining - jittered


__all__ = ["RetryPolicy", "RateLimitError"]
