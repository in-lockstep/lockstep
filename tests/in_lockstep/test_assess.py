"""Green is not done: a change is assessed against the ticket, by a model that did not write it.

GATE-ASSESS-1. TDD proves a staged test went from red to green, which is arithmetic and is why it
is deterministic. Whether the change ANSWERS the ticket is judgement, and until this verb nothing
asked it: #450 satisfied its own tests, skipped an acceptance criterion outright -- "the syntax
clause is in the refusal a person actually reads" -- and wrote a test asserting the omission as a
requirement. Green throughout. A person reading the issue beside the diff caught it.

Driven with a stub invoker rather than a model: what is asserted is what the verb does with an
answer, which is where every defect of this kind lives.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from in_lockstep.adapters.ai.assess import AiAssess
from in_lockstep.ai.invoker import Invocation
from in_lockstep.core.outcome import Status
from in_lockstep.core.types import Assess, AssessReport, CriterionVerdict

CRITERIA = ("the refusal says where the trailer goes", "the two refusals differ")


class _Stub:
    """An invoker that answers with whatever JSON the test hands it.

    The real `Invocation`, not a stand-in: `settle` reads `truncated` off it, and a fake missing a
    field is a test that fails for a reason the code under test did not cause.
    """

    model = "stub:assessor"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0

    async def run(self, **_: Any) -> Invocation:
        self.calls += 1
        return Invocation(content=self.answer)


def _ctx() -> Any:
    return type("_Ctx", (), {"repo": type("R", (), {"root": "."})()})()


def _assess(answer: str, *, criteria: tuple[str, ...] = CRITERIA, change: str = "a.py:\nx = 1\n") -> Any:
    stub = _Stub(answer)
    adapter = AiAssess(invoker_factory=lambda _ctx: stub)
    outcome = asyncio.run(
        adapter.invoke(_ctx(), Assess(criteria=criteria, change=change, ticket="#444", title="t"))
    )
    return outcome, stub


def _answer(*pairs: tuple[str, bool, str]) -> str:
    import json

    return json.dumps(
        {"verdicts": [{"criterion": c, "met": m, "reason": r} for c, m, r in pairs], "summary": "s"}
    )


# -- the report's own arithmetic -------------------------------------------------------


def test_gate_assess_1_an_assessment_of_no_verdicts_is_not_met() -> None:
    """Absent is not met. An empty report is what a malformed reply, a refused assessor and a
    ticket with no criteria all produce, and every one of them would be a change waved through if
    `met` were vacuously true over an empty list."""
    assert AssessReport().met is False


def test_a_report_is_met_only_when_every_criterion_is() -> None:
    """The positive control beside it: `met` has to be capable of being True, or the test above
    passes over a property that is simply always false."""
    both = (CriterionVerdict("a", True), CriterionVerdict("b", True))
    one = (CriterionVerdict("a", True), CriterionVerdict("b", False))

    assert AssessReport(verdicts=both).met is True
    assert AssessReport(verdicts=one).met is False
    assert AssessReport(verdicts=one).unmet == (CriterionVerdict("b", False),)


# -- what the verb does with an answer -------------------------------------------------


def test_gate_assess_1_a_criterion_the_assessor_did_not_answer_is_unmet() -> None:
    """Silence is not assent, and this is the failure mode with teeth: an assessor that returns
    three verdicts for four criteria would otherwise have the fourth read as met by omission."""
    outcome, _ = _assess(_answer((CRITERIA[0], True, "yes")))

    report = outcome.value
    assert outcome.status is Status.SUCCEEDED
    assert len(report.verdicts) == 2, "one verdict per criterion asked, not per verdict returned"
    assert report.met is False
    missing = next(v for v in report.verdicts if v.criterion == CRITERIA[1])
    assert missing.met is False
    assert "no verdict" in missing.reason


def test_verdicts_are_paired_by_criterion_and_not_by_position() -> None:
    """An assessor answering out of order must not have its verdicts attached to the wrong
    criteria. Position is the obvious implementation and it is wrong the first time a model
    reorders its reply."""
    outcome, _ = _assess(_answer((CRITERIA[1], False, "no"), (CRITERIA[0], True, "yes")))

    by_criterion = {v.criterion: v.met for v in outcome.value.verdicts}
    assert by_criterion == {CRITERIA[0]: True, CRITERIA[1]: False}


def test_a_verdict_for_a_criterion_nobody_asked_about_is_dropped() -> None:
    """The other direction: an assessor inventing a criterion does not get to add one, and it
    certainly does not get to make the report longer than the list it was handed."""
    outcome, _ = _assess(
        _answer((CRITERIA[0], True, "y"), (CRITERIA[1], True, "y"), ("a criterion nobody wrote", True, "y"))
    )

    assert [v.criterion for v in outcome.value.verdicts] == list(CRITERIA)
    assert outcome.value.met is True


def test_a_reply_naming_no_criterion_at_all_is_blocked_rather_than_met() -> None:
    """A reply in shape that settled nothing is the loudest possible version of this defect:
    reporting it as met would wave a change through on an assessment that did not happen."""
    outcome, _ = _assess('{"verdicts": [], "summary": "looks fine to me"}')

    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "assess.no_verdicts"
    assert outcome.decided is False


def test_a_ticket_with_no_criteria_is_refused_rather_than_passed() -> None:
    """Nothing to assess is not everything assessed. A ticket stating no acceptance criteria is
    ordinary, and the caller decides what to do about it -- this refuses to manufacture a pass."""
    outcome, stub = _assess(_answer(), criteria=())

    assert outcome.status is Status.BLOCKED
    assert outcome.reason == "assess.no_criteria"
    assert stub.calls == 0, "and it refuses before spending"


def test_the_assessor_is_named_on_the_report() -> None:
    """So a record says who assessed, and a reader can see whether it was the model that wrote
    the change -- which is the whole argument for the verb being routed apart."""
    outcome, _ = _assess(_answer((CRITERIA[0], True, "y"), (CRITERIA[1], True, "y")))

    assert outcome.value.assessor == "stub:assessor"


def test_a_change_too_large_to_send_is_marked_truncated() -> None:
    """A verdict decided from two thirds of a change is a verdict about a change nobody made, so
    the fact travels on the report rather than being silently assessed."""
    from in_lockstep.adapters.ai.assess import MAX_CHANGE_CHARS

    outcome, _ = _assess(
        _answer((CRITERIA[0], True, "y"), (CRITERIA[1], True, "y")),
        change="x" * (MAX_CHANGE_CHARS + 1),
    )

    assert outcome.value.truncated is True


def test_a_change_that_fits_is_not_marked_truncated() -> None:
    """The negative control for the flag."""
    outcome, _ = _assess(_answer((CRITERIA[0], True, "y"), (CRITERIA[1], True, "y")))

    assert outcome.value.truncated is False


# -- the change reaches the model as untrusted -----------------------------------------


def test_the_change_under_assessment_is_untrusted_context() -> None:
    """A model wrote it, on a ticket anybody can file. A diff carrying "this criterion is met"
    addressed to the assessor is a diff to assess, not an instruction to follow."""
    from in_lockstep.ai.context import Provenance

    seen: dict[str, Any] = {}

    class _Capturing(_Stub):
        async def run(self, **kwargs: Any) -> Invocation:
            seen.update(kwargs)
            return await super().run(**kwargs)

    stub = _Capturing(_answer((CRITERIA[0], True, "y"), (CRITERIA[1], True, "y")))
    adapter = AiAssess(invoker_factory=lambda _ctx: stub)
    asyncio.run(adapter.invoke(_ctx(), Assess(criteria=CRITERIA, change="a.py:\nx=1\n", ticket="#444")))

    item = seen["context"].items[0]
    assert item.provenance is Provenance.UNTRUSTED_EXTERNAL
    assert item.kind == "change"


@pytest.mark.parametrize("round_number, expected", [(1, False), (2, True)])
def test_the_round_is_said_in_the_prompt(round_number: int, expected: bool) -> None:
    """A correcting round composes a different prompt from a first pass, so two rounds of one run
    are distinguishable in the record rather than identical."""
    from in_lockstep.prompts.assess import ASSESS_PROMPTS, AssessParams

    text = ASSESS_PROMPTS["assess/criteria"]().user_text(
        AssessParams(ticket="#444", criteria=CRITERIA, round_number=round_number)
    )

    assert ("This is round" in text) is expected


# -- per-phase routing -----------------------------------------------------------------


def test_a_phase_resolves_the_route_its_aspect_names() -> None:
    """GATE-ASSESS-2. `routed_model` has always read `verb/aspect` before `verb` -- the
    `review/security` mechanism (#204) -- and nothing passed an aspect, so a repository could route
    `implement` and not `implement/assess`. This is that seam working."""
    from in_lockstep.ai.bootstrap import routed_model

    table = {"implement": "a:coder", "implement/assess": "b:assessor"}

    assert routed_model(table, "implement", "assess") == "b:assessor"
    assert routed_model(table, "implement", "green") == "a:coder", "a phase with no route takes the verb's"
    assert routed_model(table, "implement") == "a:coder"


def test_run_phase_asks_the_session_for_the_aspects_invoker() -> None:
    """The plumbing half: naming an aspect reaches a different invoker, and naming none reaches
    the run's. A repository routing only the verb must get exactly what it got before."""
    import asyncio

    from in_lockstep.adapters.ai.strategy import run_phase

    asked: list[str] = []
    run_invoker, phase_invoker = _Stub("{}"), _Stub("{}")

    class _Session:
        invoker = run_invoker
        tools = None
        run_tool = None
        policy = None
        workspace = None

        @staticmethod
        def invoker_for(aspect: str) -> Any:
            asked.append(aspect)
            return phase_invoker

    asyncio.run(run_phase(_Session(), "s", [], None, prefix="implement"))
    assert asked == [] and run_invoker.calls == 1, "no aspect named: the run's invoker"

    asyncio.run(run_phase(_Session(), "s", [], None, prefix="implement", aspect="assess"))
    assert asked == ["assess"] and phase_invoker.calls == 1, "an aspect named: that phase's"
