"""The measuring half of the learning loop, behind the `Improver` port.

Three questions, each answered from the corpus and the ledger and nothing else:

  * **Which body is a qualifying trend attributed to?** The census `improve --explain` prints, read
    the same way: a trend counts when it clears both thresholds on BILLED runs, and it is attributed
    to a body by exact membership in what that body declares it answers. Nothing is inferred from
    the shape of a finding id.
  * **Which promoted cases are evidence about this body?** The ones whose recorded prompt carries
    the body's text verbatim. A case recorded against an older body is not evidence about this one
    -- not failed, not skipped into the denominator, counted as `unattributable` so a reader can see
    how much of the corpus a proposal was measured on.
  * **Did the draft improve on the current body?** Both arms graded on the same cases with the same
    grader `eval run` uses, over the raw answer. `improved` is a case the current body fails and the
    draft passes, with nothing lost; `regressed` is anything lost, whatever was gained.

The last question is the one worth being precise about, because a harvested case's expectations
were derived from the answer it was harvested with, so the current body passes it by construction.
What makes a case one the current body FAILS is a person: they read the answer, decided what a
correct one would have contained, and tightened the expectations to say so. That is the labelling
act this loop learns from, and it is why `improve` refuses to spend on a corpus with nothing to
improve -- a draft measured against a corpus at its ceiling can only stay level or fall.

This module may import `evaluation` and the census, which the workflow that consumes it may not.
That is the reason it exists as a layer rather than as part of the workflow.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import metrics
from .ai.prompt import parse_frontmatter
from .core.improve import (
    IMPROVED,
    REGRESSED,
    UNCHANGED,
    UNMEASURED,
    Answered,
    Arm,
    Attribution,
    Baseline,
    Failure,
    Improvable,
    JudgeAsk,
    Probe,
    Scorecard,
    Verdict,
)
from .evaluation import Case, load_cases
from .evaluation.cases import Rubric, grade


class CorpusImprover:
    """`Improver` over a directory of promoted cases -- `evidence/cases/` here."""

    def __init__(self, corpus: str | Path) -> None:
        self.corpus = Path(corpus)

    def attribute(self, records: Sequence[Mapping[str, Any]], declared: Sequence[Improvable]) -> Attribution:
        trends = metrics.recurring([dict(r) for r in records])
        if not trends:
            return Attribution(
                None, reason="the ledger records no finding at all; a run that finds nothing writes no id"
            )
        considered = trends[0].considered
        qualifying = [t for t in trends if t.qualifies]
        if not qualifying:
            return Attribution(
                None,
                considered=considered,
                reason=(
                    f"nothing recurs yet: no finding id clears both thresholds (min_runs "
                    f"{trends[0].min_runs} billed, min_weeks {trends[0].min_weeks}) over {considered} "
                    f"record(s)"
                ),
            )
        # Strongest qualifying trend first -- `recurring` sorts by runs -- and the FIRST declared
        # body that answers it, so a lifecycle's declaration order breaks a tie between two bodies
        # claiming one id, visibly, rather than a dict's.
        for trend in qualifying:
            for body in declared:
                if body.answers_for(trend.finding):
                    return Attribution(
                        body,
                        finding=trend.finding,
                        runs=trend.runs,
                        billed_runs=trend.billed_runs,
                        weeks=trend.weeks,
                        considered=considered,
                    )
        names = ", ".join(t.finding for t in qualifying)
        return Attribution(
            None,
            considered=considered,
            reason=(
                f"{len(qualifying)} finding id(s) qualify ({names}) and none answers to a declared "
                f"Improvable, so the trend is attributed to a dash"
            ),
        )

    def baseline(self, body: Improvable, current: str) -> Baseline:
        text = _body_text(current)
        cases = load_cases(self.corpus)
        mine = [c for c in cases if carries(c, text)]
        results = [grade(c, as_answer(str(c.recorded.get("content", "")))) for c in mine]
        return Baseline(
            cases=tuple(c.name for c in mine),
            arm=arm_of(results),
            corpus=len(cases),
            unattributable=len(cases) - len(mine),
            models=tuple(sorted({_model_of(c) for c in mine if _model_of(c)})),
            unqualified=tuple(c.name for c in mine if ":" not in _model_of(c)),
        )

    def probes(self, body: Improvable, current: str, draft: str) -> tuple[Probe, ...]:
        before, after = _body_text(current), _body_text(draft)
        out: list[Probe] = []
        for case in load_cases(self.corpus):
            if not carries(case, before):
                continue
            request = dict(case.input["request"])
            # One substitution, of the body's text and nothing around it. The guardrails and
            # skills in the recorded prompt stay as they were RECORDED, not as this build composes
            # them -- if they have moved since, that movement is not what is being measured.
            request["system"] = str(request.get("system", "")).replace(before, after, 1)
            out.append(Probe(case=case.name, model=_model_of(case), request=request))
        return tuple(out)

    def rubrics(self, baseline: Baseline, answers: Sequence[Answered]) -> tuple[JudgeAsk, ...]:
        """One ask per case per arm whose deterministic half passed -- and none for an arm that
        failed it. Deterministic first (O7, `GATE-JUDGE-2`): `verdict_of` already reads a failed
        check as a failed case whatever a judge would say, so a verdict on that arm could change
        nothing, and a model call that cannot change the answer is spend on nothing."""
        by_case = {a.case: a for a in answers}
        wanted = set(baseline.cases)
        asks: list[JudgeAsk] = []
        for case in load_cases(self.corpus):
            rubric = case.rubric
            if case.name not in wanted or rubric is None or not rubric.scored:
                continue
            recorded = str(case.recorded.get("content", ""))
            if _deterministic_holds(case, recorded):
                asks.append(_ask(case.name, "before", rubric, recorded))
            answer = by_case.get(case.name)
            if (
                answer is not None
                and answer.status == "answered"
                and _deterministic_holds(case, answer.content)
            ):
                asks.append(_ask(case.name, "after", rubric, answer.content))
        return tuple(asks)

    def score(
        self, baseline: Baseline, answers: Sequence[Answered], verdicts: Sequence[Verdict] = ()
    ) -> Scorecard:
        by_case = {a.case: a for a in answers}
        # A verdict settles the rubric of one case on one arm; the rest stay outstanding.
        settled = {(v.case, v.arm): v for v in verdicts}
        wanted = set(baseline.cases)
        cases = {c.name: c for c in load_cases(self.corpus) if c.name in wanted}
        measured: list[str] = []
        dropped: list[tuple[str, str]] = []
        before: list[dict[str, Any]] = []
        after: list[dict[str, Any]] = []
        for name in baseline.cases:
            answer = by_case.get(name)
            if answer is None:
                dropped.append((name, "no answer came back for this case"))
                continue
            if answer.status != "answered":
                dropped.append((name, answer.detail or answer.status))
                continue
            case = cases[name]
            before.append(
                grade(
                    case,
                    as_answer(str(case.recorded.get("content", ""))),
                    _verdict_of(settled.get((name, "before"))),
                )
            )
            after.append(grade(case, as_answer(answer.content), _verdict_of(settled.get((name, "after")))))
            measured.append(name)
        return Scorecard(
            cases=tuple(measured),
            before=arm_of(before),
            after=arm_of(after),
            verdict=verdict_of(before, after),
            dropped=tuple(dropped),
        )


def _deterministic_holds(case: Case, content: str) -> bool:
    """Whether the checks a script can settle did not fail on this answer. A case with no checks
    holds: there was nothing to fail, and the rubric is the only question it asks."""
    return grade(case, as_answer(content))["deterministic_passed"] is not False


def _ask(case: str, arm: str, rubric: Rubric, answer: str) -> JudgeAsk:
    record = rubric.as_record()
    return JudgeAsk(
        case=case,
        arm=arm,
        rubric=record,
        answer=answer,
        rubric_sha256=hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest(),
        answer_sha256=hashlib.sha256(answer.encode()).hexdigest(),
    )


def _verdict_of(verdict: Verdict | None) -> dict[str, Any] | None:
    if verdict is None:
        return None
    return {"level": verdict.level, "reason": verdict.reason, "evidence": list(verdict.evidence)}


def carries(case: Case, body_text: str) -> bool:
    """Whether a promoted case is evidence about this body: its recorded prompt holds the text
    verbatim, and it carries the answer it was harvested with."""
    request = (case.input or {}).get("request")
    if not isinstance(request, dict) or not body_text:
        return False
    return body_text in str(request.get("system", "")) and bool(case.recorded.get("content"))


def as_answer(content: str) -> Any:
    """A recorded reply as the grader sees it -- the same reading `eval run` makes."""
    try:
        return json.loads(content)
    except ValueError:
        return content


def arm_of(results: Sequence[Mapping[str, Any]]) -> Arm:
    """One side, from graded results. `passed` and `failed` are the deterministic verdicts; a
    rubric nobody judged is `outstanding` and is in neither."""
    failures = tuple(
        Failure(case=str(r["case"]), check=str(c["check"]), detail=str(c["detail"]))
        for r in results
        if r["deterministic_passed"] is False
        for c in r["checks"]
        if not c["passed"]
    )
    # A judged rubric that failed is a failure of the arm, named like a check (GATE-JUDGE-1).
    failures += tuple(
        Failure(case=str(r["case"]), check="rubric", detail=str(r.get("rubric_reason", "")))
        for r in results
        if r.get("rubric_passed") is False
    )
    return Arm(
        measured=len(results),
        passed=sum(1 for r in results if _settled(r) and _passed(r)),
        failed=sum(1 for r in results if _settled(r) and not _passed(r)),
        outstanding=sum(1 for r in results if r.get("rubric_outstanding")),
        failures=failures,
        judged=sum(1 for r in results if r.get("rubric_passed") is not None),
    )


def _settled(result: Mapping[str, Any]) -> bool:
    """A case one half of which said something and the other half of which is not still waiting."""
    return not result.get("rubric_outstanding") and (
        result.get("deterministic_passed") is not None or result.get("rubric_passed") is not None
    )


def _passed(result: Mapping[str, Any]) -> bool:
    return result.get("deterministic_passed") is not False and result.get("rubric_passed") is not False


def verdict_of(before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]) -> str:
    """The comparison, case by case over one denominator.

    Any case the current body passes and the draft fails is a regression, and a regression
    decides the verdict whatever else the draft gained: the corpus is the floor. Only when nothing
    was lost does a case that flipped the other way count as an improvement.
    """
    if not before:
        return UNMEASURED
    pairs = list(zip(before, after, strict=True))

    def outcome(r: Mapping[str, Any]) -> bool | None:
        # A settled case is passed or failed, both halves considered; an outstanding one is
        # neither and cannot flip a verdict in either direction.
        return _passed(r) if _settled(r) else None

    if any(outcome(b) is True and outcome(a) is False for b, a in pairs):
        return REGRESSED
    if any(outcome(b) is False and outcome(a) is True for b, a in pairs):
        return IMPROVED
    return UNCHANGED


def _body_text(text: str) -> str:
    _, body = parse_frontmatter(text)
    return body.strip()


def _model_of(case: Case) -> str:
    request = (case.input or {}).get("request")
    recorded = str((case.harvested or {}).get("model", ""))
    return recorded or (str(request.get("model", "")) if isinstance(request, dict) else "")
