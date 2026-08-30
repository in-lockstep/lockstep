"""RFE, backed by a model.

The last of goal 2's six workflows, riding the triage vertical rather than growing its own: one
untrusted context item in, one structured answer out, and everything the invoker owns — the loop
bound, the spend ceiling, the injection scan, egress — not re-implemented here. Where triage
reads an issue and places it, this reads a rough idea and drafts the ticket a team could pick
up.

The draft never files itself. An idea is authored by whoever had it, anyone who can describe a
feature can write into a prompt, and a ticket in the tracker is an instruction to future agents
— so the `rfe` guardrail denies the issue-writing tools, and creation is a separate, human step:
the caller reads the draft and takes it to `TicketSource.create`, which is what the CLI's
`--create` flag spells.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ...ai.context import ContextItem, ContextPackage, Provenance
from ...ai.invoker import AiInvoker, InvocationBlocked, InvocationFailed, InvokePolicy, ToolRunner
from ...ai.prompt import PromptLayers
from ...ai.structured import SchemaError, parse, schema_instruction, validate
from ...ai.tools import ToolSet
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.verbs import Capability, Verb
from ...privileged.egress import EgressRefused
from ...prompts.rfe import RFE_PROMPTS, RFE_SCHEMA, RfeParams, RfePrompt, rfe_layers


@dataclass(frozen=True)
class RfeDraft:
    title: str
    problem: str = ""
    proposal: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Whether a team could pick this up as it stands — no decision left unmade."""
        return bool(self.title and self.problem and self.proposal) and not self.open_questions

    def render(self) -> str:
        """The draft as the ticket body it would become — what `TicketSource.create` files."""
        lines = ["## Problem", self.problem or "(none)", "", "## Proposal", self.proposal or "(none)"]
        if self.acceptance_criteria:
            lines += ["", "## Acceptance criteria"]
            lines += [f"- {c}" for c in self.acceptance_criteria]
        if self.open_questions:
            lines += ["", "## Open questions"]
            lines += [f"- {q}" for q in self.open_questions]
        return "\n".join(lines)


@dataclass(frozen=True)
class Rfe:
    """The RFE request: one idea to draft from. Workflows do `ctx.do(Rfe(...))`; a binding
    decides what runs it. Frozen like every request type: hashed for step identity and
    serialized into checkpoints.

    `idea` is the rough request — a sentence from a terminal, a file, or the body of an existing
    feature-kind issue being elaborated. `key` names where it came from when it came from
    somewhere (`#42`, a filename), and is empty for an idea typed at a prompt."""

    idea: str
    key: str = ""

    @classmethod
    def from_ticket(cls, ticket: Any) -> Rfe:
        """Elaborate an existing rough issue: its title, body and discussion become the idea."""
        parts = [str(getattr(ticket, "title", "")), "", str(getattr(ticket, "description", ""))]
        comments = getattr(ticket, "comments", ()) or ()
        if comments:
            parts += ["", "Discussion:"]
            parts += [f"- {c}" for c in comments]
        return cls(idea="\n".join(parts).strip(), key=str(getattr(ticket, "key", "")))


class AiRfe:
    verb: ClassVar[Verb] = Verb.RFE
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READS_REPO, Capability.SPENDS_BUDGET}
    )

    def __init__(
        self,
        invoker_factory: Callable[[Any], AiInvoker] | None = None,
        *,
        policy: InvokePolicy | None = None,
        prompts: Mapping[str, type[RfePrompt]] | None = None,
        prompt_id: str = "rfe/drafter",
        tools: ToolSet | None = None,
        run_tool: ToolRunner | None = None,
        layers: PromptLayers | None = None,
    ) -> None:
        # No invoker by default: the model comes from `lockstep.models.route(<verb>, ...)`,
        # resolved per run off the context. Passing one is the seam for a custom registry,
        # gateway, or cassette provider.
        self.invoker_factory = invoker_factory
        # Injected like `prompts=` — usually `rfe_layers().plus(guardrails=...)` so the shipped
        # baseline stays underneath.
        self.layers = layers
        # One turn and no tools by default, the same honest default as triage: the drafter is
        # handed the whole idea in the prompt. A repository that wants the drafter to read the
        # code the idea touches passes `builtins.read_only(...)` and raises `max_turns` with it.
        self.policy = policy or InvokePolicy(max_turns=1)
        self.prompts: Mapping[str, type[RfePrompt]] = (
            dict(prompts) if prompts is not None else dict(RFE_PROMPTS)
        )
        self.prompt_id = prompt_id
        self.tools = tools
        self.run_tool = run_tool

    async def invoke(self, ctx: Any, inp: Rfe) -> Outcome[RfeDraft]:
        lens = self.prompts.get(self.prompt_id)
        if lens is None:
            return _blocked(
                "rfe.unknown_prompt",
                f"no rfe prompt named {self.prompt_id!r}; have {sorted(self.prompts)}",
            )
        if not inp.idea.strip():
            # The empty idea is refused before a token is spent: a draft of nothing would be the
            # model inventing a feature, which is the gold-plating failure with no requester at all.
            return _blocked("rfe.no_idea", "there is no idea to draft from; nothing was sent")

        prompt: RfePrompt = lens()
        layers: PromptLayers = self.layers if self.layers is not None else rfe_layers()
        package = ContextPackage(
            items=(
                ContextItem(
                    kind="idea",
                    content=inp.idea,
                    provenance=Provenance.UNTRUSTED_EXTERNAL,
                    path=inp.key,
                ),
            )
        )

        system = prompt.system(layers) + "\n\n" + schema_instruction(RFE_SCHEMA)
        messages = prompt.render(RfeParams(key=inp.key), package)

        from ...ai.bootstrap import routed_invoker

        factory = self.invoker_factory or routed_invoker(type(self).verb)
        invoker: AiInvoker = factory(ctx)
        try:
            invocation = await invoker.run(
                system=system,
                messages=messages,
                context=package,
                tools=self.tools,
                run_tool=self.run_tool,
                policy=self.policy,
            )
        except InvocationBlocked as e:
            return _blocked(e.reason, str(e))
        except EgressRefused as e:
            return _blocked(e.reason, str(e))
        except InvocationFailed as e:
            return _errored(e.reason, str(e), None)

        if invocation.truncated:
            return _errored(
                "rfe.truncated",
                f"the model stopped at the {self.policy.max_tokens}-token output cap with its "
                f"draft unfinished. Raise `InvokePolicy.max_tokens`.",
                invocation.cost,
            )

        try:
            parsed = parse(invocation.content)
        except SchemaError as e:
            return _errored("rfe.unparseable", str(e), invocation.cost)

        problems = validate(parsed.value, RFE_SCHEMA)
        if problems:
            return Outcome(
                status=Status.ERRORED,
                reason="rfe.schema_mismatch",
                cost=invocation.cost,
                findings=tuple(
                    Finding(id="rfe.schema_mismatch", message=p, severity=Severity.ERROR, blocking=True)
                    for p in problems
                ),
            )

        draft = _to_draft(parsed.value)
        findings = tuple(
            Finding(
                id="rfe.open_question",
                message=f"undecided before work can start: {q}",
                severity=Severity.WARNING,
                path=inp.key,
                blocking=False,
            )
            for q in draft.open_questions
        )
        injection_findings = tuple(
            Finding(
                id=f"injection.{f.name}",
                message=f"{f.severity}: {f.excerpt}",
                severity=Severity.ERROR if f.severity == "critical" else Severity.WARNING,
                blocking=False,
            )
            for f in invocation.findings
        )

        return Outcome(
            status=Status.SUCCEEDED,
            value=draft,
            findings=findings + injection_findings,
            cost=invocation.cost,
            decided=not invocation.exhausted,
            reason="exhausted" if invocation.exhausted else None,
        )


def _blocked(reason: str, message: str) -> Outcome[RfeDraft]:
    return Outcome.blocked_by(
        reason,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


def _errored(reason: str, message: str, cost: Any) -> Outcome[RfeDraft]:
    return Outcome(
        status=Status.ERRORED,
        reason=reason,
        cost=cost,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


def _to_draft(value: object) -> RfeDraft:
    data = value if isinstance(value, dict) else {}

    def _strs(name: str) -> tuple[str, ...]:
        raw = data.get(name)
        return tuple(str(x) for x in raw) if isinstance(raw, list) else ()

    return RfeDraft(
        title=str(data.get("title", "")),
        problem=str(data.get("problem", "")),
        proposal=str(data.get("proposal", "")),
        acceptance_criteria=_strs("acceptance_criteria"),
        open_questions=_strs("open_questions"),
        labels=_strs("labels"),
    )
