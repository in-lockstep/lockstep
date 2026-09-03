"""Phase-6 gates: the ported knowledge, strategies, and eval identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.evaluation import load_cases, subject_for, summarize
from in_lockstep.evaluation.cases import Case, CaseError, grade

PROMPTS = Path(__file__).resolve().parents[2] / "src" / "in_lockstep" / "prompts"
CORPUS = Path(__file__).resolve().parents[2] / "src" / "in_lockstep" / "corpus"


# -- the ported corpus ---------------------------------------------------------------


def test_every_shipped_family_has_prompts() -> None:
    families = {p.name for p in PROMPTS.iterdir() if p.is_dir() and p.name not in ("skills", "__pycache__")}
    assert families == {"review", "implement", "fix", "backport", "triage", "rfe", "retro"}


def test_the_eval_corpus_came_across_intact() -> None:
    """27 cases, which is what solves the cold-start problem for a new adopter."""
    cases = load_cases(CORPUS)
    assert len(cases) == 27


def test_every_case_expects_something() -> None:
    """A case with no expectation cannot fail, and would sit in the corpus looking like coverage."""
    for case in load_cases(CORPUS):
        assert case.expect, f"{case.name} expects nothing"


def test_a_case_expecting_something_nothing_checks_is_refused() -> None:
    with pytest.raises(CaseError, match="unknown expectation"):
        Case.parse({"expect": {"vibes": "good"}}, name="bad")


def test_a_case_with_no_expectation_is_refused() -> None:
    with pytest.raises(CaseError, match="cannot fail"):
        Case.parse({"expect": {}}, name="empty")


# -- GATE-EVAL-2 / #194: a count expectation no answer could satisfy ------------------


def test_a_case_expecting_at_least_one_passes_on_an_answer_that_found_one() -> None:
    """What twelve shipped cases already mean by "found something". The check compared a length to
    the expectation with `==`, so an int met a dict, and every one of them was pinned at failed
    whatever the model answered (#194)."""
    case = Case(name="c", expect={"count": {"findings": {"min": 1}}})
    assert grade(case, {"findings": [{"path": "a"}]})["deterministic_passed"] is True


def test_at_least_one_is_still_refused_by_an_answer_that_found_nothing() -> None:
    """The negative control. A comparator satisfied by every answer is the same defect wearing the
    other face, and it is the one a careless fix produces."""
    case = Case(name="c", expect={"count": {"findings": {"min": 1}}})
    assert grade(case, {"findings": []})["deterministic_passed"] is False


def test_at_most_n_refuses_the_answer_that_exceeds_it() -> None:
    case = Case(name="c", expect={"count": {"findings": {"max": 2}}})
    assert grade(case, {"findings": [1, 2]})["deterministic_passed"] is True
    assert grade(case, {"findings": [1, 2, 3]})["deterministic_passed"] is False


def test_a_plain_number_still_means_exactly_that_many() -> None:
    """Eight shipped cases count exactly, and several of them count zero: `a-window-too-thin-to-read`
    expects `findings: 0` and means it. Reading the exact form as a floor would turn those into the
    cases that cannot fail."""
    case = Case(name="c", expect={"count": {"findings": 0}})
    assert grade(case, {"findings": []})["deterministic_passed"] is True
    assert grade(case, {"findings": [{"path": "a"}]})["deterministic_passed"] is False


def test_no_shipped_case_states_a_count_that_no_answer_could_satisfy() -> None:
    """GATE-EVAL-2, over the corpus that broke it. `Case.parse` already refuses an expectation
    nothing checks, on the grounds that it always passes; a comparator no length satisfies is that
    hole from the other side, and it held five of the nine review cases -- the only family `trial`
    drives -- at failed for every possible answer.

    Checked by search rather than by reading: an expectation is satisfiable if some length passes
    it, so a comparator added later that nothing implements fails here even if nobody thought to
    write a case for it."""
    for case in load_cases(CORPUS):
        for field, want in (case.deterministic.get("count") or {}).items():
            satisfied_by = [
                n
                for n in range(0, 12)
                if grade(Case(name=case.name, expect={"count": {field: want}}), {field: [None] * n})[
                    "deterministic_passed"
                ]
            ]
            assert satisfied_by, f"{case.name}: no answer satisfies count.{field} = {want!r}"


def test_a_count_comparator_nothing_implements_is_refused() -> None:
    """The shape check the key check already had. `{"at_least": 1}` reads like it means something,
    and silently meant "this case fails forever"."""
    with pytest.raises(CaseError, match="count"):
        Case.parse({"expect": {"count": {"findings": {"at_least": 1}}}}, name="bad")


def test_a_count_of_something_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(CaseError, match="count"):
        Case.parse({"expect": {"count": {"findings": "several"}}}, name="bad")


# -- GATE-OUT-2: outstanding is not passed -------------------------------------------


def test_gate_out_2_an_unjudged_rubric_is_outstanding_not_passed() -> None:
    case = Case(name="c", expect={"rubric": "Says something sensible."})
    result = grade(case, {"findings": []})
    assert result["rubric_outstanding"] is True

    summary = summarize([result])
    assert summary["pass_rate"] is None, (
        "a perfect score computed from no evidence would land in a baseline and be compared against forever"
    )
    assert summary["outstanding"] == 1
    assert summary["ok"] is True, "a run is not blocked by its own honesty"


def test_a_deterministic_case_is_decided() -> None:
    case = Case(name="c", expect={"schema": ["findings"]})
    summary = summarize([grade(case, {"findings": []})])
    assert summary["decided"] == 1
    assert summary["pass_rate"] == 1.0


def test_a_failing_deterministic_case_is_not_ok() -> None:
    case = Case(name="c", expect={"count": {"findings": 0}})
    summary = summarize([grade(case, {"findings": [{"path": "a"}]})])
    assert summary["ok"] is False
    assert summary["pass_rate"] == 0.0


def test_an_undecided_outcome_is_distinguishable_from_a_cache_hit() -> None:
    """Both are 'not a failure'. Only one of them decided anything."""
    undecided = Outcome(status=Status.SUCCEEDED, decided=False)
    cache_hit = Outcome.skipped()
    assert (undecided.status, undecided.decided) != (cache_hit.status, cache_hit.decided)


# -- GATE-LEDGER-4 / GATE-EVAL-1: identity ------------------------------------------


def _subject(prompt: str = "body", skills: tuple[str, ...] = ("s1",)) -> object:
    return subject_for(
        verb="review",
        strategy_id="review/security",
        composed_prompt=prompt,
        skills=skills,
        context_recipe=("diff",),
        model_id="anthropic:claude-sonnet-4-6",
        prompt_id="SecurityReviewPrompt",
        prompt_version="1",
    )


def test_gate_ledger_4_a_guardrail_change_moves_the_subject_key() -> None:
    """Even though prompt_id@version is unchanged — which is why identity is a content hash."""
    before = _subject(prompt="guardrail A\n\nbody")
    after = _subject(prompt="guardrail B\n\nbody")
    assert before.prompt_version == after.prompt_version
    assert before.key != after.key


def test_gate_eval_1_a_skill_body_change_moves_the_subject_key() -> None:
    """Skill bodies load by progressive disclosure, so they are NOT in the composed text.

    Without hashing them separately, editing a skill leaves the key unchanged and its effect is
    measured as noise — the same failure a content hash was chosen to prevent, from the other side.
    """
    assert _subject(skills=("v1",)).key != _subject(skills=("v2",)).key


def test_identical_inputs_produce_an_identical_key() -> None:
    assert _subject().key == _subject().key


def test_the_declared_version_is_carried_for_display_not_identity() -> None:
    subject = _subject()
    assert "SecurityReviewPrompt" in subject.label()
    assert subject.prompt_id not in subject.key


# -- strategies -----------------------------------------------------------------------
#
# The StrategyRegistry and its GATE-GUARD-3 check (a privileged strategy unreachable from
# untrusted input) were deleted when strategies became directly bindable adapters: which strategy
# runs is a bind-time code decision in lockstep.py, so there is no selection an attacker-
# influenceable input could steer. The gate is structural now rather than a registry refusal.
