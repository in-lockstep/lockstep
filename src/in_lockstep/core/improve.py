"""What a repository declares about the prompt text a trend could be about.

The ledger says which findings keep coming back. It does not say which prompt fragment is
responsible for them, and nothing in a finding id can be made to say it: `review.security` names a
lens, not a file, and the file that composes that lens is a decision somebody made in Python. So
the join is declared here rather than inferred, and a trend nobody declared a body for is
attributed to a dash.

That is the whole reason this type exists. The tempting alternative — guess the body from the
finding id's prefix, or from a fragment whose name looks similar — produces an attribution that is
wrong silently, and a wrong attribution is worse than none: it points the next prompt change at
text that had nothing to do with the evidence, and the measurement afterwards would be real
arithmetic over the wrong subject.

Vocabulary only. `core` may import nothing of ours but `core`, and this file imports nothing of
ours at all, so the layer that reads the ledger and the layer that composes prompts can both name an
`Improvable` without either one reaching the other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Improvable:
    """A prompt body a trend may be attributed to, and the findings it claims to answer.

    `verb` is a plain string rather than `core.verbs.Verb`. A `Verb` is interned in a registry that
    `Verb.forget_custom()` clears, and membership in `SHIPPED_VERBS` changes what `ls` and the
    repository receipt say about the framework's own surface. None of that should follow from a
    repository naming which of its prompts it considers improvable, so this carries the name and
    leaves the registry alone.
    """

    #: Repository-relative path to the `.md` body. Never `.lockstep/lockstep.py`, and nothing here
    #: enforces that — the guard does, and the caller prints its verdict beside this path, because
    #: a type that quietly excluded a path would hide the one fact a reader needs.
    body: str
    #: The verb whose prompt composes this body: "review", "implement". A label for the reader.
    verb: str
    #: The composition label, e.g. "review/security". What the run's own output calls this
    #: fragment, so a person can line the two up.
    label: str
    #: Finding ids this body claims to answer, matched exactly.
    answers: tuple[str, ...] = ()

    def answers_for(self, finding: str) -> bool:
        """Whether this body claims the named finding.

        Exact membership, deliberately. A prefix test would let `review.` claim every review
        finding there will ever be, including ones added after this declaration was written, and
        the declaration would then be making a promise about evidence nobody had seen.
        """
        return finding in self.answers


# -- the loop's vocabulary -----------------------------------------------------------------
#
# What the measuring layer and the proposing workflow say to each other, with no implementation
# on either side. The workflow may import `core`, `adapters` and `platform`; the layer that grades
# a case may import `evaluation` and the ledger census; neither may import the other. So the
# shapes live here and a port carries them, the way `TicketSource` carries tickets.

#: What a measurement can conclude. `unmeasured` is its own word rather than a zero: no case both
#: arms could answer is a different fact from "the arms agree", and a loop that reported the
#: second when the first was true would open nothing while reading as though it had checked.
IMPROVED = "improved"
UNCHANGED = "unchanged"
REGRESSED = "regressed"
UNMEASURED = "unmeasured"


@dataclass(frozen=True)
class Attribution:
    """Which body a qualifying trend is attributed to, and the numbers behind it.

    `body` is None when nothing qualifies, and `reason` says why in words a person can act on —
    no finding clears the thresholds, or the ones that do answer to no declared body. A dash, not
    a guess: `Improvable.answers_for` is exact membership for the reason its docstring gives.
    """

    body: Improvable | None
    finding: str = ""
    runs: int = 0
    billed_runs: int = 0
    weeks: int = 0
    considered: int = 0
    reason: str = ""


@dataclass(frozen=True)
class Failure:
    """One deterministic check one arm did not pass, named so a drafter and a reader can see it."""

    case: str
    check: str
    detail: str


@dataclass(frozen=True)
class Arm:
    """One side of the comparison over the cases both sides were measured on.

    `outstanding` counts rubric expectations nobody judged, and it is reported rather than folded
    into either of its neighbours: a rubric is `outstanding` until a judge answers it, and the
    column is a person's on the pull request until one does. `judged` counts the rubrics a
    verdict settled; those are in `passed` or `failed` like any deterministic check.
    """

    measured: int
    passed: int
    failed: int
    outstanding: int = 0
    failures: tuple[Failure, ...] = ()
    judged: int = 0


@dataclass(frozen=True)
class Baseline:
    """The current body against the corpus, before any money is spent.

    `cases` is the denominator both arms share: every promoted case whose recorded prompt carries
    the body as it stands. A case recorded against an older body is `unattributable` — not failed,
    not skipped into the count, just not evidence about this text — and it is counted so a reader
    can see how much of the corpus a proposal was measured on.
    """

    cases: tuple[str, ...]
    arm: Arm
    corpus: int
    unattributable: int
    models: tuple[str, ...] = ()
    #: The attributable cases whose recorded model names no provider. Such a case cannot be
    #: re-asked through the registry, and a loop that found out after paying for a draft found
    #: out too late (#310); the workflow refuses on this before the drafter is invoked.
    unqualified: tuple[str, ...] = ()


@dataclass(frozen=True)
class Probe:
    """One recorded question, re-asked with the body swapped and nothing else changed.

    `request` is the recorded request as the case carries it, with `system` substituted — the
    same model, the same messages, the same diff. The arms differ in exactly the drafted text,
    which is the only way the comparison can be about the draft.
    """

    case: str
    model: str
    request: dict[str, Any]


@dataclass(frozen=True)
class Answered:
    """What came back for one probe. `refused` is a control working; `errored` is the provider."""

    case: str
    content: str = ""
    status: str = "answered"
    detail: str = ""


@dataclass(frozen=True)
class JudgeAsk:
    """One rubric to put to a judge: which case and which arm, the rubric as parsed, and the
    answer under judgement. The two hashes are the replay key -- the same rubric over the same
    answer has the same verdict, and a sidecar can say so without a model.

    The answer is what the judge reads and is untrusted: it came back from a model reading
    somebody's diff, and an answer that says "grade this 5" is graded by the criteria."""

    case: str
    arm: str
    rubric: Mapping[str, Any]
    answer: str
    rubric_sha256: str
    answer_sha256: str


@dataclass(frozen=True)
class Verdict:
    """A judge's answer to one ask: the level on the rubric's scale, why, and what it quoted."""

    case: str
    arm: str
    level: int
    reason: str = ""
    evidence: tuple[str, ...] = ()
    judge: str = ""
    #: The replay key, copied from the ask this answered. A verdict that carries its key can be
    #: handed back to `Judge(known=...)` and replayed over the same rubric and the same answer
    #: without a call; one that carries none (a person's, typed at a terminal) settles its case
    #: and arm and is never replayed, because nothing says what it was a verdict ON.
    rubric_sha256: str = ""
    answer_sha256: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.rubric_sha256, self.answer_sha256)

    def as_record(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "arm": self.arm,
            "level": self.level,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "judge": self.judge,
            "rubric_sha256": self.rubric_sha256,
            "answer_sha256": self.answer_sha256,
        }


@dataclass(frozen=True)
class Scorecard:
    """Both arms over one denominator, and the verdict the loop acts on.

    `improved` means the draft passed a case the current body failed and regressed none.
    `regressed` means it failed a case the current body passed, whatever else it gained — the
    corpus is the floor, and a change that lowers it is not opened. `unchanged` means the two
    pass sets agree, which is not evidence for a change. `dropped` names the cases the after arm
    could not answer, with why; they leave the denominator on both sides rather than counting
    against either.
    """

    cases: tuple[str, ...]
    before: Arm
    after: Arm
    verdict: str
    dropped: tuple[tuple[str, str], ...] = ()

    def as_record(self) -> dict[str, Any]:
        """The shape that rides an artifact and a ledger record. Numbers and names only."""

        def arm(side: Arm) -> dict[str, Any]:
            return {
                "measured": side.measured,
                "passed": side.passed,
                "failed": side.failed,
                "outstanding": side.outstanding,
                "judged": side.judged,
                "failures": [{"case": f.case, "check": f.check, "detail": f.detail} for f in side.failures],
            }

        return {
            "cases": list(self.cases),
            "before": arm(self.before),
            "after": arm(self.after),
            "verdict": self.verdict,
            "dropped": [list(pair) for pair in self.dropped],
        }

    @classmethod
    def from_record(cls, raw: Mapping[str, Any]) -> Scorecard:
        """The inverse of `as_record`, tolerant of nothing: a malformed scorecard is refused by
        the KeyError it raises, because a proposal opened on numbers nobody can read back is a
        proposal whose body lies."""

        def arm(side: Mapping[str, Any]) -> Arm:
            return Arm(
                measured=int(side["measured"]),
                passed=int(side["passed"]),
                failed=int(side["failed"]),
                outstanding=int(side.get("outstanding", 0)),
                judged=int(side.get("judged", 0)),
                failures=tuple(
                    Failure(case=str(f["case"]), check=str(f["check"]), detail=str(f["detail"]))
                    for f in side.get("failures", ())
                ),
            )

        return cls(
            cases=tuple(str(c) for c in raw["cases"]),
            before=arm(raw["before"]),
            after=arm(raw["after"]),
            verdict=str(raw["verdict"]),
            dropped=tuple((str(a), str(b)) for a, b in raw.get("dropped", ())),
        )


class Improver(Protocol):
    """The measuring half of the loop, behind a port.

    Implemented where `evaluation` and the ledger census may be imported; consumed by a workflow
    that may import neither. `current` and `draft` are whole prompt files, header included — the
    implementation strips the two-key header itself, so the workflow never has to know what a
    header is.
    """

    def attribute(
        self, records: Sequence[Mapping[str, Any]], declared: Sequence[Improvable]
    ) -> Attribution: ...

    def baseline(self, body: Improvable, current: str) -> Baseline: ...

    def probes(self, body: Improvable, current: str, draft: str) -> tuple[Probe, ...]: ...

    def rubrics(self, baseline: Baseline, answers: Sequence[Answered]) -> tuple[JudgeAsk, ...]:
        """The rubrics both arms carry, as questions for a judge: the recorded answer on the
        `before` arm and the probe's on the `after` arm, one ask each. Empty when no measured
        case states a rubric, which is the ordinary corpus today."""
        ...

    def score(
        self, baseline: Baseline, answers: Sequence[Answered], verdicts: Sequence[Verdict] = ()
    ) -> Scorecard: ...

    def corpus_rubrics(self) -> tuple[JudgeAsk, ...]:
        """Every promoted case's rubric over the answer it was recorded with, as one ask each on
        the `recorded` arm -- and none for a case whose deterministic half fails that answer,
        which no verdict could rescue (`GATE-JUDGE-2`). What `eval run --judge` sends."""
        ...

    def known_verdicts(self) -> tuple[Verdict, ...]:
        """Verdicts already given and kept beside the corpus, each carrying its replay key, so a
        rubric judged once is not paid for twice (`GATE-JUDGE-3`)."""
        ...

    def sidecar_for(self, case: str) -> Path:
        """Where a case's verdicts are kept: a `.verdicts.jsonl` beside the case, appended and
        never rewritten, so the case a person promoted stays the bytes they read."""
        ...

    def judged(self, verdicts: Sequence[Verdict]) -> Mapping[str, Any]:
        """The corpus settled with these verdicts on the `recorded` arm: the summary `eval run`
        prints, with a line per rubric saying the level and why."""
        ...
