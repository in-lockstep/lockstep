"""Failure is data.

A red test run is a domain outcome, not an exception. Workflows branch on `Outcome`; exceptions
are reserved for programmer error, so a failing suite never unwinds the stack.

The taxonomy exists because alerting and control flow need it — one "failure" bucket cannot
distinguish a red test from a provider 500, and those page differently.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, ClassVar, Generic, TypeVar

ValueT = TypeVar("ValueT")


class Status(Enum):
    """Closed at five members. Deliberately.

    `UNDECIDED` is not here: "how did it end" and "did it produce evidence" are orthogonal
    questions, and an unjudged rubric is a fully successful run that decided nothing. Folding it
    into a status would make it look like a way of ending rather than an absence of evidence —
    which is exactly the reassuring-number failure the eval contract exists to prevent. Evidence
    lives on `Outcome.decided`, which also composes under fan-out (`all(o.decided)`) where a
    status member would not.

    `SKIPPED` is not here either, and was. It was reserved to a `Cache` middleware — "a cache hit
    yields SKIPPED, decided" — and the reservation was the entire mechanism: no such middleware
    was ever written, `Outcome.skipped()` had no caller under `src/`, and a member of a closed
    taxonomy that nothing produces is a promise the ledger and the metric were being asked to
    honour on no evidence (#267, `GATE-OUT-2`). Removed rather than kept "in case", by the same
    rule that retired the `Retry` middleware: surface nobody asked for is surface to remove. An
    adapter with nothing to do says so as a `BLOCKED` refusal with a reason, or a `SUCCEEDED`
    that decided nothing, both of which the ledger already tells apart.

    `PARKED` was reserved at 1.0 with no producer, by a recorded deferral rather than a comment
    (`design/in-lockstep-design.md` §17.11), because adding to a closed enum later is breaking
    and keeping a member a decision reserved is not. It has producers now: `ctx.park` and a
    `fan_out` with a human branch (`GATE-OUT-6`). Not terminal, on purpose: a barrier answers "is
    everyone done", and a branch waiting on a person is not.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"  # the domain said no: tests red, review rejected. Routable data.
    ERRORED = "errored"  # infrastructure broke. Retryable, alertable.
    BLOCKED = "blocked"  # policy or a gate stopped it. Neither failure nor error.
    PARKED = "parked"  # ended at a human boundary with a continuation registered.

    @property
    def terminal(self) -> bool:
        """Whether a barrier may treat this branch as done. Completion is not success."""
        return self in (
            Status.SUCCEEDED,
            Status.FAILED,
            Status.ERRORED,
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

    #: A finding's message is frequently model output, and a ledger record is meant to stay
    #: diffable. Long enough for a real explanation, short enough that one pathological run cannot
    #: write a page into a permanent record.
    MAX_RECORDED_MESSAGE: ClassVar[int] = 500

    def as_record(self) -> dict[str, object]:
        """The durable form. The type owns its serialization, as `injection.Finding` already does."""
        message = self.message
        if len(message) > self.MAX_RECORDED_MESSAGE:
            message = message[: self.MAX_RECORDED_MESSAGE] + "…[truncated]"
        record: dict[str, object] = {
            "id": self.id,
            "message": message,
            "severity": self.severity.value,
            "blocking": self.blocking,
        }
        # Omitted rather than written empty: a record saying `path: ""` reads as a finding about
        # the repository root, which is a different claim from one about nothing in particular.
        if self.path:
            record["path"] = self.path
        if self.line is not None:
            record["line"] = self.line
        return record


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
    # How many of these tokens were actually paid for. Zero when the answer came from a cassette
    # or a canned reply: the tokens are real — reproducing them is what a replay is FOR — and the
    # money is not. See `billed_fraction`.
    #
    # A count rather than a boolean, for the same reason `priced_tokens` is one: costs add, and a
    # run that mixes a live call with a replayed one has a fraction rather than a flag.
    billed_tokens: int = 0

    def beyond(self, other: Cost) -> Cost:
        """What of this cost `other` does not already hold, field by field, never below zero.

        Exists for one caller: `RunContext.do`, which charges an outcome's cost onto the run's
        `Spend` -- and an AI adapter has already charged that same `Spend` turn by turn through
        the invoker it was handed. Charging the outcome again counted every model call twice
        (GATE-COST-7). What is charged is the outcome's cost beyond what the `Spend` moved during
        the call: for a deterministic adapter that is the whole cost, for an AI adapter it is
        nothing but the wall clock the invoker did not measure.
        """
        return Cost(
            input_tokens=max(0, self.input_tokens - other.input_tokens),
            output_tokens=max(0, self.output_tokens - other.output_tokens),
            cache_read_tokens=max(0, self.cache_read_tokens - other.cache_read_tokens),
            cache_write_tokens=max(0, self.cache_write_tokens - other.cache_write_tokens),
            usd=max(0.0, self.usd - other.usd),
            wall_seconds=max(0.0, self.wall_seconds - other.wall_seconds),
            priced_tokens=max(0, self.priced_tokens - other.priced_tokens),
            billed_tokens=max(0, self.billed_tokens - other.billed_tokens),
        )

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
    def billed_fraction(self) -> float | None:
        """What share of this actually cost money. `None` when nothing was billable.

        This exists because `usd: 0.0` is ambiguous and one of its readings is the fabrication
        this module already refuses elsewhere: a comfortable zero standing in for a model whose
        price was never known. A replayed run has real tokens and no cost, and without a second
        number saying so it is indistinguishable from a run that was mispriced to nothing.

        It also keeps a ledger honest across many runs. A repository replaying a cassette on every
        pull request would otherwise accumulate the recording's cost as though it were spent, and
        any trend built on that reads replays as money.
        """
        if self.billable_tokens == 0:
            return None
        return self.billed_tokens / self.billable_tokens

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
            billed_tokens=self.billed_tokens + other.billed_tokens,
        )


#: How a set of outcomes is read as one: any parked makes the whole parked -- a run waiting on a
#: person has not ended, whatever its other branches did -- else any blocked, else any errored,
#: else any failed, else succeeded. `RunContext.verdict()` reads a run's steps this way and
#: `JoinResult.status` reads a fan-out's branches the same way, and it is defined once, here,
#: because two precedences that could drift apart is how a run and its branches would come to
#: disagree about what happened.
VERDICT_PRECEDENCE: tuple[Status, ...] = (Status.PARKED, Status.BLOCKED, Status.ERRORED, Status.FAILED)


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
        """A control refused this run. Nothing was judged, so `decided` defaults to False.

        `decided` defaulted to True here, which said a blocked run had settled something. It had
        not: a budget ceiling, an approval gate or an egress refusal stops the work from
        happening, so there is no verdict to have reached. `undecided_rate` counted
        `decided is False` and therefore counted none of them, and every blocked run read as a run
        that decided (#256).

        A caller may still pass `decided=True` — a control that fires *after* a real verdict has
        one to keep — which is why this is a default rather than an override.
        """
        kw.setdefault("decided", False)
        return cls(status=Status.BLOCKED, reason=reason, **kw)


@dataclass(frozen=True)
class JoinResult:
    """What a `fan_out` came to: branch name -> the branch's `Outcome`, read as one.

    A mapping and not a status, because the barrier answers "is everyone done" and what the mix
    means is the continuation's decision (design §4.7): `status` is the precedence a run's steps
    already use, `decided` is `all(...)` -- one branch that judged nothing is a join that judged
    nothing (`GATE-OUT-3`) -- `cost` is the sum, and `as_outcome()` is for the workflow that has
    no more to say than the join did. Frozen, like `Outcome`, so a continuation reads the same
    result the barrier produced.
    """

    branches: tuple[tuple[str, Outcome[Any]], ...] = ()

    def __getitem__(self, name: str) -> Outcome[Any]:
        for branch, outcome in self.branches:
            if branch == name:
                return outcome
        raise KeyError(name)

    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.branches)

    def outcomes(self) -> tuple[Outcome[Any], ...]:
        return tuple(outcome for _, outcome in self.branches)

    @property
    def decided(self) -> bool:
        return all(outcome.decided for outcome in self.outcomes())

    @property
    def status(self) -> Status:
        for status in VERDICT_PRECEDENCE:
            if any(outcome.status is status for outcome in self.outcomes()):
                return status
        return Status.SUCCEEDED

    @property
    def reason(self) -> str | None:
        """The deciding branch's reason: the first branch whose status decided the join's."""
        status = self.status
        for _, outcome in self.branches:
            if outcome.status is status and outcome.reason:
                return outcome.reason
        return None

    @property
    def cost(self) -> Cost:
        total = Cost()
        for outcome in self.outcomes():
            total = total + outcome.cost
        return total

    def as_outcome(self) -> Outcome[JoinResult]:
        """The join as one `Outcome`: every branch's findings, the summed cost, the deciding
        branch's reason, `decided` only if every branch decided, and the join itself as value."""
        return Outcome(
            status=self.status,
            value=self,
            findings=tuple(f for outcome in self.outcomes() for f in outcome.findings),
            cost=self.cost,
            reason=self.reason,
            decided=self.decided,
        )
