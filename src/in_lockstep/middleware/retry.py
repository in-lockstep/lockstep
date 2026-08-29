"""Retry at the action boundary — narrowed, deliberately.

Retry belongs at the transport, where one HTTP attempt is one HTTP attempt. Re-invoking an action
that spends budget re-runs a whole agentic loop and re-pays every turn already spent, which is the
opposite of what a retry is for. So this refuses such actions and says why, rather than silently
declining to retry them.
"""

from __future__ import annotations

import asyncio
import random

from ..core.middleware import ActionCall, Next, capabilities_for
from ..core.outcome import Finding, Outcome, Severity, Status
from ..core.verbs import Capability


class Retry:
    def __init__(self, *, attempts: int = 3, base_delay: float = 0.5, max_delay: float = 8.0) -> None:
        self.attempts = attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    async def __call__(self, ctx: object, call: ActionCall, next: Next) -> Outcome[object]:
        capabilities = capabilities_for(ctx, call)

        if Capability.SPENDS_BUDGET in capabilities:
            outcome = await next()
            return outcome.with_findings(
                Finding(
                    id="retry.refused_budgeted_action",
                    message=(
                        "not retried: this action spends budget, and re-invoking it would re-pay "
                        "every turn already spent. Transport-level retry applies instead."
                    ),
                    severity=Severity.NOTE,
                )
            )

        last: Outcome[object] | None = None
        for attempt in range(self.attempts):
            last = await next()
            # ERRORED only. A red test suite is a domain answer, not a flaky call.
            if last.status is not Status.ERRORED:
                return last
            if attempt < self.attempts - 1:
                delay = min(self.base_delay * (2**attempt), self.max_delay)
                await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
        assert last is not None
        return last
