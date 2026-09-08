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


# -- GATE-JUDGE-2: the judge is a routed, priced, recorded verb ------------------------------------


def _ask(case: str, arm: str, answer: str, *, levels: int = 5, minimum: int = 4) -> JudgeAsk:
    import hashlib

    rubric = {"criteria": ["names the mechanism"], "levels": levels, "min": minimum, "anchors": []}
    return JudgeAsk(
        case=case,
        arm=arm,
        rubric=rubric,
        answer=answer,
        rubric_sha256=hashlib.sha256(json.dumps(rubric, sort_keys=True).encode()).hexdigest(),
        answer_sha256=hashlib.sha256(answer.encode()).hexdigest(),
    )


def _invocation(content: str, *, truncated: bool = False) -> Any:
    from in_lockstep.ai.invoker import Invocation
    from in_lockstep.core.outcome import Cost

    return Invocation(
        content=content, cost=Cost(usd=0.001, input_tokens=10, output_tokens=5), truncated=truncated
    )


class _Scripted:
    """An invoker that answers from a script and remembers what it was asked."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.asked: list[dict[str, Any]] = []
        self.model = "scripted"

    async def run(self, **kwargs: Any) -> Any:
        self.asked.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if not isinstance(reply, str) else _invocation(reply)


class _Counting:
    """A provider that answers with a verdict and counts how often it was reached."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def name(self) -> str:
        return "stub"

    async def generate(self, input: Any) -> Any:
        from in_lockstep.llm.types import LLMOutput, TokenUsage

        self.calls.append(input)
        return LLMOutput(
            content=json.dumps({"level": 5, "reason": "names the lock", "evidence": ["the session lock"]}),
            usage=TokenUsage(input_tokens=100, output_tokens=20),
        )


def _real_invoker(provider: Any, *, priced: bool = True, structured: bool = True) -> Any:
    from in_lockstep.ai.invoker import AiInvoker
    from in_lockstep.ai.pricing import CostTable, Rate
    from in_lockstep.core.spend import Budget, Spend
    from in_lockstep.llm.registry import ModelCaps
    from in_lockstep.privileged.egress import UnsandboxedEgress

    table = CostTable()
    if priced:
        table.add("m", Rate(3.0, 15.0))
    return AiInvoker(
        provider,
        model="m",
        cost_table=table,
        spend=Spend(budget=Budget(usd=5.0)),
        egress=UnsandboxedEgress(),
        caps=ModelCaps(structured_output=structured),
    )


def _ctx(**overrides: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(**{"models": {}, "recording": None, **overrides})


def _judge(adapter: Any, request: Any, ctx: Any = None) -> Any:
    import asyncio

    return asyncio.run(adapter.invoke(ctx if ctx is not None else _ctx(), request))


def test_gate_judge_2_the_judge_is_refused_by_name_unrouted_unpriced_or_unshaped_with_no_call() -> None:
    """GATE-JUDGE-2. Three refusals, each naming what is missing, each before a provider is
    reached. Unrouted is the adapter's own; unpriced and no-structured-output are the invoker's,
    driven here through the real `AiInvoker` so the judge is shown to sit on the shared path
    rather than on a check of its own."""
    from in_lockstep.adapters.ai import AiJudge, Judge
    from in_lockstep.core.outcome import Status

    ask = _ask("c", "before", "the session lock")

    unrouted = _judge(AiJudge(), Judge(asks=(ask,)))
    assert unrouted.status is Status.BLOCKED and unrouted.reason == "judge.unrouted"
    assert 'lockstep.models.route("judge", ...)' in unrouted.findings[0].message

    provider = _Counting()
    unpriced = _judge(AiJudge(lambda ctx: _real_invoker(provider, priced=False)), Judge(asks=(ask,)))
    assert unpriced.status is Status.BLOCKED and unpriced.reason == "cost.unpriced_model"
    assert "'m'" in unpriced.findings[0].message

    unshaped = _judge(AiJudge(lambda ctx: _real_invoker(provider, structured=False)), Judge(asks=(ask,)))
    assert unshaped.status is Status.BLOCKED and unshaped.reason == "model.no_structured_output"

    assert provider.calls == [], "nothing was sent and nothing was charged"
    assert all(o.cost.usd == 0 for o in (unrouted, unpriced, unshaped))


def test_gate_judge_2_a_routed_priced_judge_returns_a_verdict_and_the_answer_travels_untrusted(
    tmp_path: Path,
) -> None:
    """GATE-JUDGE-2. Through the real invoker against a priced registration: one call, one
    verdict on the rubric's scale carrying the model that gave it and the ask's replay key, a
    cost above zero, and -- when the run keeps a recording -- the call on the tape, because
    `resolve_invoker` is the seam and it wraps whatever the factory returned."""
    from in_lockstep.adapters.ai import AiJudge, Judge
    from in_lockstep.ai.context import Provenance
    from in_lockstep.ai.replay import Cassette
    from in_lockstep.core.outcome import Status

    ask = _ask("c", "after", "the session lock is held across the await")
    provider = _Counting()
    tape = Cassette(path=tmp_path / "judge.json")
    outcome = _judge(AiJudge(lambda ctx: _real_invoker(provider)), Judge(asks=(ask,)), _ctx(recording=tape))

    assert outcome.status is Status.SUCCEEDED and outcome.decided
    (verdict,) = outcome.value.verdicts
    assert (verdict.case, verdict.arm, verdict.level, verdict.judge) == ("c", "after", 5, "m")
    assert verdict.evidence == ("the session lock",)
    assert verdict.key == (ask.rubric_sha256, ask.answer_sha256), "a verdict carries its replay key"
    assert outcome.value.calls == 1 and outcome.value.replayed == ()
    assert outcome.cost.usd > 0, "priced from the table, not invented"
    assert len(provider.calls) == 1
    assert len(tape.provider_calls) == 1, "recorded without being asked (O4)"

    # The answer is the only context item and it is untrusted; the rubric is in the instruction.
    scripted = _Scripted(json.dumps({"level": 4, "reason": "ok"}))
    _judge(AiJudge(lambda ctx: scripted), Judge(asks=(ask,)))
    (asked,) = scripted.asked
    (item,) = asked["context"].items
    assert item.kind == "answer" and item.provenance is Provenance.UNTRUSTED_EXTERNAL
    assert item.content == ask.answer
    user = asked["messages"][0].content
    assert "names the mechanism" in user and "1 to 5" in user and "4 or above passes" in user
    assert asked["schema"]["required"] == ["level", "reason"]
    assert asked["policy"].max_turns == 1


def test_gate_judge_2_a_pair_already_judged_is_replayed_without_a_call_or_a_route() -> None:
    """GATE-JUDGE-2. The replay key is the rubric's and the answer's content hashes. A known
    verdict over the same pair is copied onto the ask -- re-addressed to the ask's case and arm --
    and no model is consulted; when every ask is known, no route is looked up either."""
    from in_lockstep.adapters.ai import AiJudge, Judge
    from in_lockstep.core.outcome import Status

    same = _ask("c", "before", "the session lock")
    other = _ask("c", "after", "a different answer")
    known = Verdict(
        "old-case",
        "after",
        5,
        "names it",
        judge="m",
        rubric_sha256=same.rubric_sha256,
        answer_sha256=same.answer_sha256,
    )
    keyless = Verdict("c", "after", 1, "a person's, replayable over nothing")

    only_known = _judge(AiJudge(), Judge(asks=(same,), known=(known, keyless)))
    assert only_known.status is Status.SUCCEEDED and only_known.decided
    (replayed,) = only_known.value.verdicts
    assert (replayed.case, replayed.arm, replayed.level, replayed.judge) == ("c", "before", 5, "m")
    assert only_known.value.replayed == ("c/before",) and only_known.value.calls == 0

    scripted = _Scripted(json.dumps({"level": 2, "reason": "vague"}))
    mixed = _judge(AiJudge(lambda ctx: scripted), Judge(asks=(same, other), known=(known,)))
    assert [(v.case, v.arm, v.level) for v in mixed.value.verdicts] == [("c", "before", 5), ("c", "after", 2)]
    assert mixed.value.calls == 1 and len(scripted.asked) == 1, "only the unjudged pair was sent"


def test_gate_judge_2_an_arm_whose_deterministic_half_failed_gets_no_ask(tmp_path: Path) -> None:
    """GATE-JUDGE-2, O7. `verdict_of` reads a failed check as a failed case whatever a judge
    says, so an ask on that arm could change nothing. The recorded answer says "lock"; the probe
    answer does not; a third case's recorded answer fails its own check and neither arm is asked."""
    from in_lockstep.core.improve import Improvable

    corpus = tmp_path / "cases"
    _case(corpus, "held", rubric="names the mechanism", contains=("lock",))
    _case(corpus, "lost", rubric="names the mechanism", contains=("zebra",))
    body = Improvable(
        body="house/security.md", verb="review", label="review/security", answers=("review.security",)
    )
    improver = CorpusImprover(corpus)
    baseline = improver.baseline(body, HEADER + BODY)
    answers = [
        Answered(case="held", content=json.dumps({"findings": [{"summary": "no such thing"}]})),
        Answered(case="lost", content=json.dumps({"findings": [{"summary": "a zebra"}]})),
    ]
    asks = improver.rubrics(baseline, answers)
    assert [(a.case, a.arm) for a in asks] == [("held", "before"), ("lost", "after")]


def test_an_unshaped_reply_or_an_off_scale_level_leaves_that_rubric_outstanding_and_asks_the_next() -> None:
    """One ask the model could not settle does not discard the rest. An off-scale level is not a
    verdict (`Verdict.level` means a rung), and a reply that is still not JSON after the one
    re-prompt `settle` allows is named; both are `judge.unsettled` findings and the outcome is
    not `decided`."""
    from in_lockstep.adapters.ai import AiJudge, Judge
    from in_lockstep.core.outcome import Status

    asks = (_ask("a", "before", "x"), _ask("b", "before", "y"), _ask("c", "before", "z"))
    scripted = _Scripted(
        json.dumps({"level": 9, "reason": "off the scale"}),
        "not json",
        "still not json",  # the one re-prompt
        json.dumps({"level": 4, "reason": "fine", "evidence": ["z"]}),
    )
    outcome = _judge(AiJudge(lambda ctx: scripted), Judge(asks=asks))
    assert outcome.status is Status.SUCCEEDED and not outcome.decided and outcome.reason == "judge.unsettled"
    assert [(v.case, v.level) for v in outcome.value.verdicts] == [("c", 4)]
    assert [(c, a) for c, a, _ in outcome.value.unsettled] == [("a", "before"), ("b", "before")]
    assert "9" in outcome.value.unsettled[0][2] and "5-point" in outcome.value.unsettled[0][2]
    assert "unparseable" in outcome.value.unsettled[1][2]
    assert [f.id for f in outcome.findings] == ["judge.unsettled", "judge.unsettled"]
    assert outcome.value.calls == 3 and len(scripted.asked) == 4
    assert outcome.cost.usd == pytest.approx(0.004), "the re-prompt is billed like the rest"


def test_a_ceiling_firing_mid_corpus_keeps_the_verdicts_already_paid_for() -> None:
    """BLOCKED under the control's own name, with what was settled before it on the outcome."""
    from in_lockstep.adapters.ai import AiJudge, Judge
    from in_lockstep.ai.invoker import InvocationBlocked
    from in_lockstep.core.outcome import Status

    asks = (_ask("a", "before", "x"), _ask("b", "before", "y"), _ask("c", "before", "z"))
    scripted = _Scripted(
        json.dumps({"level": 5, "reason": "good"}), InvocationBlocked("budget.exceeded", "the ceiling")
    )
    outcome = _judge(AiJudge(lambda ctx: scripted), Judge(asks=asks))
    assert outcome.status is Status.BLOCKED and outcome.reason == "budget.exceeded" and not outcome.decided
    assert [(v.case, v.level) for v in outcome.value.verdicts] == [("a", 5)]
    assert outcome.value.calls == 2 and len(scripted.asked) == 2, "the third ask was never sent"
    assert outcome.cost.usd == pytest.approx(0.001)


def test_a_provider_that_could_not_be_reached_errors_the_judge_and_decides_nothing() -> None:
    """The first real `judge/corpus` run here dialled an Ollama nobody had started, and the
    record said `decided: true` over zero verdicts (run judge-corpus-20260908T023019Z-c427).
    ERRORED is infrastructure, not a control refusing, and it is not a decision either."""
    from in_lockstep.adapters.ai import AiJudge, Judge
    from in_lockstep.ai.invoker import InvocationFailed
    from in_lockstep.core.outcome import Status

    asks = (_ask("a", "before", "x"), _ask("b", "before", "y"))
    scripted = _Scripted(
        json.dumps({"level": 5, "reason": "good"}),
        InvocationFailed("provider.transient", "All connection attempts failed"),
    )
    outcome = _judge(AiJudge(lambda ctx: scripted), Judge(asks=asks))
    assert outcome.status is Status.ERRORED and outcome.reason == "provider.transient"
    assert not outcome.decided, "a judge nobody could reach decided nothing"
    assert [(v.case, v.level) for v in outcome.value.verdicts] == [("a", 5)], "what was paid for is kept"
