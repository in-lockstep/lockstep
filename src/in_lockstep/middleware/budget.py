"""Reconciling budget check at the action boundary.

This is the *second* half of cost enforcement, not the whole of it. The predictive check lives
inside the invocation, because a whole agentic loop is one action call and a check here only fires
once the turns are already paid for. What this adds is reconciliation: if the estimate and the
actual diverge, the run stops here rather than drifting quietly.
"""

from __future__ import annotations

from ..core.middleware import ActionCall, Next
from ..core.outcome import Finding, Outcome, Severity, Status
from ..core.spend import Budget


class CostBudget:
    def __init__(
        self, *, usd: float | None = None, tokens: int | None = None, wall_seconds: float | None = None
    ) -> None:
        self.budget = Budget(usd=usd, tokens=tokens, wall_seconds=wall_seconds)

    async def __call__(self, ctx: object, call: ActionCall, next: Next) -> Outcome[object]:
        spend = getattr(ctx, "spend", None)
        if spend is None:
            return await next()

        spend.budget = spend.budget.merge(self.budget)

        exceeded = spend.exceeded()
        if exceeded is not None:
            return Outcome(
                status=Status.BLOCKED,
                reason=f"budget:{exceeded}",
                findings=(
                    Finding(
                        id="cost.budget_exceeded",
                        message=f"run budget exceeded before {call!r}: {exceeded}",
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )

        outcome = await next()

        # Reconcile: the predictive check inside the invocation works from an estimate, and an
        # estimate that drifts is a ceiling that does not hold.
        after = spend.exceeded()
        if after is not None and outcome.status is not Status.BLOCKED:
            return Outcome(
                status=Status.BLOCKED,
                value=outcome.value,
                reason=f"budget:{after}",
                cost=outcome.cost,
                findings=(
                    *outcome.findings,
                    Finding(
                        id="cost.budget_overrun",
                        message=(
                            f"actual spend exceeded the ceiling after {call!r}: {after}. "
                            "The pre-flight estimate was too low."
                        ),
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )
        return outcome
