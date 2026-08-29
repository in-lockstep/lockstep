"""Phase-6 gates: the ported knowledge, strategies, and eval identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from in_lockstep.ai.strategy import (
    StrategyRefused,
    StrategyRegistry,
    UnknownStrategy,
)
from in_lockstep.core.outcome import Outcome, Status
from in_lockstep.core.verbs import Verb
from in_lockstep.evaluation import load_cases, subject_for, summarize
from in_lockstep.evaluation.cases import Case, CaseError, grade
from in_lockstep.strategies import default_registry

PROMPTS = Path(__file__).resolve().parents[2] / "src" / "in_lockstep" / "prompts"
CORPUS = Path(__file__).resolve().parents[2] / "src" / "in_lockstep" / "corpus"


# -- the ported corpus ---------------------------------------------------------------


def test_every_shipped_family_has_prompts() -> None:
    families = {p.name for p in PROMPTS.iterdir() if p.is_dir() and p.name not in ("skills", "__pycache__")}
    assert families == {"review", "implement", "fix", "triage", "retro"}


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


def test_the_shipped_strategies_register_with_defaults() -> None:
    registry = default_registry()
    assert registry.select(Verb.REVIEW).id == "review/security"
    # The default is the executable one. `implement/tdd` and `implement/direct` are catalogue
    # entries whose factories return a string, and defaulting to one would make `ctx.do(Implement,
    # ...)` with no explicit choice refuse — with the registry's own default as the reason.
    assert registry.select(Verb.IMPLEMENT).id == "implement/oneshot"
    assert len(registry.for_verb(Verb.REVIEW)) == 4, "an aspect is an agent, not a data row"


def test_an_explicit_choice_wins_over_the_default() -> None:
    registry = default_registry()
    assert registry.select(Verb.REVIEW, explicit="review/intent").id == "review/intent"


def test_gate_guard_3_a_privileged_strategy_is_unreachable_from_untrusted_input() -> None:
    """Ticket labels can steer selection, and the improver holds a grant on prompts/."""
    registry = default_registry()
    with pytest.raises(StrategyRefused, match="attacker-influenceable"):
        registry.select(Verb.IMPLEMENT, explicit="improve/propose", from_untrusted_input=True)


def test_a_privileged_strategy_is_reachable_from_an_explicit_selection() -> None:
    registry = default_registry()
    assert registry.select(Verb.IMPLEMENT, explicit="improve/propose").id == "improve/propose"


def test_an_unregistered_strategy_names_what_exists() -> None:
    registry = StrategyRegistry()
    with pytest.raises(UnknownStrategy, match="no strategy"):
        registry.select(Verb.REVIEW, explicit="review/nope")


def test_defaulting_to_an_unregistered_strategy_is_refused() -> None:
    with pytest.raises(UnknownStrategy):
        StrategyRegistry().default(Verb.REVIEW, "nothing")
