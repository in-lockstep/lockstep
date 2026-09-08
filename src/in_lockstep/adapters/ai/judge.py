"""The judge, backed by a model.

Thin like `triage.py`: one rubric and one answer per call, one turn, the structured reply settled
against `JUDGE_SCHEMA`, and a `Verdict` per ask. Everything the invoker owns -- the route, the
price, the recording, the injection scan, egress, the budget -- is not re-implemented here, which
is the whole point of the judge being a verb: it is routed, priced and recorded the way every
other model call is (O4, O11), and refused by name where any of those is missing.

Two things are decided before a model is reached, because O7 asks what part of a verb is
arithmetic wearing a prompt. A verdict already given over the same rubric and the same answer --
the `(rubric_sha256, answer_sha256)` pair `JudgeAsk` carries -- is replayed from `known` without a
call. And whether a case's deterministic half passed is not this adapter's question at all: the
improver grades that half first and never builds an ask for a case that failed it, so an ask that
reaches here is one no script could settle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ...ai.context import ContextItem, ContextPackage, Provenance
from ...ai.invoker import InvocationBlocked, InvocationFailed, InvokePolicy, Invoker
from ...ai.prompt import Composition, PromptLayers, compositions
from ...ai.structured import schema_instruction, settle
from ...core.improve import JudgeAsk, Verdict
from ...core.outcome import Cost, Finding, Outcome, Severity, Status
from ...core.verbs import Capability, Verb
from ...privileged.egress import EgressRefused
from ...prompts.judge import JUDGE_PROMPTS, JUDGE_SCHEMA, JudgeParams, JudgePrompt, judge_layers


@dataclass(frozen=True)
class Judge:
    """The Judge request: rubrics to settle, and verdicts already given that need no model.

    `known` is read by replay key and nothing else. A verdict whose key matches an ask is copied
    onto that ask's case and arm; one whose key matches nothing is ignored, and one with no key at
    all (a person's) cannot be replayed and is left to whoever holds it. Frozen like every request
    type: it is hashed for step identity.
    """

    asks: tuple[JudgeAsk, ...]
    known: tuple[Verdict, ...] = ()


@dataclass(frozen=True)
class JudgeReport:
    """What a `Judge` came to: a verdict per settled ask, which of them cost nothing, and which
    asks the model could not settle, with why. `calls` is how many asks were sent."""

    verdicts: tuple[Verdict, ...] = ()
    replayed: tuple[str, ...] = ()
    unsettled: tuple[tuple[str, str, str], ...] = ()
    calls: int = 0


class AiJudge:
    verb: ClassVar[Verb] = Verb.JUDGE
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.SPENDS_BUDGET})

    def __init__(
        self,
        invoker_factory: Callable[[Any], Invoker] | None = None,
        *,
        policy: InvokePolicy | None = None,
        prompts: Mapping[str, type[JudgePrompt]] | None = None,
        prompt_id: str = "judge/rubric",
        layers: PromptLayers | None = None,
    ) -> None:
        # No invoker by default: the model comes from `lockstep.models.route("judge", ...)`,
        # resolved per run off the context. Passing one is the seam for a custom registry, a
        # gateway, or a cassette provider.
        self.invoker_factory = invoker_factory
        # One turn and no tools, and there is no seam to add them: a judge reads one answer it was
        # handed whole, and a judge that could go and read the diff would be grading the change
        # rather than the answer. `max_tokens` is small because the reply is three fields.
        self.policy = policy or InvokePolicy(max_turns=1, max_tokens=2048)
        self.prompts: Mapping[str, type[JudgePrompt]] = (
            dict(prompts) if prompts is not None else dict(JUDGE_PROMPTS)
        )
        self.prompt_id = prompt_id
        self.layers = layers

    def compositions(self) -> dict[str, Composition]:
        """This adapter's prompts, for `show-prompt` and `ls`. See `AiReview.compositions`."""
        return compositions(
            self.prompts,
            self.layers if self.layers is not None else judge_layers(),
            verb=str(type(self).verb),
            source=type(self).__name__,
        )

    async def invoke(self, ctx: Any, inp: Judge) -> Outcome[JudgeReport]:
        lens = self.prompts.get(self.prompt_id)
        if lens is None:
            return _blocked(
                "judge.unknown_prompt",
                f"no judge prompt named {self.prompt_id!r}; have {sorted(self.prompts)}",
            )

        # Replay first, and before the route is even looked up: a verdict already given over this
        # rubric and this answer is the same verdict, and a corpus every ask of which was judged
        # last time needs no model and no route to be re-scored.
        by_key = {v.key: v for v in inp.known if v.rubric_sha256 and v.answer_sha256}
        verdicts: list[Verdict] = []
        replayed: list[str] = []
        pending: list[JudgeAsk] = []
        for ask in inp.asks:
            given = by_key.get((ask.rubric_sha256, ask.answer_sha256))
            if given is None:
                pending.append(ask)
                continue
            verdicts.append(_replayed(ask, given))
            replayed.append(f"{ask.case}/{ask.arm}")
        if not pending:
            return Outcome(
                status=Status.SUCCEEDED,
                value=JudgeReport(verdicts=tuple(verdicts), replayed=tuple(replayed)),
                decided=True,
            )

        from .strategy import resolve_invoker

        try:
            invoker: Invoker = resolve_invoker(self.invoker_factory, type(self).verb, ctx)
        except LookupError as e:
            # `MissingModelRoute`, by its base class: `adapters` may name `ai.bootstrap` but the
            # refusal is about the route table and not about who built the invoker. BLOCKED, not
            # raised, so the run leaves a record saying which line to add (`GATE-JUDGE-2`).
            return _blocked("judge.unrouted", str(e))

        prompt: JudgePrompt = lens()
        layers: PromptLayers = self.layers if self.layers is not None else judge_layers()
        system = prompt.system(layers) + "\n\n" + schema_instruction(JUDGE_SCHEMA)
        who = str(getattr(invoker, "model", "") or "")

        total = Cost()
        calls = 0
        unsettled: list[tuple[str, str, str]] = []
        for ask in pending:
            package = ContextPackage(
                items=(
                    ContextItem(
                        kind="answer",
                        content=ask.answer,
                        # The answer came back from a model reading somebody's diff or issue. Under
                        # judgement, that is exactly the party the judge is checking.
                        provenance=Provenance.UNTRUSTED_EXTERNAL,
                        path=f"{ask.case}/{ask.arm}",
                    ),
                )
            )
            messages = prompt.render(_params(ask), package)
            calls += 1
            try:
                invocation = await invoker.run(
                    system=system,
                    messages=messages,
                    context=package,
                    policy=self.policy,
                    schema=JUDGE_SCHEMA,
                )
                settled = await settle(
                    invoker,
                    invocation,
                    schema=JUDGE_SCHEMA,
                    system=system,
                    messages=messages,
                    context=package,
                    policy=self.policy,
                )
                invocation = settled.invocation
            except InvocationBlocked as e:
                # A ceiling or a registration refusing mid-corpus. What was settled before it is
                # kept on the outcome -- those verdicts were paid for and are true -- and the run
                # is BLOCKED under the control's own name, not read as a corpus half-judged.
                return _blocked(
                    e.reason, str(e), report=_report(verdicts, replayed, unsettled, calls), cost=total
                )
            except EgressRefused as e:
                return _blocked(
                    e.reason, str(e), report=_report(verdicts, replayed, unsettled, calls), cost=total
                )
            except InvocationFailed as e:
                return Outcome(
                    status=Status.ERRORED,
                    reason=e.reason,
                    value=_report(verdicts, replayed, unsettled, calls),
                    cost=total,
                    findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
                )
            total = total + invocation.cost

            why = _unsettled_reason(settled, invocation, self.policy)
            if why:
                # One ask the model could not answer in shape does not discard the rest: the
                # rubric stays outstanding for that case and arm, named, and the next is asked.
                unsettled.append((ask.case, ask.arm, why))
                continue
            data = settled.value if isinstance(settled.value, dict) else {}
            level = data.get("level")
            levels = int(ask.rubric.get("levels", 0) or 0)
            if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= levels:
                # A level off the scale is not a verdict. `grade` would refuse it too; refusing it
                # here keeps `Verdict.level` meaning "a rung on this rubric's scale".
                unsettled.append(
                    (ask.case, ask.arm, f"judge answered {level!r}, not a level on a {levels}-point scale")
                )
                continue
            verdicts.append(
                Verdict(
                    case=ask.case,
                    arm=ask.arm,
                    level=level,
                    reason=str(data.get("reason", "")),
                    evidence=tuple(str(e) for e in (data.get("evidence") or []) if isinstance(e, str)),
                    judge=who,
                    rubric_sha256=ask.rubric_sha256,
                    answer_sha256=ask.answer_sha256,
                )
            )

        findings = tuple(
            Finding(
                id="judge.unsettled",
                message=f"{case}/{arm}: {why}; the rubric stays outstanding",
                severity=Severity.WARNING,
                path=case,
                blocking=False,
            )
            for case, arm, why in unsettled
        )
        return Outcome(
            status=Status.SUCCEEDED,
            value=_report(verdicts, replayed, unsettled, calls),
            findings=findings,
            cost=total,
            # Decided when every ask got a rung. An ask the model could not settle leaves its
            # rubric outstanding, which is not a decision about that case.
            decided=not unsettled,
            reason="judge.unsettled" if unsettled else None,
        )


def _params(ask: JudgeAsk) -> JudgeParams:
    """The rubric as the judge is told it, read from the record `Rubric.as_record` wrote."""
    rubric = ask.rubric
    criteria = rubric.get("criteria") or ()
    anchors = rubric.get("anchors") or ()
    return JudgeParams(
        case=ask.case,
        arm=ask.arm,
        criteria=tuple(str(c) for c in criteria),
        levels=int(rubric.get("levels", 0) or 0),
        # `Rubric.as_record` writes the bar as `min`, the spelling the shipped corpus uses; a
        # record written by hand may say `minimum`. Either. The bar is already on the scale by
        # the time it is here -- `Rubric.parse` refused one that was not before any ask existed.
        minimum=int(rubric.get("min", rubric.get("minimum", 0)) or 0),
        anchors=tuple((int(pair[0]), str(pair[1])) for pair in anchors if len(pair) == 2),
    )


def _replayed(ask: JudgeAsk, given: Verdict) -> Verdict:
    """A known verdict, re-addressed to the ask it answers. The level, reason, evidence and judge
    are the original's; the case and arm are the ask's, because the same answer can sit on the
    `before` arm of one measurement and the `after` arm of the next."""
    return Verdict(
        case=ask.case,
        arm=ask.arm,
        level=given.level,
        reason=given.reason,
        evidence=given.evidence,
        judge=given.judge,
        rubric_sha256=ask.rubric_sha256,
        answer_sha256=ask.answer_sha256,
    )


def _unsettled_reason(settled: Any, invocation: Any, policy: InvokePolicy) -> str:
    if invocation.truncated:
        return f"the model stopped at the {policy.max_tokens}-token output cap with its verdict unfinished"
    if settled.reason == "unparseable":
        return f"unparseable: {settled.detail}"
    if settled.reason == "schema_mismatch":
        return "schema mismatch: " + "; ".join(settled.problems)
    return ""


def _report(
    verdicts: list[Verdict], replayed: list[str], unsettled: list[tuple[str, str, str]], calls: int
) -> JudgeReport:
    return JudgeReport(
        verdicts=tuple(verdicts), replayed=tuple(replayed), unsettled=tuple(unsettled), calls=calls
    )


def _blocked(
    reason: str, message: str, *, report: JudgeReport | None = None, cost: Cost | None = None
) -> Outcome[JudgeReport]:
    return Outcome.blocked_by(
        reason,
        value=report,
        cost=cost if cost is not None else Cost(),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
