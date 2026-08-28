"""Run-scoped spend accounting, checked before the money is spent.

Placed in `core/` and hung on `RunContext` rather than held by the AI subsystem, for two reasons.
It has to be checkable from inside an agentic loop — a whole loop is one action call, so a
middleware check at the action boundary only fires once the turns are already paid for, and the
loop re-sends its accumulated message list every turn, which makes cost quadratic in turns rather
than linear. And a fan-out shares one budget: branches multiply spend, so the ceiling is joint,
never per-branch.

It is deliberately not a module-level singleton. The upstream token tracker was one, which meant
no run scoping and cross-stamped labels the moment two runs overlapped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .outcome import Cost


class Unpriced(Exception):
    """A model with no rate. Refused before the call, never billed at a default rate.

    Pricing an unrecognised model at some default is wrong in both directions: it overcharges a
    local model and undercharges a frontier one, and it produces a number that looks like
    evidence.
    """


@dataclass(frozen=True)
class Budget:
    """A ceiling. `None` means unset, so merging several takes the lowest rather than the last."""

    usd: float | None = None
    tokens: int | None = None
    wall_seconds: float | None = None
    turns: int | None = None

    def merge(self, other: Budget) -> Budget:
        """Two ceilings are two constraints: take the lowest, never the last."""
        return Budget(
            usd=_lowest_float(self.usd, other.usd),
            tokens=_lowest_int(self.tokens, other.tokens),
            wall_seconds=_lowest_float(self.wall_seconds, other.wall_seconds),
            turns=_lowest_int(self.turns, other.turns),
        )


def _lowest_float(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _lowest_int(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


@dataclass
class Spend:
    """What this run has spent, and whether it may spend more."""

    budget: Budget = field(default_factory=Budget)
    charged: Cost = field(default_factory=Cost)
    turns: int = 0

    def charge(self, cost: Cost) -> None:
        self.charged = self.charged + cost

    def charge_turn(self, cost: Cost) -> None:
        self.charge(cost)
        self.turns += 1

    def remaining_usd(self) -> float | None:
        if self.budget.usd is None:
            return None
        return max(0.0, self.budget.usd - self.charged.usd)

    def exceeded(self) -> str | None:
        """Which ceiling has already been crossed, if any."""
        b = self.budget
        if b.usd is not None and self.charged.usd > b.usd:
            return f"usd:{self.charged.usd:.4f}>{b.usd:.4f}"
        if b.tokens is not None and self.charged.total_tokens > b.tokens:
            return f"tokens:{self.charged.total_tokens}>{b.tokens}"
        if b.wall_seconds is not None and self.charged.wall_seconds > b.wall_seconds:
            return f"wall:{self.charged.wall_seconds:.1f}>{b.wall_seconds:.1f}"
        if b.turns is not None and self.turns > b.turns:
            return f"turns:{self.turns}>{b.turns}"
        return None

    def would_exceed(self, projected: Cost) -> str | None:
        """The predictive check. Asked BEFORE a call, with the projected cost of making it.

        The projection must bound output by the request's max_tokens rather than by an expected
        value: a single turn that returns its full allowance would otherwise overshoot a ceiling
        that was checked against an average.
        """
        b = self.budget
        after = self.charged + projected
        if b.usd is not None and after.usd > b.usd:
            return f"usd:{after.usd:.4f}>{b.usd:.4f}"
        if b.tokens is not None and after.total_tokens > b.tokens:
            return f"tokens:{after.total_tokens}>{b.tokens}"
        if b.turns is not None and self.turns + 1 > b.turns:
            return f"turns:{self.turns + 1}>{b.turns}"
        return None
