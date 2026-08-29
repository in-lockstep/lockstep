"""Failure is data.

A red test run is a domain outcome, not an exception. Workflows branch on `Outcome`; exceptions
are reserved for programmer error, so a failing suite never unwinds the stack.

The taxonomy exists because alerting and control flow need it — one "failure" bucket cannot
distinguish a red test from a provider 500, and those page differently.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Generic, TypeVar

ValueT = TypeVar("ValueT")


class Status(Enum):
    """Closed at six members. Deliberately.

    `UNDECIDED` is not here: "how did it end" and "did it produce evidence" are orthogonal
    questions, and an unjudged rubric is a fully successful run that decided nothing. Folding it
    into SKIPPED would make it indistinguishable from a cache hit — which is exactly the
    reassuring-number failure the eval contract exists to prevent. Evidence lives on
    `Outcome.decided`, which also composes under fan-out (`all(o.decided)`) where a seventh
    status member would not.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"  # the domain said no: tests red, review rejected. Routable data.
    ERRORED = "errored"  # infrastructure broke. Retryable, alertable.
    BLOCKED = "blocked"  # policy or a gate stopped it. Neither failure nor error.
    SKIPPED = "skipped"  # cache hit or conditional bypass. Reserved to Cache.
    PARKED = "parked"  # ended at a human boundary with a continuation registered.

    @property
    def terminal(self) -> bool:
        """Whether a barrier may treat this branch as done. Completion is not success."""
        return self in (
            Status.SUCCEEDED,
            Status.FAILED,
            Status.ERRORED,
            Status.SKIPPED,
            Status.BLOCKED,
        )


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"
    NOTE = "note"


@dataclass(frozen=True)
class Finding:
    """Something an action noticed. Blocking findings gate continuation; the rest are reported."""

    id: str  # stable, greppable: "cost.unpriced_model", "guard.protected_path"
    message: str
    severity: Severity = Severity.WARNING
    path: str = ""
    line: int | None = None
    blocking: bool = False


@dataclass(frozen=True)
class ArtifactRef:
    kind: str
    content_hash: str
    ref: str = ""


@dataclass(frozen=True)
class Cost:
    """What an action consumed. Tokens are measured; dollars are derived.

    An unpriced model is refused before the call rather than recorded as free, so `usd` is never
    a comfortable zero standing in for "we did not recognise the model name".
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    wall_seconds: float = 0.0
    # How many billable tokens were priced from a rate that was actually declared, rather than
    # from a substitution. See `priced_fraction`.
    priced_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def billable_tokens(self) -> int:
        """Every token a provider charges for, cache included.

        Distinct from `total_tokens`, which is the input/output pair the usage metric reports.
        Cache tokens cost money and belong in the denominator of a claim about pricing coverage.
        """
        return self.total_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def priced_fraction(self) -> float | None:
        """What share of this cost came from a declared rate. `None` when nothing was billable.

        `None` rather than `1.0`, and that is the whole gate. A run that spent no tokens has not
        achieved complete pricing coverage — it has no coverage to report, and `1.0` is a
        reassuring number computed from an empty denominator. It is the same distinction
        `Outcome.decided` draws and the same one `evaluation.summarize` draws by returning
        `pass_rate: None`; a metric that reads perfect when nothing happened is how a broken
        pipeline looks healthy.
        """
        if self.billable_tokens == 0:
            return None
        return self.priced_tokens / self.billable_tokens

    def __add__(self, other: Cost) -> Cost:
        return Cost(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            usd=self.usd + other.usd,
            wall_seconds=self.wall_seconds + other.wall_seconds,
            priced_tokens=self.priced_tokens + other.priced_tokens,
        )


@dataclass(frozen=True)
class Outcome(Generic[ValueT]):
    status: Status
    value: ValueT | None = None
    findings: tuple[Finding, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    cost: Cost = field(default_factory=Cost)
    # A stable machine code refining the status: "expired", "deadline", "killswitch",
    # "unpriced_model". Carried so that every future refinement is a reason rather than a new
    # status member — adding to a closed enum breaks every exhaustive match and the ledger schema.
    reason: str | None = None
    # Did this produce evidence? True for everything that decides something, including failures.
    # False only where a judgement was expected and never made. Meaningless-but-True for
    # deterministic verbs, which is a wart accepted deliberately: the alternative is a status
    # member that does not compose.
    decided: bool = True

    @property
    def succeeded(self) -> bool:
        return self.status is Status.SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.status is Status.FAILED

    @property
    def blocked(self) -> bool:
        return self.status is Status.BLOCKED

    @property
    def terminal(self) -> bool:
        return self.status.terminal

    @property
    def blocking_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.blocking)

    def with_cost(self, cost: Cost) -> Outcome[ValueT]:
        return replace(self, cost=cost)

    def with_findings(self, *findings: Finding) -> Outcome[ValueT]:
        return replace(self, findings=(*self.findings, *findings))

    # -- constructors, so call sites read as intent rather than as enum plumbing --

    @classmethod
    def succeeded_with(cls, value: ValueT, **kw: Any) -> Outcome[ValueT]:
        return cls(status=Status.SUCCEEDED, value=value, **kw)

    @classmethod
    def failed_with(cls, value: ValueT | None = None, **kw: Any) -> Outcome[ValueT]:
        return cls(status=Status.FAILED, value=value, **kw)

    @classmethod
    def errored(cls, reason: str, **kw: Any) -> Outcome[ValueT]:
        return cls(status=Status.ERRORED, reason=reason, **kw)

    @classmethod
    def blocked_by(cls, reason: str, **kw: Any) -> Outcome[ValueT]:
        return cls(status=Status.BLOCKED, reason=reason, **kw)

    @classmethod
    def skipped(cls, reason: str = "cache", **kw: Any) -> Outcome[ValueT]:
        return cls(status=Status.SKIPPED, reason=reason, **kw)
