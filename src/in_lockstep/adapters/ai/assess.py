"""Does the change do what the ticket asked for?

TDD settles red-to-green deterministically, and that is the point of it: a test that failed and now
passes is arithmetic, and arithmetic does not need a model. What it does not settle is whether the
change ANSWERS the ticket, and nothing was asking -- so #450 satisfied its own tests, skipped an
acceptance criterion outright, and wrote a test asserting the omission as a requirement, green
throughout.

**One call, every criterion.** A criterion is not independent of its neighbours: "the clause is in
the refusal a person reads" and "the two refusals differ" are about the same sentence from two
sides, and an assessor shown one at a time cannot see that satisfying one broke the other. One call
also carries the diff once rather than once per criterion, which is the difference between a few
cents and a few dollars on a large change.

**The assessor is not the author, and the route is what makes that true.** `Verb.ASSESS` routes
apart, so `lockstep.models.route("assess", ...)` names a model that did not write the change. That
is the whole argument for the verb existing: a model that has just argued itself into an
implementation is the worst available judge of whether the implementation answers the ticket, and
the same context that produced an omission is the context that would have to notice it.

Nothing stops a repository routing both at the same id, and nothing should -- a small repository
with one provider is not wrong to. The framework makes the separation available and says when it
is not in force; it does not refuse to run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from ...ai.context import ContextItem, ContextPackage, Provenance
from ...ai.invoker import InvocationBlocked, InvocationFailed, InvokePolicy, Invoker
from ...ai.prompt import Composition, PromptLayers, compositions
from ...ai.structured import schema_instruction, settle
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.types import Assess, AssessReport, CriterionVerdict
from ...core.verbs import Capability, Verb
from ...privileged.egress import EgressRefused
from ...prompts.assess import ASSESS_PROMPTS, ASSESS_SCHEMA, AssessParams, AssessPrompt, assess_layers

#: How much of the change travels to the assessor. The same ceiling `attempts.py` puts on a diff
#: and for the same reason: the criteria and the guardrails must not lose their place to a model
#: that rewrote a thousand-line file. A truncated diff is said out loud on the report rather than
#: silently assessed, because "met" decided from two thirds of a change is a verdict about a
#: change nobody made.
MAX_CHANGE_CHARS = 60_000

#: What a criterion's verdict says when the assessor did not answer it. Named rather than written
#: twice, because `invoke` distinguishes "the assessor answered nothing at all" from "the change
#: met nothing" by asking whether every reason is this one -- and two spellings of the sentence is
#: that distinction quietly becoming always-false.
_UNANSWERED = "the assessor returned no verdict for this criterion"


def _blocked(reason: str, message: str, *, report: AssessReport | None = None) -> Outcome[AssessReport]:
    return Outcome(
        status=Status.BLOCKED,
        reason=reason,
        value=report,
        decided=False,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


class AiAssess:
    verb: ClassVar[Verb] = Verb.ASSESS
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.SPENDS_BUDGET})

    def __init__(
        self,
        invoker_factory: Callable[[Any], Invoker] | None = None,
        *,
        policy: InvokePolicy | None = None,
        prompts: Mapping[str, type[AssessPrompt]] | None = None,
        prompt_id: str = "assess/criteria",
        layers: PromptLayers | None = None,
    ) -> None:
        # No invoker by default: the model comes from `lockstep.models.route("assess", ...)`,
        # resolved per run off the context. Passing one is the seam for a custom registry.
        self.invoker_factory = invoker_factory
        # One turn and no tools, deliberately, and the reasoning is `AiJudge`'s with one change.
        # An assessor that could go and read the repository would be reviewing rather than
        # assessing, and would answer about code the change did not touch. What it needs is the
        # criteria and the change, and it is handed both whole.
        #
        # `max_tokens` is larger than a judge's: a verdict per criterion with a reason each is
        # several times a single level-and-reason, and a reply the loop truncates is a round paid
        # for and discarded.
        self.policy = policy or InvokePolicy(max_turns=1, max_tokens=8192)
        self.prompts: Mapping[str, type[AssessPrompt]] = (
            dict(prompts) if prompts is not None else dict(ASSESS_PROMPTS)
        )
        self.prompt_id = prompt_id
        self.layers = layers

    def compositions(self) -> dict[str, Composition]:
        """This adapter's prompts, for `show-prompt` and `ls`. See `AiReview.compositions`."""
        return compositions(
            self.prompts,
            self.layers if self.layers is not None else assess_layers(),
            verb=str(type(self).verb),
            source=type(self).__name__,
        )

    async def invoke(self, ctx: Any, inp: Assess) -> Outcome[AssessReport]:
        lens = self.prompts.get(self.prompt_id)
        if lens is None:
            return _blocked(
                "assess.unknown_prompt",
                f"no assess prompt named {self.prompt_id!r}; have {sorted(self.prompts)}",
            )
        if not inp.criteria:
            # Nothing to assess is not "everything passed". A ticket with no acceptance criteria
            # is a real and common case, and the caller decides what to do about it -- this
            # refuses to manufacture a verdict rather than returning an empty `met`.
            return _blocked(
                "assess.no_criteria",
                "the ticket states no acceptance criteria, so there is nothing to assess it "
                "against. An assessment of no criteria is not an assessment that passed.",
            )

        from .strategy import resolve_invoker

        try:
            invoker: Invoker = resolve_invoker(self.invoker_factory, type(self).verb, ctx)
        except LookupError as e:
            # By its base class, as `AiJudge` does: the refusal is about the route table, and a
            # BLOCKED outcome leaves a record naming the line to add rather than a traceback.
            return _blocked("assess.unrouted", str(e))

        change, truncated = inp.change, False
        if len(change) > MAX_CHANGE_CHARS:
            change = change[:MAX_CHANGE_CHARS]
            truncated = True

        prompt: AssessPrompt = lens()
        layers: PromptLayers = self.layers if self.layers is not None else assess_layers()
        system = prompt.system(layers) + "\n\n" + schema_instruction(ASSESS_SCHEMA)
        who = str(getattr(invoker, "model", "") or "")
        package = ContextPackage(
            items=(
                ContextItem(
                    kind="change",
                    content=change,
                    # A model wrote it, on a ticket anybody can file. Under assessment that is
                    # exactly the party being checked.
                    provenance=Provenance.UNTRUSTED_EXTERNAL,
                    path=inp.ticket or "change",
                ),
            )
        )
        messages = prompt.render(
            AssessParams(
                ticket=inp.ticket,
                title=inp.title,
                criteria=inp.criteria,
                round_number=inp.round_number,
            ),
            package,
        )
        try:
            invocation = await invoker.run(
                system=system, messages=messages, context=package, policy=self.policy, schema=ASSESS_SCHEMA
            )
            settled = await settle(
                invoker,
                invocation,
                schema=ASSESS_SCHEMA,
                system=system,
                messages=messages,
                context=package,
                policy=self.policy,
            )
            invocation = settled.invocation
        except (InvocationBlocked, EgressRefused) as e:
            return _blocked(e.reason, str(e))
        except InvocationFailed as e:
            # `decided` stated, for `AiJudge`'s reason: `Outcome` defaults it True, and a provider
            # that could not be reached assessed nothing. No cost: the failure may have come from
            # the first call, where there is no invocation to read one off, and a zero invented
            # here would be a number nobody measured.
            return Outcome(
                status=Status.ERRORED,
                reason=e.reason,
                decided=False,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )

        data = settled.value if isinstance(settled.value, dict) else {}
        verdicts = _verdicts(data, inp.criteria)
        # Whether the assessor answered ANY of the criteria it was asked about -- not whether
        # `_verdicts` produced entries, which it always does, one per criterion asked. The two
        # are different facts and only this one distinguishes a broken assessor from a change that
        # failed everything: pairing fills an unanswered criterion with "no verdict returned",
        # which is the right reason to show a person and a useless one to hand the correcting
        # round, since there is nothing in it to fix.
        if not any(v.met or v.reason != _UNANSWERED for v in verdicts):
            # A reply in shape that named no criterion settles nothing, and reporting it as `met`
            # would be the loudest possible version of this whole defect.
            return Outcome(
                status=Status.BLOCKED,
                reason="assess.no_verdicts",
                value=AssessReport(summary=str(data.get("summary", "")), assessor=who, truncated=truncated),
                cost=invocation.cost,
                decided=False,
                findings=(
                    Finding(
                        id="assess.no_verdicts",
                        message="the assessor returned no verdict for any criterion it was asked "
                        "about, so nothing was assessed. This is not a pass, and it is not a "
                        "change that failed either -- there is nothing here to correct against.",
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )
        report = AssessReport(
            verdicts=verdicts,
            summary=str(data.get("summary", "")),
            assessor=who,
            truncated=truncated,
        )
        return Outcome(status=Status.SUCCEEDED, value=report, cost=invocation.cost, decided=True)


def _verdicts(data: Mapping[str, Any], criteria: tuple[str, ...]) -> tuple[CriterionVerdict, ...]:
    """The verdicts the assessor returned, paired with the criteria that were asked.

    Matched on the criterion text the assessor echoed back rather than on position: a reply that
    returns three verdicts for four criteria would otherwise silently answer the wrong ones, and
    a verdict attached to a criterion nobody asked about is dropped rather than counted.

    A criterion the assessor did not answer is UNMET, with a reason saying so. Treating silence as
    met is the failure this whole verb exists to remove, one layer in.
    """
    returned: dict[str, Mapping[str, Any]] = {}
    for raw in data.get("verdicts") or ():
        if isinstance(raw, Mapping) and isinstance(raw.get("criterion"), str):
            returned[str(raw["criterion"]).strip()] = raw

    out: list[CriterionVerdict] = []
    for criterion in criteria:
        raw = returned.get(criterion.strip())
        if raw is None:
            out.append(
                CriterionVerdict(
                    criterion=criterion,
                    met=False,
                    reason=_UNANSWERED,
                )
            )
            continue
        out.append(
            CriterionVerdict(
                criterion=criterion,
                met=bool(raw.get("met")),
                reason=str(raw.get("reason", "")),
                evidence=tuple(str(e) for e in (raw.get("evidence") or []) if isinstance(e, str)),
            )
        )
    return tuple(out)
