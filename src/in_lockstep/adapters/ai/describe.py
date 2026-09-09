"""Describe, backed by a model: what a change did, for the person who has to review it.

Thin like `triage.py` and for the same reasons — one untrusted document in, one structured answer
out, no tools, one turn — and here because of what a pull request the framework opened actually
said. #389's opening paragraph was the model reasoning about its own test mocks, because a
session's cover note is written to the framework at the end of its turn and gets rendered as though
it were written to a reader (#398).

The fix is not a better prompt for that session. It is a different reader: this verb is handed the
ticket, the diff and the verdict, and nothing of the conversation that produced them, so what it
knows is what the reviewer knows. A description written from the inside leaves out exactly what a
reader is missing, and cannot tell that it has.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from ...ai.context import ContextItem, ContextPackage, Provenance
from ...ai.invoker import InvocationBlocked, InvocationFailed, InvokePolicy, Invoker, ToolRunner
from ...ai.prompt import Composition, PromptLayers, compositions
from ...ai.structured import schema_instruction, settle
from ...ai.tools import ToolSet
from ...core.outcome import Cost, Finding, Outcome, Severity, Status
from ...core.verbs import Capability, Verb
from ...privileged.egress import EgressRefused
from ...prompts.describe import (
    DESCRIBE_PROMPTS,
    DESCRIBE_SCHEMA,
    DescribeParams,
    DescribePrompt,
    describe_layers,
)

#: How much of a diff is worth sending. A description is written from what a change DID, and a
#: reader of a 200k-character diff is not reading it either -- past a point the useful signal is
#: the shape, which the head of the diff carries. Bounded here rather than at the caller so every
#: caller gets the same bound, and said in the text so the model knows it saw a part.
MAX_DIFF_CHARS = 60_000


@dataclass(frozen=True)
class Description:
    """What a reviewer is told, before the framework's own measured lines."""

    summary: str = ""
    changes: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.summary or self.changes)


@dataclass(frozen=True)
class Describe:
    """The Describe request: one staged change, and the ticket it was made for.

    Frozen like every request type: it is hashed for step identity and serialized into
    checkpoints. `verdict` is the framework's own measured line, passed so the description does not
    contradict it -- not so the model can repeat it, which the body forbids.
    """

    key: str = ""
    title: str = ""
    ticket: str = ""
    diff: str = ""
    verdict: str = ""

    def render(self) -> str:
        """The ticket and the diff as one block, for the untrusted context item.

        Untrusted as a whole, and the diff is the reason it is not split: the ticket is written by
        whoever filed it, and the diff is written by a model that read the ticket. Neither is a
        document this framework authored, and marking one of them trusted would be marking a
        laundering path trusted.
        """
        diff = self.diff[:MAX_DIFF_CHARS]
        clipped = "\n\n[the diff continues past what was sent]" if len(self.diff) > MAX_DIFF_CHARS else ""
        lines = [f"# {self.key}: {self.title}".rstrip(), "", "## What was asked for", self.ticket or "(none)"]
        if self.verdict:
            lines += ["", "## What the framework measured (do not restate this)", self.verdict]
        lines += ["", "## The change", "```diff", diff + clipped, "```"]
        return "\n".join(lines)


class AiDescribe:
    verb: ClassVar[Verb] = Verb.DESCRIBE
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.SPENDS_BUDGET})

    def __init__(
        self,
        invoker_factory: Callable[[Any], Invoker] | None = None,
        *,
        policy: InvokePolicy | None = None,
        prompts: Mapping[str, type[DescribePrompt]] | None = None,
        prompt_id: str = "describe/change",
        tools: ToolSet | None = None,
        run_tool: ToolRunner | None = None,
        layers: PromptLayers | None = None,
    ) -> None:
        self.invoker_factory = invoker_factory
        self.layers = layers
        # One turn and no tools, and unlike the analyst's this is not a default a repository is
        # expected to raise: everything the answer is about is in the prompt. A describer that
        # could read the repository would be a describer that could describe something other than
        # the change.
        self.policy = policy or InvokePolicy(max_turns=1)
        self.prompts: Mapping[str, type[DescribePrompt]] = (
            dict(prompts) if prompts is not None else dict(DESCRIBE_PROMPTS)
        )
        self.prompt_id = prompt_id
        self.tools = tools
        self.run_tool = run_tool

    def compositions(self) -> dict[str, Composition]:
        return compositions(
            self.prompts,
            self.layers if self.layers is not None else describe_layers(),
            verb=str(type(self).verb),
            source=type(self).__name__,
        )

    async def invoke(self, ctx: Any, inp: Describe) -> Outcome[Description]:
        lens = self.prompts.get(self.prompt_id)
        if lens is None:
            return _blocked(
                "describe.unknown_prompt",
                f"no describe prompt named {self.prompt_id!r}; have {sorted(self.prompts)}",
            )
        if not inp.diff.strip():
            # Nothing to describe is not an error and not an empty description: it is a caller
            # asking about a change that does not exist, and the honest answer costs no turn.
            return _blocked("describe.no_diff", "there is no change to describe")

        prompt: DescribePrompt = lens()
        layers: PromptLayers = self.layers if self.layers is not None else describe_layers()
        package = ContextPackage(
            items=(
                ContextItem(
                    kind="change",
                    content=inp.render(),
                    provenance=Provenance.UNTRUSTED_EXTERNAL,
                    path=inp.key,
                ),
            )
        )
        system = prompt.system(layers) + "\n\n" + schema_instruction(DESCRIBE_SCHEMA)
        messages = prompt.render(DescribeParams(key=inp.key), package)

        from .strategy import resolve_invoker

        invoker: Invoker = resolve_invoker(self.invoker_factory, type(self).verb, ctx)
        try:
            invocation = await invoker.run(
                system=system,
                messages=messages,
                context=package,
                tools=self.tools,
                run_tool=self.run_tool,
                policy=self.policy,
                schema=DESCRIBE_SCHEMA,
            )
            settled = await settle(
                invoker,
                invocation,
                schema=DESCRIBE_SCHEMA,
                system=system,
                messages=messages,
                context=package,
                policy=self.policy,
            )
            invocation = settled.invocation
        except InvocationBlocked as e:
            return _blocked(e.reason, str(e))
        except EgressRefused as e:
            return _blocked(e.reason, str(e))
        except InvocationFailed as e:
            return _errored("describe.failed", str(e), None)

        if invocation.truncated:
            return _errored(
                "describe.truncated",
                f"the model stopped at the {self.policy.max_tokens}-token output cap with its "
                f"answer unfinished. Raise `InvokePolicy.max_tokens`.",
                invocation.cost,
            )
        if settled.reason in ("unparseable", "schema_mismatch"):
            # No salvage of the raw text here, deliberately, and it is the whole point of this
            # verb: an unparsed reply rendered as prose is what #389 published. The caller falls
            # back to what the run itself said, or to saying it has nothing.
            return _errored(f"describe.{settled.reason}", settled.detail, invocation.cost)

        value = settled.value if isinstance(settled.value, dict) else {}
        description = Description(
            summary=str(value.get("summary", "")).strip(),
            changes=_strings(value.get("changes")),
            risks=_strings(value.get("risks")),
        )
        return Outcome(
            status=Status.SUCCEEDED,
            value=description,
            cost=invocation.cost,
            findings=tuple(
                Finding(
                    id=f"injection.{f.name}",
                    message=f"{f.severity}: {f.excerpt}",
                    severity=Severity.ERROR if f.severity == "critical" else Severity.WARNING,
                )
                for f in invocation.findings
            ),
        )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(text for v in value if (text := str(v).strip()))


def _blocked(reason: str, message: str) -> Outcome[Description]:
    return Outcome(
        status=Status.BLOCKED,
        reason=reason,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
        decided=False,
    )


def _errored(reason: str, message: str, cost: Cost | None) -> Outcome[Description]:
    return Outcome(
        status=Status.ERRORED,
        reason=reason,
        cost=cost or Cost(),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
        decided=False,
    )
