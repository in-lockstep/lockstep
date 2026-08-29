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


class UndeclaredBudget(Exception):
    """A lifecycle binds something that spends money and never said how much.

    The substrate this replaced made a missing budget a **compile-time error**: `DOC006` was
    `Severity.ERROR`, so shipping an agent without a ceiling was structurally impossible. Moving
    invocation in-process deleted that, and porting it into an advisory `doctor` check would have
    turned a refusal into a suggestion — which is not a port, it is a downgrade with the same name.

    So it refuses at startup instead, and it refuses for the *right* runs: a lifecycle binding only
    deterministic verbs spends nothing and needs no ceiling. The trigger is a bound adapter
    declaring `Capability.SPENDS_BUDGET`, which is the same declaration egress, approval and retry
    already key on.
    """


class DailySpendExceeded(Exception):
    """This repository's runs have already spent today's ceiling; refused before this one starts.

    The rolling per-repository window the substrate's per-day partition used to provide, rebuilt
    from the ledger's own records — which item 17's timestamps made possible. Weaker than the
    original on purpose and honestly: the sum is of what THIS clone's ledger has seen, so a fresh
    CI runner starts from whatever history the checkout carries and concurrent runs race the
    read. A SHARED-store compare-and-set is the declared upgrade path when cross-machine truth is
    actually needed; the provider-side organisation limit (`DOC101`) remains the durable backstop.
    """

    reason = "cost.daily_exceeded"


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

    @property
    def declared(self) -> bool:
        """Whether any ceiling was set. All-`None` is the default, and the default is not a budget."""
        return any(v is not None for v in (self.usd, self.tokens, self.wall_seconds, self.turns))

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
