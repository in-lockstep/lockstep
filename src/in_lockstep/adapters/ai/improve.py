"""The paid half of the learning loop: draft a body, and re-ask recorded questions with it.

Two request types on one adapter, because they spend from one budget and record onto one tape.
`Draft` is one model call: the body as it stands, the finding that recurs under it, and the checks
the current answers fail, in; the whole revised body, out. `Measure` is one call per promoted case
the body is attributable to: the recorded request with the body swapped and nothing else changed,
against the model the case was recorded on. Neither writes a file — the workflow stages what
comes back into a ChangeSet, and the job holding a write token opens it.

What is deliberately NOT here is any reading of a case or any grading of an answer. This layer
may not import `evaluation`, and the split is right: an adapter that could see the expectations it
was about to be measured against is an adapter one refactor away from optimising for them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ...ai.context import ContextItem, ContextPackage, Provenance
from ...ai.invoker import AiInvoker, InvocationBlocked, InvocationFailed, InvokePolicy
from ...ai.prompt import Composition, PromptLayers, compositions
from ...ai.replay import request_from
from ...ai.structured import SchemaError, parse, schema_instruction, validate
from ...core.improve import Answered, Probe
from ...core.outcome import Cost, Finding, Outcome, Severity, Status
from ...core.verbs import Capability, Verb
from ...privileged.egress import EgressRefused
from ...prompts.improve import IMPROVE_SCHEMA, PROMPTS, ImproveParams, ImprovePrompt, improve_layers
from .strategy import resolve_invoker

#: The drafter's output cap. A prompt body is one to three thousand tokens; this is headroom for a
#: long one plus its rationale, and it bounds the pre-flight estimate the way `_REVIEW_MAX_TOKENS`
#: does for a review — erring low is the expensive mistake, because a truncated body is paid for
#: in full and cannot be applied.
_DRAFT_MAX_TOKENS = 8192


@dataclass(frozen=True)
class Draft:
    """Ask for a revised body. `current` is the whole file, header included; the header comes back
    verbatim on the result, because a model that could rewrite the two-key header could choose a
    different name for a body other prompts compose by name."""

    body: str
    label: str
    current: str
    finding: str
    runs: int
    considered: int
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftReport:
    text: str
    rationale: str


@dataclass(frozen=True)
class Measure:
    """Re-ask every probe against the model it was recorded on. One request, so the budget
    middleware sees the whole measurement as one step and a ceiling stops it as one."""

    probes: tuple[Probe, ...]


def split_header(text: str) -> tuple[str, str]:
    """The two-key header and the body, by the `---` fences and nothing cleverer.

    `parse_frontmatter` returns the parsed keys and the body; what this needs is the header's
    BYTES, so they can be re-attached without a single character moving. A header re-rendered
    from parsed keys would be a header the framework rewrote on every proposal.
    """
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end < 0:
        return "", text
    cut = end + len("\n---\n")
    return text[:cut], text[cut:]


class AiImprove:
    verb: ClassVar[Verb] = Verb.IMPROVE
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.SPENDS_BUDGET})

    def __init__(
        self,
        invoker_factory: Callable[[Any], AiInvoker] | None = None,
        *,
        probe_factory: Callable[[str], Callable[[Any], AiInvoker]] | None = None,
        policy: InvokePolicy | None = None,
        prompts: Mapping[str, type[ImprovePrompt]] | None = None,
        layers: PromptLayers | None = None,
    ) -> None:
        # The drafting call resolves like every other AI adapter: an injected factory, or the
        # model routed for `improve`. The measuring calls cannot, because each one must go to the
        # model its case was RECORDED on — a comparison that also changed the model would be a
        # comparison of two things — so `probe_factory` maps a model id to a factory, and the
        # default is `ai.bootstrap.invoker_factory`, which attaches the run's recording itself.
        self.invoker_factory = invoker_factory
        self.probe_factory = probe_factory
        self.policy = policy or InvokePolicy(max_turns=1, max_tokens=_DRAFT_MAX_TOKENS)
        self.prompts: Mapping[str, type[ImprovePrompt]] = (
            dict(prompts) if prompts is not None else dict(PROMPTS)
        )
        self.layers = layers

    def compositions(self) -> dict[str, Composition]:
        return compositions(
            self.prompts,
            self.layers if self.layers is not None else improve_layers(),
            verb=str(type(self).verb),
            source=type(self).__name__,
        )

    async def invoke(self, ctx: Any, inp: Draft | Measure) -> Outcome[Any]:
        if isinstance(inp, Measure):
            return await self._measure(ctx, inp)
        return await self._draft(ctx, inp)

    async def _draft(self, ctx: Any, inp: Draft) -> Outcome[Any]:
        header, body = split_header(inp.current)
        prompt = next(iter(self.prompts.values()))()
        layers = self.layers if self.layers is not None else improve_layers()
        system = prompt.system(layers) + "\n\n" + schema_instruction(IMPROVE_SCHEMA)
        package = ContextPackage(
            items=(
                ContextItem(
                    kind="file", content=body.strip(), provenance=Provenance.TRUSTED_REPO, path=inp.body
                ),
                # The checks quote recorded answers, which are a model's prose about a diff
                # somebody else wrote. Untrusted, so the egress rule that covers a review's diff
                # covers this too.
                ContextItem(
                    kind="log",
                    content="\n".join(inp.evidence) or "(no failing check was named)",
                    provenance=Provenance.UNTRUSTED_EXTERNAL,
                    path="failing checks",
                ),
            )
        )
        params = ImproveParams(label=inp.label, finding=inp.finding, runs=inp.runs, considered=inp.considered)
        messages = prompt.render(params, package)
        invoker: AiInvoker = resolve_invoker(self.invoker_factory, type(self).verb, ctx)
        try:
            invocation = await invoker.run(
                system=system, messages=messages, context=package, policy=self.policy
            )
        except (InvocationBlocked, EgressRefused) as e:
            return Outcome.blocked_by(
                e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )
        except InvocationFailed as e:
            return _errored(e.reason, str(e))
        if invocation.truncated:
            return _errored(
                "improve.truncated",
                f"the drafter stopped at the {self.policy.max_tokens}-token output cap with the body "
                f"unfinished; a partial body cannot be applied",
                cost=invocation.cost,
            )
        try:
            parsed = parse(invocation.content)
        except SchemaError as e:
            return _errored("improve.unparseable", str(e), cost=invocation.cost)
        problems = validate(parsed.value, IMPROVE_SCHEMA)
        if problems:
            return _errored("improve.schema_mismatch", "; ".join(problems), cost=invocation.cost)
        value = parsed.value if isinstance(parsed.value, dict) else {}
        revised = str(value.get("body", "")).strip()
        if not revised or revised == body.strip():
            # A draft identical to the current body would measure `unchanged` after a paid probe
            # per case. Refused here, where the cost is one call.
            return Outcome(
                status=Status.FAILED,
                reason="improve.no_change",
                cost=invocation.cost,
                findings=(
                    Finding(
                        id="improve.no_change",
                        message="the drafter returned the body unchanged, so there is nothing to measure",
                        severity=Severity.WARNING,
                    ),
                ),
            )
        return Outcome(
            status=Status.SUCCEEDED,
            value=DraftReport(
                text=header + revised + "\n", rationale=str(value.get("rationale", "")).strip()
            ),
            cost=invocation.cost,
            decided=not invocation.exhausted,
        )

    async def _measure(self, ctx: Any, inp: Measure) -> Outcome[Any]:
        from ...ai.bootstrap import invoker_factory as routed_factory
        from ...ai.bootstrap import recorded

        answers: list[Answered] = []
        invokers: dict[str, AiInvoker] = {}
        total = Cost()
        for probe in inp.probes:
            invoker = invokers.get(probe.model)
            if invoker is None:
                factory = (self.probe_factory or routed_factory)(probe.model)
                invoker = factory(ctx)
                # The same wrap `resolve_invoker` applies: the framework holds what the factory
                # returned, and the recording is not something a custom factory can decline.
                log = getattr(ctx, "recording", None)
                if log is not None and getattr(invoker, "provider", None) is not None:
                    invoker.provider = recorded(invoker.provider, log)
                invokers[probe.model] = invoker
            request = request_from(probe.request)
            # The recorded request's own ceilings, so the after arm answers under the terms the
            # before arm was recorded under. One turn: a review recorded with no tools has nothing
            # a second turn could do.
            policy = InvokePolicy(max_turns=1, max_tokens=request.max_tokens, temperature=request.temperature)
            package = ContextPackage(
                items=(
                    ContextItem(
                        kind="diff", content="", provenance=Provenance.UNTRUSTED_EXTERNAL, path=probe.case
                    ),
                )
            )
            try:
                invocation = await invoker.run(
                    system=request.system, messages=list(request.messages), context=package, policy=policy
                )
            except (InvocationBlocked, EgressRefused) as e:
                # A control refusing mid-measurement stops the measurement: the cases answered so
                # far travel with the refusal so a reader can see how far it got, and the verdict
                # is BLOCKED rather than a scorecard over a denominator a ceiling chose.
                return Outcome.blocked_by(
                    e.reason,
                    value=tuple(answers),
                    cost=total,
                    findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
                )
            except InvocationFailed as e:
                answers.append(Answered(case=probe.case, status="errored", detail=str(e)))
                continue
            total = total + invocation.cost
            if invocation.truncated:
                answers.append(
                    Answered(
                        case=probe.case,
                        content=invocation.content,
                        status="errored",
                        detail=f"truncated at the {request.max_tokens}-token output cap",
                    )
                )
                continue
            answers.append(Answered(case=probe.case, content=invocation.content))
        return Outcome(status=Status.SUCCEEDED, value=tuple(answers), cost=total)


def _errored(reason: str, message: str, cost: Cost | None = None) -> Outcome[Any]:
    return Outcome(
        status=Status.ERRORED,
        reason=reason,
        cost=cost if cost is not None else Cost(),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
