"""Triage, backed by a model.

Thin by design, exactly like `review.py`: it assembles the issue as one untrusted context item,
runs the analyst prompt through the invoker, and maps the structured answer onto a decision.
Everything the invoker owns — the loop bound, the spend ceiling, the injection scan, egress — is
not re-implemented here, so a third read-only AI verb would be a fourth copy of nothing.

An issue is authored by whoever filed it, and anyone who can file one can write into a prompt, so
the whole issue travels as `UNTRUSTED_EXTERNAL`. The `triage` guardrail denies the issue-writing
tools; this verb reads and reports, and acting on the verdict (a comment, a label, a duplicate
link) is a separate step the caller takes through `TicketSource`.
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
from ...prompts.triage import TRIAGE_PROMPTS, TRIAGE_SCHEMA, TriageParams, TriagePrompt, triage_layers


class Triage:
    """The verb interface. Workflows ask for this; a binding decides what serves it."""


@dataclass(frozen=True)
class TriageDecision:
    kind: str
    priority: str
    reason: str = ""
    missing: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    comment: str = ""
    duplicate_of: str = ""

    @property
    def actionable(self) -> bool:
        """Whether the issue can be worked as it stands — placed, and nothing missing."""
        return self.kind not in ("", "unclear") and not self.missing


@dataclass(frozen=True)
class TriageSpec:
    """One issue to place. Frozen like every other verb spec: it is hashed for step identity and
    serialized into checkpoints, so a mutation after dispatch would rewrite a key already written.

    The fields mirror what the analyst prompt reads and what the eval corpus supplies. `criteria`
    and `criteria_source` travel together because the format skill treats criteria a parser
    guessed differently from criteria a human typed into a field."""

    key: str
    summary: str = ""
    description: str = ""
    discussion: tuple[tuple[str, str], ...] = ()  # (author, body)
    labels: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    criteria_source: str = "none"

    @classmethod
    def from_ticket(cls, ticket: Any) -> TriageSpec:
        """Build a spec from a `platform.tickets.Ticket`. The tracker's comments become the
        discussion; the criteria source is what `criteria_from` could tell, which for a plain
        GitHub body is the reporter's prose rather than a field somebody filled in."""
        criteria = tuple(getattr(ticket, "acceptance_criteria", ()) or ())
        return cls(
            key=str(getattr(ticket, "key", "")),
            summary=str(getattr(ticket, "title", "")),
            description=str(getattr(ticket, "description", "")),
            discussion=tuple(("", str(c)) for c in getattr(ticket, "comments", ()) or ()),
            labels=tuple(getattr(ticket, "labels", ()) or ()),
            acceptance_criteria=criteria,
            criteria_source="description" if criteria else "none",
        )

    def render(self) -> str:
        """The issue as one text block, for the untrusted context item."""
        lines = [f"# {self.key}: {self.summary}".rstrip(), ""]
        if self.labels:
            lines.append(f"labels: {', '.join(self.labels)}")
        lines.append(f"criteria_source: {self.criteria_source}")
        lines += ["", "## Description", self.description or "(none)"]
        if self.acceptance_criteria:
            lines += ["", "## Acceptance criteria"]
            lines += [f"- {c}" for c in self.acceptance_criteria]
        if self.discussion:
            lines += ["", "## Discussion"]
            for author, body in self.discussion:
                who = f"{author}: " if author else ""
                lines.append(f"- {who}{body}")
        return "\n".join(lines)


class AiTriage:
    verb: ClassVar[Verb] = Verb.TRIAGE
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READS_REPO, Capability.SPENDS_BUDGET}
    )

    def __init__(
        self,
        invoker_factory: Callable[[Any], AiInvoker],
        *,
        policy: InvokePolicy | None = None,
        prompts: Mapping[str, type[TriagePrompt]] | None = None,
        prompt_id: str = "triage/analyst",
        tools: ToolSet | None = None,
        run_tool: ToolRunner | None = None,
        layers: PromptLayers | None = None,
    ) -> None:
        self.invoker_factory = invoker_factory
        # Injected like `prompts=` — see AiImplement, which carries the reasoning; usually
        # `triage_layers().plus(guardrails=...)` so the shipped baseline stays underneath.
        self.layers = layers
        # One turn and no tools by default, the honest default rather than a gap: the analyst is
        # handed the whole issue in the prompt, so a second turn has nothing to do with a tool
        # result and would only cost. A repository that wants the analyst to search for duplicates
        # passes `builtins.read_only(...)` here and raises `max_turns` with it — the same seam
        # `AiReview` documents, and it is a real seam because `invoke` threads these through.
        self.policy = policy or InvokePolicy(max_turns=1)
        self.prompts: Mapping[str, type[TriagePrompt]] = (
            dict(prompts) if prompts is not None else dict(TRIAGE_PROMPTS)
        )
        self.prompt_id = prompt_id
        self.tools = tools
        self.run_tool = run_tool

    async def invoke(self, ctx: Any, inp: TriageSpec) -> Outcome[TriageDecision]:
        lens = self.prompts.get(self.prompt_id)
        if lens is None:
            return _blocked(
                "triage.unknown_prompt",
                f"no triage prompt named {self.prompt_id!r}; have {sorted(self.prompts)}",
            )

        prompt: TriagePrompt = lens()
        layers: PromptLayers = self.layers if self.layers is not None else triage_layers()
        package = ContextPackage(
            items=(
                ContextItem(
                    kind="ticket",
                    content=inp.render(),
                    provenance=Provenance.UNTRUSTED_EXTERNAL,
                    path=inp.key,
                ),
            )
        )

        system = prompt.system(layers) + "\n\n" + schema_instruction(TRIAGE_SCHEMA)
        messages = prompt.render(TriageParams(key=inp.key), package)

        invoker: AiInvoker = self.invoker_factory(ctx)
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
            # A control refusing is what BLOCKED means, and routing it through an Outcome is what
            # gets it a ledger record — the same reasoning as `AiReview`.
            return _blocked(e.reason, str(e))
        except InvocationFailed as e:
            # ERRORED, not BLOCKED: a provider that could not answer is infrastructure, not a
            # policy refusal. The message arrives already redacted from the invoker.
            return _errored(e.reason, str(e), None)

        if invocation.truncated:
            return _errored(
                "triage.truncated",
                f"the model stopped at the {self.policy.max_tokens}-token output cap with its "
                f"answer unfinished. Raise `InvokePolicy.max_tokens`.",
                invocation.cost,
            )

        try:
            parsed = parse(invocation.content)
        except SchemaError as e:
            return _errored("triage.unparseable", str(e), invocation.cost)

        problems = validate(parsed.value, TRIAGE_SCHEMA)
        if problems:
            return Outcome(
                status=Status.ERRORED,
                reason="triage.schema_mismatch",
                cost=invocation.cost,
                findings=tuple(
                    Finding(id="triage.schema_mismatch", message=p, severity=Severity.ERROR, blocking=True)
                    for p in problems
                ),
            )

        decision = _to_decision(parsed.value)
        findings = tuple(
            Finding(
                id="triage.missing",
                message=f"missing before this can be worked: {item}",
                severity=Severity.WARNING,
                path=inp.key,
                blocking=False,
            )
            for item in decision.missing
        )
        # Anything the injection scanner saw in the issue travels with the outcome: an issue that
        # tried to talk to the analyst is a fact about the issue.
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
            value=decision,
            findings=findings + injection_findings,
            cost=invocation.cost,
            decided=not invocation.exhausted,
            reason="exhausted" if invocation.exhausted else None,
        )


def _blocked(reason: str, message: str) -> Outcome[TriageDecision]:
    return Outcome.blocked_by(
        reason,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


def _errored(reason: str, message: str, cost: Any) -> Outcome[TriageDecision]:
    return Outcome(
        status=Status.ERRORED,
        reason=reason,
        cost=cost,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


def _to_decision(value: object) -> TriageDecision:
    data = value if isinstance(value, dict) else {}

    def _strs(name: str) -> tuple[str, ...]:
        raw = data.get(name)
        return tuple(str(x) for x in raw) if isinstance(raw, list) else ()

    return TriageDecision(
        kind=str(data.get("kind", "")),
        priority=str(data.get("priority", "")),
        reason=str(data.get("reason", "")),
        missing=_strs("missing"),
        acceptance_criteria=_strs("acceptance_criteria"),
        labels=_strs("labels"),
        comment=str(data.get("comment", "")),
        duplicate_of=str(data.get("duplicate_of", "")),
    )
