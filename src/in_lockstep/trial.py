"""Measuring a pack before trusting it, for nothing.

The step that makes a catalog worth anything. A listing can be checked — `imports`, capabilities,
a receipt that matches the installed code — but none of that says whether the pack is any *good*,
and the honest answer to that is a measurement somebody makes on their own cases rather than a
number the author published.

Two pieces already existed and this joins them. Cassettes sit at the `LLMInput`/`LLMOutput` seam
and replay deterministically with no key and no spend. A corpus states what a good answer looks
like, splitting what a machine can settle from what needs a judge. A trial runs the second against
the first.

What it reports is bounded by what it can honestly claim, and the bound is stated in the output
rather than hidden in the arithmetic:

**A case with no recorded exchange is `unrecorded`, never failed.** A cassette holds the answers
its author recorded; a case they did not record is one this cannot run, which is an absence of
evidence and not evidence of absence. Counting it as a failure would let a pack look bad for the
author's incomplete recording, and counting it as a pass would be worse.

**A rubric is `outstanding` until a judge answers it.** That is `evaluation/`'s contract and this
does not get to soften it: a suite reporting 100% while half of it was never decided is the
reassuring number computed from no evidence.

**A family this cannot drive is `not exercised`, and is counted.** Only `review` runs today. A
trial that silently dropped the cases it could not run would report a pass rate over a corpus it
never saw, which is the same failure with better manners.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evaluation import Case, load_cases
from .evaluation.cases import grade
from .packs import Pack

#: The corpus families a trial knows how to run. `review` is a single model turn with the diff in
#: the prompt, which is exactly what a cassette replays; the agentic verbs interleave tool calls
#: whose results a cassette also holds, and driving those honestly is more than this does today.
DRIVEN = ("review",)

DECIDED = "decided"
OUTSTANDING = "outstanding"
UNRECORDED = "unrecorded"
NOT_EXERCISED = "not exercised"
ERRORED = "errored"


@dataclass(frozen=True)
class CaseResult:
    """One case, and what became of it. `passed` is `None` unless something was settled."""

    case: str
    family: str
    origin: str  # "pack" or "yours"
    state: str
    passed: bool | None = None
    detail: str = ""


@dataclass(frozen=True)
class Trial:
    results: tuple[CaseResult, ...] = ()
    cassettes: tuple[str, ...] = ()

    def summary(self, origin: str = "") -> dict[str, Any]:
        """Counts, and a pass rate over what was actually decided — `None` when nothing was.

        `None` rather than `1.0`, and rather than `0.0`: a rate computed over an empty denominator
        is the number this whole module exists to refuse to print.
        """
        scoped = [r for r in self.results if not origin or r.origin == origin]
        decided = [r for r in scoped if r.state == DECIDED and r.passed is not None]
        return {
            "cases": len(scoped),
            "decided": len(decided),
            "outstanding": sum(1 for r in scoped if r.state == OUTSTANDING),
            "unrecorded": sum(1 for r in scoped if r.state == UNRECORDED),
            "not_exercised": sum(1 for r in scoped if r.state == NOT_EXERCISED),
            "errored": sum(1 for r in scoped if r.state == ERRORED),
            "pass_rate": (sum(1 for r in decided if r.passed) / len(decided)) if decided else None,
        }


def aspect_of(case: Case) -> str:
    """Which review lens a case belongs to, from where it sits.

    `corpus/review/security-reviewer/*.json` is the shipped shape, so the directory names the lens
    and the suffix is prose. A convention, and the same one the pack layout uses to pair
    `prompts/<aspect>.md` with `corpus/review/<aspect>-reviewer/` — stated in `docs/extending.md`
    rather than inferred, because a pack author has to be able to satisfy it deliberately.
    """
    if case.path is None:
        return ""
    directory = case.path.parent.name
    return directory[: -len("-reviewer")] if directory.endswith("-reviewer") else directory


def family_of(case: Case) -> str:
    return case.path.parent.parent.name if case.path else "?"


def collect(pack: Pack, extra: Path | None = None) -> list[tuple[Case, str]]:
    """The pack's own cases and yours, kept apart.

    Apart because they answer different questions. A pack's corpus says what its author decided it
    should do; yours says whether it does what *you* need — and the second is the number worth
    installing on, which is the whole argument for measuring locally rather than reading a badge.
    """
    cases: list[tuple[Case, str]] = []
    own = pack.corpus()
    if own is not None:
        cases += [(case, "pack") for case in load_cases(own)]
    if extra is not None and extra.is_dir():
        cases += [(case, "yours") for case in load_cases(extra)]
    return cases


def run(
    pack: Pack,
    *,
    extra: Path | None = None,
    invoker_factory: Any,
    lenses: Any = None,
) -> Trial:
    """Run every case a trial can drive, and account for every case it cannot.

    `invoker_factory` is the seam: a replaying one costs nothing, a recording one is how a pack
    author produces the cassette in the first place. Nothing here decides which — a module that
    chose would be a module that could quietly spend money.
    """
    from .adapters.ai import AiReview

    adapter = AiReview(invoker_factory, lenses=lenses)
    results: list[CaseResult] = []

    for case, origin in collect(pack, extra):
        family = family_of(case)
        if family not in DRIVEN:
            results.append(
                CaseResult(
                    case=case.name,
                    family=family,
                    origin=origin,
                    state=NOT_EXERCISED,
                    detail=f"nothing here drives the {family!r} family yet",
                )
            )
            continue
        results.append(_review_case(adapter, case, origin, family))

    return Trial(results=tuple(results), cassettes=tuple(pack.cassettes()))


def _review_case(adapter: Any, case: Case, origin: str, family: str) -> CaseResult:
    from .adapters.ai import Review
    from .core.outcome import Status

    aspect = aspect_of(case)
    diff = str(case.input.get("diff", ""))
    if not diff:
        return CaseResult(
            case=case.name,
            family=family,
            origin=origin,
            state=ERRORED,
            detail="a review case needs `input.diff`; there is nothing to review",
        )

    request = Review(base="HEAD~1", head="HEAD", aspect=aspect, diff=diff)
    # The adapter directly, not `ctx.do`: a trial measures one adapter, and dispatching would put
    # the repository's middleware — its budget, its approval gate, its tracing — around a
    # measurement that is not a run of anything.
    try:
        outcome = asyncio.run(adapter.invoke(_TrialContext(), request))
    except LookupError as e:
        # What `ReplayProvider` raises for a request its cassette does not hold. Caught by the
        # exception rather than by reading a reason string, because this is the single most
        # important state to get right: a case the author never recorded is one this could not
        # run, and calling that a failure would let a pack look bad for an incomplete recording.
        return CaseResult(case=case.name, family=family, origin=origin, state=UNRECORDED, detail=str(e))

    if outcome.status is not Status.SUCCEEDED:
        return CaseResult(
            case=case.name,
            family=family,
            origin=origin,
            state=ERRORED,
            detail=outcome.reason or outcome.status.value,
        )

    graded = grade(case, _as_output(outcome.value))
    if graded["deterministic_passed"] is None:
        return CaseResult(
            case=case.name,
            family=family,
            origin=origin,
            state=OUTSTANDING,
            detail="a judge has to answer this",
        )
    return CaseResult(
        case=case.name,
        family=family,
        origin=origin,
        state=DECIDED,
        passed=bool(graded["deterministic_passed"]),
        detail="; ".join(
            f"{check['check']} {check['detail']}".strip() for check in graded["checks"] if not check["passed"]
        ),
    )


def _as_output(report: Any) -> dict[str, Any]:
    """A report as the shape a case's expectations are written against.

    `contains` searches the JSON of this, so what a case can assert on is what a reviewer would
    read: the findings and the verdict, not the object graph that carried them.
    """
    findings = getattr(report, "findings", ()) or ()
    return {
        "findings": [
            {
                "path": getattr(finding, "path", ""),
                "line": getattr(finding, "line", 0),
                "summary": getattr(finding, "summary", ""),
                "detail": getattr(finding, "detail", ""),
                "severity": getattr(finding, "severity", ""),
            }
            for finding in findings
        ],
        "verdict": getattr(report, "verdict", ""),
        "aspect": getattr(report, "aspect", ""),
    }


class _TrialContext:
    """The least context an adapter needs, and deliberately not a `RunContext`.

    A trial is a measurement, not a run: it leaves no ledger record, charges no budget it did not
    declare, and must not look like a lifecycle execution in telemetry. Spend is still real —
    replaying costs nothing, and recording costs whatever the model costs — so a `Spend` is
    carried and the invoker's own per-turn check reads it.
    """

    def __init__(self) -> None:
        from .core.context import RepoInfo
        from .core.spend import Budget, Spend

        self.repo = RepoInfo(root=str(Path.cwd()))
        self.spend = Spend(budget=Budget())
        self.models: dict[str, str] = {}
        self.run_id = "trial"

    async def do(self, request: Any, via: Any = None) -> Any:  # pragma: no cover - not used
        raise NotImplementedError("a trial invokes an adapter directly; it does not dispatch")


def render(trial: Trial, *, pack: str, recording: bool) -> list[str]:
    """The human form. Every state is printed, including the ones that are absences."""
    lines: list[str] = []
    overall = trial.summary()
    mode = (
        "recording — this calls the provider and costs money" if recording else "replaying — no key, no spend"
    )
    lines.append(f"trial         {pack}  ({mode})")
    lines.append(
        f"cassettes     {len(trial.cassettes)}"
        + (f"  ({', '.join(trial.cassettes)})" if trial.cassettes else "  — nothing to replay")
    )
    lines.append("")

    for origin, label in (("pack", "its own cases"), ("yours", "your cases")):
        scoped = trial.summary(origin)
        if not scoped["cases"]:
            continue
        lines.append(f"{label}  ({scoped['cases']})")
        lines.append(
            f"  decided     {scoped['decided']}"
            + (f"    pass rate {scoped['pass_rate']:.2f}" if scoped["pass_rate"] is not None else "")
        )
        if scoped["outstanding"]:
            lines.append(f"  outstanding {scoped['outstanding']}  — need a judge, so not passes")
        if scoped["unrecorded"]:
            lines.append(f"  unrecorded  {scoped['unrecorded']}  — no recorded exchange; not failures")
        if scoped["not_exercised"]:
            lines.append(f"  unexercised {scoped['not_exercised']}  — a family a trial cannot drive yet")
        if scoped["errored"]:
            lines.append(f"  errored     {scoped['errored']}")
        lines.append("")

    failures = [r for r in trial.results if r.state == DECIDED and r.passed is False]
    if failures:
        lines.append("did not pass")
        for result in failures:
            lines.append(f"  {result.origin}/{result.case}  {result.detail}")
        lines.append("")

    if overall["pass_rate"] is None:
        lines.append(
            "nothing was decided, so there is no pass rate. That is the honest answer and not a "
            "zero:\n  a corpus nobody recorded cannot be replayed, and a rubric nobody judged is "
            "outstanding."
        )
    return lines


def as_json(trial: Trial) -> str:
    return json.dumps(
        {
            "cassettes": list(trial.cassettes),
            "summary": trial.summary(),
            "pack": trial.summary("pack"),
            "yours": trial.summary("yours"),
            "cases": [
                {
                    "case": r.case,
                    "family": r.family,
                    "origin": r.origin,
                    "state": r.state,
                    "passed": r.passed,
                    "detail": r.detail,
                }
                for r in trial.results
            ],
        },
        sort_keys=True,
        indent=2,
    )
