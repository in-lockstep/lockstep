"""GATE-JUDGE-1: a rubric is parsed, and a verdict handed in settles it.

A rubric nobody judged stays `outstanding`; a judged one is passed or failed with the judge's
reason; `pass_rate` falls the first time a judged rubric fails. `grade` does not care who the
judge was -- a model, a person, a sidecar -- which is what lets the deciding half be tested with
no model at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from in_lockstep.core.improve import Answered, JudgeAsk, Verdict
from in_lockstep.evaluation.cases import Case, CaseError, Rubric, grade, summarize
from in_lockstep.improver import CorpusImprover, arm_of, verdict_of


def test_a_rubric_is_parsed_from_either_spelling_and_refused_when_no_answer_could_settle_it() -> None:
    """The sentence the harvested corpus writes, and the object the shipped corpus writes: one
    criterion or several, a scale as a number or as anchored rungs, and a bar. Refused at parse,
    before a judge is ever paid, when the result could not depend on the answer."""
    bare = Rubric.parse("Names the mechanism.")
    assert bare.criteria == ("Names the mechanism.",) and (bare.levels, bare.minimum) == (5, 4)
    shipped = Rubric.parse(
        {
            "criteria": "Reports the gap.",
            "levels": {"5": "names what is absent", "3": "vague", "1": "invents"},
            "min": 4,
        }
    )
    assert shipped.levels == 5 and shipped.minimum == 4
    assert shipped.anchors == ((1, "invents"), (3, "vague"), (5, "names what is absent"))
    assert shipped.as_record()["anchors"][0] == [1, "invents"]
    several = Rubric.parse({"criteria": ["a", "b"], "levels": 3, "minimum": 2})
    assert several.criteria == ("a", "b") and several.settles(2) and not several.settles(1)

    for bad, why in (
        ("   ", "no text"),
        ({"criteria": "x", "min": 6}, "outside"),
        ({"criteria": "x", "levels": 3, "min": 0}, "outside"),
        ({"criteria": "x", "levels": 0}, "scale no answer"),
        ({"criteria": "x", "levels": {"five": "?"}}, "not a rung"),
        ({"criteria": "x", "min": "4"}, "integer"),
        ({"criteria": "x", "min": 3, "minimum": 3}, "one bar"),
        ({"criteria": [], "min": 3}, "no criteria"),
        ({"criteria": "x", "grader": "me"}, "no judge reads"),
        (7, "sentence or an object"),
    ):
        with pytest.raises(CaseError, match=why):
            Rubric.parse(bad, name="bad")
    with pytest.raises(CaseError, match="outside"):
        Case.parse({"expect": {"rubric": {"criteria": "x", "min": 9}}}, name="bad")


def test_gate_judge_1_an_unjudged_rubric_is_outstanding_and_a_verdict_settles_it_either_way() -> None:
    """GATE-JUDGE-1. Without a verdict the rubric is outstanding and the suite decides nothing;
    with one it is passed or failed by the bar, with the judge's reason, and `pass_rate` falls
    the first time a judged rubric fails. A level off the scale is not a verdict."""
    case = Case(name="c", expect={"rubric": {"criteria": "names the mechanism", "min": 4}})
    waiting = grade(case, {"findings": []})
    assert waiting["rubric_outstanding"] is True and waiting["rubric_passed"] is None
    assert summarize([waiting])["pass_rate"] is None

    passed = grade(
        case, {"findings": []}, {"level": 5, "reason": "it names the lock", "evidence": ["the lock"]}
    )
    assert passed["rubric_outstanding"] is False and passed["rubric_passed"] is True
    assert passed["rubric_level"] == 5 and passed["rubric_reason"] == "it names the lock"
    assert passed["rubric_evidence"] == ["the lock"]
    failed = grade(case, {"findings": []}, {"level": 2, "reason": "it names the symptom"})
    assert failed["rubric_passed"] is False and failed["rubric_reason"] == "it names the symptom"

    summary = summarize([passed, failed])
    assert summary == {
        "total": 2,
        "decided": 2,
        "outstanding": 0,
        "judged": 2,
        "passed": 1,
        "pass_rate": 0.5,
        "ok": False,
    }
    assert summarize([passed])["pass_rate"] == 1.0

    off = grade(case, {}, {"level": 9, "reason": "?"})
    assert off["rubric_outstanding"] is True and "not a level" in off["rubric_reason"]
    assert grade(case, {}, {"level": True})["rubric_outstanding"] is True

    # A verdict for a case with no rubric is an answer to a question nobody asked.
    plain = Case(name="p", expect={"contains": ["x"]})
    assert grade(plain, "x", {"level": 5})["rubric_passed"] is None


def test_gate_judge_1_both_halves_must_agree_for_a_case_to_pass() -> None:
    """A judged-failed rubric beside a passed deterministic half is a failed case, and a judged
    rubric beside an unsettled deterministic half decides the case alone."""
    both = Case(name="b", expect={"contains": ["x"], "rubric": "sensible"})
    mixed = grade(both, "x", {"level": 1, "reason": "no"})
    assert mixed["deterministic_passed"] is True and mixed["rubric_passed"] is False
    assert summarize([mixed])["passed"] == 0 and summarize([mixed])["ok"] is False
    alone = Case(name="a", expect={"rubric": "sensible"})
    judged = grade(alone, "anything", {"level": 4, "reason": "fine"})
    assert judged["deterministic_passed"] is None
    assert summarize([judged]) == {
        "total": 1,
        "decided": 1,
        "outstanding": 0,
        "judged": 1,
        "passed": 1,
        "pass_rate": 1.0,
        "ok": True,
    }
    # Checks passed and the rubric unjudged: half an answer is still outstanding, not decided.
    half = grade(both, "x")
    assert summarize([half])["decided"] == 0 and summarize([half])["outstanding"] == 1


HEADER = "---\nname: security-reviewer\ndescription: a test body\n---\n"
BODY = "You review one pull request for security, and for nothing else.\n\nName the mechanism.\n"


def _case(root: Path, name: str, *, rubric: Any, contains: tuple[str, ...] = ()) -> None:
    from in_lockstep.ai.replay import key_of, request_from

    request = {
        "model": "stub:model-1",
        "system": "<!-- g -->\n" + BODY + "\n<!-- s -->",
        "messages": [{"role": "user", "content": "diff"}],
    }
    case: dict[str, Any] = {
        "input": {"request": request},
        "expect": {**({"contains": list(contains)} if contains else {}), "rubric": rubric},
        "recorded": {
            "content": json.dumps({"findings": [{"summary": "the session lock"}]}),
            "tool_calls": [],
            "usage": {},
            "stop_reason": "end_turn",
        },
        "harvested": {
            "cassette": "gone",
            "filed_under": "k",
            "model": "stub:model-1",
            "key": key_of(request_from(request)),
        },
    }
    (root / "review").mkdir(parents=True, exist_ok=True)
    (root / "review" / f"{name}.json").write_text(json.dumps(case))


def test_gate_judge_1_the_improver_asks_one_question_per_arm_and_a_verdict_settles_that_arm(
    tmp_path: Path,
) -> None:
    """`rubrics` yields the before arm's recorded answer and the after arm's probe answer as two
    asks keyed by content hash; `score(..., verdicts=)` folds each verdict into its arm, and the
    verdict of the comparison reads both halves: a draft whose rubric the judge failed regresses
    a case the recorded answer passed, whatever the deterministic checks said."""
    from in_lockstep.core.improve import IMPROVED, REGRESSED, Improvable

    corpus = tmp_path / "cases"
    _case(corpus, "judged", rubric={"criteria": "names the mechanism", "min": 4}, contains=("lock",))
    _case(corpus, "plain", rubric="sensible")
    body = Improvable(
        body="house/security.md", verb="review", label="review/security", answers=("review.security",)
    )
    improver = CorpusImprover(corpus)
    baseline = improver.baseline(body, HEADER + BODY)
    assert sorted(baseline.cases) == ["judged", "plain"]
    assert baseline.arm.outstanding == 2 and baseline.arm.judged == 0

    answers = [
        Answered(case="judged", content=json.dumps({"findings": [{"summary": "a lock"}]})),
        Answered(case="plain", content="fine"),
    ]
    asks = improver.rubrics(baseline, answers)
    assert [(a.case, a.arm) for a in asks] == [
        ("judged", "before"),
        ("judged", "after"),
        ("plain", "before"),
        ("plain", "after"),
    ]
    assert all(
        isinstance(a, JudgeAsk) and len(a.rubric_sha256) == 64 and len(a.answer_sha256) == 64 for a in asks
    )
    before, after = asks[0], asks[1]
    assert before.rubric_sha256 == after.rubric_sha256 and before.answer_sha256 != after.answer_sha256
    assert before.rubric == {"criteria": ["names the mechanism"], "levels": 5, "min": 4, "anchors": []}

    unjudged = improver.score(baseline, answers)
    assert unjudged.before.outstanding == 2 and unjudged.after.judged == 0

    verdicts = [
        Verdict("judged", "before", 5, "names the lock", judge="test"),
        Verdict("judged", "after", 2, "names the symptom", judge="test"),
        Verdict("plain", "before", 3, "meh", judge="test"),
        Verdict("plain", "after", 5, "good", judge="test"),
    ]
    scored = improver.score(baseline, answers, verdicts=verdicts)
    assert (scored.before.judged, scored.after.judged) == (2, 2)
    assert (scored.before.passed, scored.before.failed) == (1, 1)
    assert (scored.after.passed, scored.after.failed) == (1, 1)
    assert scored.verdict == REGRESSED, (
        "the judged case the recorded answer passed and the draft failed decides it"
    )
    assert any(
        f.case == "judged" and f.check == "rubric" and "symptom" in f.detail for f in scored.after.failures
    )
    assert scored.as_record()["after"]["judged"] == 2
    assert type(scored).from_record(scored.as_record()).after.judged == 2

    better = improver.score(baseline, answers, verdicts=[v for v in verdicts if v.case == "plain"])
    assert better.verdict == IMPROVED and better.after.outstanding == 1


def test_arm_and_verdict_helpers_read_judged_rubrics(tmp_path: Path) -> None:
    case = Case(name="c", expect={"rubric": "x"})
    outstanding = grade(case, "a")
    good = grade(case, "a", {"level": 5, "reason": "r"})
    bad = grade(case, "a", {"level": 1, "reason": "r"})
    arm = arm_of([outstanding, good, bad])
    assert (arm.measured, arm.passed, arm.failed, arm.outstanding, arm.judged) == (3, 1, 1, 1, 2)
    assert verdict_of([good], [bad]) == "regressed" and verdict_of([bad], [good]) == "improved"
    assert verdict_of([outstanding], [good]) == "unchanged", "an outstanding half cannot flip a verdict"
