"""Implement, backed by a model — and the first verb whose strategy is genuinely dispatched.

`AiReview` runs one lens and returns. Implementing is not that shape: how a change gets made is
the interesting variable, and the design says so — `strategy_id` is part of the eval subject key
precisely so that oneshot, tdd and direct can be measured against the same ticket rather than
argued about. So this adapter does not contain an approach. It assembles what an approach needs
and hands it to one selected from the registry.

That closes a real gap rather than adding indirection. `StrategyRegistry` shipped as a catalogue
of ids whose factories returned strings, and nothing anywhere resolved one — so a registration was
documentation. Here, `Registration.factory()` has to return something with `execute`, and a
registration that does not is refused by name instead of failing on an attribute error.

The capability declaration is the load-bearing line in this file. `WRITES_FILES` and
`EXECUTES_CODE` beside `SPENDS_BUDGET` is what makes the framework treat this as an agent with
agency rather than a read-only reviewer: `ApprovalGate` becomes a startup requirement, egress
enforcement becomes mandatory before the first call, and `Retry` refuses to re-invoke it. None of
that is configured here — it is all keyed off the declaration.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ...ai.builtins import CommandRunner, ToolRunnerImpl, Workspace, read_write_execute
from ...ai.context import ContextCurator, ContextItem, ContextNeed, ContextPackage
from ...ai.invoker import AiInvoker, InvokePolicy
from ...ai.prompt import PromptLayers
from ...ai.strategy import Registration, StrategyRefused, StrategyRegistry, UnknownStrategy
from ...ai.tools import ToolSet
from ...core.changes import ChangeGuard
from ...core.outcome import Finding, Outcome, Severity
from ...core.types import ChangeSet
from ...core.verbs import Capability, Verb
from ...prompts.implement import PROMPTS, ImplementPrompt, implement_layers

#: Enough turns to look before writing, which is the whole premise of this verb. A model that has
#: to search, read four files, write two and run the tests has spent a dozen turns before it has
#: done anything wrong.
#:
#: The ceiling is not free and the cost is not linear: every turn re-sends the accumulated history,
#: so turn N pays for everything read in turns 1..N-1. Forty is chosen against that curve rather
#: than against how long a model might like to keep going — and it is the backstop, not the budget.
#: `Spend.would_exceed` is what actually stops a run, checked before each turn against the
#: projected cost of making it, so a session hits the dollar ceiling long before this on any
#: repository large enough for it to matter.
DEFAULT_TURNS = 40

#: Big enough to write a whole file in one tool call, since `write_file` replaces a path's entire
#: contents and a truncated write is a corrupted file rather than a short answer. It is also the
#: number the per-turn spend projection bounds output by, so raising it raises the headroom every
#: turn must be able to afford — at Sonnet output rates this is roughly twelve cents of projection
#: per turn, which is what a budget has to leave room for.
DEFAULT_MAX_TOKENS = 8192


@dataclass(frozen=True)
class Implement:
    """The Implement request: what to implement, and how it was chosen. Workflows do
    `ctx.do(Implement(...))`; a binding decides what runs it.

    Frozen because it is hashed for step identity and serialized into checkpoints, like every
    other request type — a mutation after dispatch would change a key already written down.
    """

    #: The ticket, whose text is untrusted by construction: anyone who can file one can write
    #: into this prompt. `Ticket.as_context` is what tags it, and it is the only route in.
    ticket: Any
    #: Empty selects the registry's default for IMPLEMENT.
    strategy: str = ""
    #: True when the strategy id came from something attacker-influenceable — a ticket label, a
    #: comment. It is a separate field rather than inferred because inferring it wrongly is
    #: silent: `StrategyRegistry.select` refuses a privileged strategy on this flag, and a
    #: selection rule that forgot to set it would hand an injected ticket a path grant.
    untrusted_selection: bool = False
    paths: tuple[str, ...] = ()
    token_budget: int = 60_000


@dataclass(frozen=True)
class ImplementReport:
    """What a session produced. The change set is the deliverable; the rest is its cover note."""

    changeset: ChangeSet = field(default_factory=ChangeSet)
    summary: str = ""
    notes: tuple[str, ...] = ()
    #: What the model says it could not do. Carried as data rather than left in prose, because a
    #: partial change that names its gap is reviewable and one that reads as complete is not.
    unfinished: tuple[str, ...] = ()
    strategy: str = ""
    turns: int = 0

    @property
    def empty(self) -> bool:
        return not self.changeset.changes


@dataclass
class ImplementSession:
    """Everything a strategy needs, assembled once by the adapter.

    A strategy is registered with a zero-argument factory — that is what lets `default_registry()`
    stay a pure catalogue built at import time — so it cannot be constructed holding an invoker or
    a workspace. Those are run-scoped, and this is how they reach it.
    """

    invoker: AiInvoker
    workspace: Workspace
    tools: ToolSet
    run_tool: ToolRunnerImpl
    policy: InvokePolicy
    layers: PromptLayers
    prompts: Mapping[str, type[ImplementPrompt]]
    curator: ContextCurator
    guard: ChangeGuard
    repo_root: str = "."

    def context(self, spec: Implement) -> ContextPackage:
        """The ticket, curated. Provenance comes from `Ticket.as_context`, not from here."""
        items: list[ContextItem] = list(spec.ticket.as_context())
        return self.curator.curate(items, ContextNeed(token_budget=spec.token_budget))


class AiImplement:
    verb: ClassVar[Verb] = Verb.IMPLEMENT
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.READS_REPO,
            Capability.SPENDS_BUDGET,
            Capability.WRITES_FILES,
            Capability.EXECUTES_CODE,
        }
    )

    def __init__(
        self,
        invoker_factory: Callable[[Any], AiInvoker],
        *,
        registry: StrategyRegistry,
        strategy: str = "",
        repo_root: str = ".",
        policy: InvokePolicy | None = None,
        curator: ContextCurator | None = None,
        commands: CommandRunner | None = None,
        guard: ChangeGuard | None = None,
        workflow_id: str = "",
        prompts: Mapping[str, type[ImplementPrompt]] | None = None,
        layers: PromptLayers | None = None,
    ) -> None:
        self.invoker_factory = invoker_factory
        self.registry = registry
        # The binding's default strategy, so `lockstep.bind(Implement, AiImplement(...,
        # strategy="implement/tdd"))` says at the binding how implementing happens, and
        # `in-lockstep ls` can print it. A request that names its own strategy still wins;
        # empty falls through to the registry's default for the verb.
        self.strategy = strategy
        self.repo_root = repo_root
        self.policy = policy or InvokePolicy(max_turns=DEFAULT_TURNS, max_tokens=DEFAULT_MAX_TOKENS)
        self.curator = curator or ContextCurator()
        # No runner by default, so `run_script` refuses until a caller supplies one. The tool is
        # still declared and the capability is still visible to policy — see `read_write_execute`.
        self.commands = commands
        self.guard = guard or ChangeGuard()
        # Keyed on the workflow id, never the strategy id: a Tier-2 grant reachable through
        # strategy selection is a grant a ticket label can steer.
        self.workflow_id = workflow_id
        self.prompts: Mapping[str, type[ImplementPrompt]] = (
            dict(prompts) if prompts is not None else dict(PROMPTS)
        )
        # The layer stack around every prompt this adapter runs — a repository's own guardrails go
        # here, usually as `implement_layers().plus(guardrails=...)` so the shipped baseline stays
        # underneath. Injected like `prompts=`: prompt text is data, and the binding site in
        # lockstep.py is where data enters, visibly.
        self.layers = layers

    async def invoke(self, ctx: Any, inp: Implement) -> Outcome[ImplementReport]:
        try:
            # Precedence: the request's own strategy, then the binding's, then the registry's
            # default for the verb. The untrusted-selection gate applies to whichever won.
            registration = self.registry.select(
                Verb.IMPLEMENT,
                explicit=inp.strategy or self.strategy or None,
                from_untrusted_input=inp.untrusted_selection,
            )
        except StrategyRefused as e:
            return _blocked("implement.strategy_refused", str(e))
        except UnknownStrategy as e:
            return _blocked("implement.unknown_strategy", str(e))

        strategy = registration.factory()
        if not hasattr(strategy, "execute"):
            # The catalogue entries that predate executable strategies return a plain string.
            # Named rather than left to raise: "'str' object has no attribute 'execute'" sends
            # someone to read this file, where the answer is that the strategy was never written.
            return _blocked(
                "implement.strategy_not_executable",
                f"{registration.id!r} is registered as a catalogue entry, not an executable "
                f"strategy: its factory returned {type(strategy).__name__}, which has no "
                f"`execute`. Executable today: {', '.join(_executable(self.registry))}.",
            )

        session = self._session(ctx, registration)
        outcome: Outcome[ImplementReport] = await strategy.execute(ctx, session, inp)
        return outcome

    def _session(self, ctx: Any, registration: Registration) -> ImplementSession:
        workspace = Workspace(
            root=Path(self.repo_root),
            guard=self.guard,
            workflow_id=self.workflow_id,
        )
        tools, runner = read_write_execute(workspace, commands=self.commands)
        return ImplementSession(
            invoker=self.invoker_factory(ctx),
            workspace=workspace,
            tools=tools,
            run_tool=runner,
            policy=self.policy,
            layers=self.layers if self.layers is not None else implement_layers(),
            prompts=self.prompts,
            curator=self.curator,
            guard=self.guard,
            repo_root=self.repo_root,
        )


def _executable(registry: StrategyRegistry) -> list[str]:
    """Which IMPLEMENT registrations actually dispatch, for a refusal that is worth reading."""
    names = []
    for registration in registry.for_verb(Verb.IMPLEMENT):
        try:
            if hasattr(registration.factory(), "execute"):
                names.append(registration.id)
        except Exception:  # noqa: BLE001 - a broken factory is not this message's problem
            continue
    return names or ["(none)"]


def _blocked(reason: str, message: str) -> Outcome[ImplementReport]:
    return Outcome.blocked_by(
        reason,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
