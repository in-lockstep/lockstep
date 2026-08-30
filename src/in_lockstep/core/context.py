"""RunContext — the single seam through which all capability flows.

Which is also the single place to fake everything in tests. Context is passed explicitly; an
ambient contextvar exists for library authors, but user code threads `ctx`.

`ctx.do` is defined as `ctx.call` followed by `ctx.run_call`, not the other way round. Branches
have to be describable before they run, and an entry point that only ever executes eagerly would
have to be re-plumbed later to allow it.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from .container import Container
from .middleware import ActionCall, Middleware, Next, compose
from .outcome import Outcome, Status
from .ports import StepStore
from .spend import Spend
from .verbs import Verb, capabilities_of, verb_of

T = TypeVar("T")

DISABLE_ENV = "IN_LOCKSTEP_DISABLE"

_current: ContextVar[RunContext | None] = ContextVar("in_lockstep_run_context", default=None)


def current_context() -> RunContext | None:
    """Ambient access, for library authors. User code is encouraged to thread `ctx` explicitly."""
    return _current.get()


@dataclass(frozen=True)
class Approval:
    """Who asked for this run, and whether they were present while it ran.

    On `RunContext` rather than read from the environment by whoever needs it, because this is the
    seam where a project's maturity shows. Young: a person types the command and watches it. Older:
    an actor gate in CI verifies a commenter and the run is unattended. Same process, same
    invocation, different provenance for the grant — and if the two are plumbed differently, the
    transition means rewriting the process rather than re-triggering it.

    `attended` is a fact about the run, not a level of trust. A person at a terminal approving
    their own run is weaker than an environment approval and stronger than nothing; recording which
    it was lets the ledger say so instead of implying they are the same.
    """

    by: str = ""
    attended: bool = False

    @property
    def granted(self) -> bool:
        return bool(self.by.strip())

    def as_record(self) -> dict[str, object]:
        return {"by": self.by, "attended": self.attended}


@dataclass(frozen=True)
class RepoFacts:
    """What detection found in the tree, so the drop-in defaults fit the repository instead of
    assuming Python.

    Pure data: the filesystem read happens in the lockstep layer (`Lockstep.detect`), and this is
    the result it hands to the composition root, which decides what to bind. A default, never a
    verdict — everything here is overridable, and an explicit bind in `lockstep.py` wins over any
    binding derived from it.
    """

    stack: str = ""  # "python" | "node" | "" when neither is recognised
    pytest: bool = False
    test_command: tuple[str, ...] = ()  # a generic runner argv, e.g. ("npm", "test")
    ruff: bool = False
    eslint: bool = False
    lint_command: tuple[str, ...] = ()  # a generic linter argv, e.g. ("npx", "eslint", ".")
    dockerfile: bool = False
    makefile: bool = False
    make_targets: tuple[str, ...] = ()
    coverage: bool = False
    ci_host: str = ""  # "github" | "gitlab" | ""
    readme: bool = False
    docs: bool = False
    agent_instructions: tuple[str, ...] = ()  # e.g. ("CLAUDE.md", "AGENTS.md")

    def summary(self) -> tuple[str, ...]:
        """A human-readable list of what was found, for `ls` and `doctor`."""
        out: list[str] = []
        if self.stack:
            out.append(f"stack: {self.stack}")
        if self.pytest:
            out.append("tests: pytest")
        elif self.test_command:
            out.append(f"tests: {' '.join(self.test_command)}")
        if self.ruff:
            out.append("lint: ruff")
        elif self.lint_command:
            out.append(f"lint: {' '.join(self.lint_command)}")
        if self.coverage:
            out.append("coverage config")
        if self.dockerfile:
            out.append("Dockerfile")
        if self.makefile:
            targets = f" ({', '.join(self.make_targets)})" if self.make_targets else ""
            out.append(f"Makefile{targets}")
        if self.ci_host:
            out.append(f"ci: {self.ci_host}")
        if self.docs:
            out.append("docs/")
        for name in self.agent_instructions:
            out.append(name)
        return tuple(out)


@dataclass(frozen=True)
class RepoInfo:
    root: str
    head: str = ""
    branch: str = ""
    dirty: bool = False
    facts: RepoFacts = field(default_factory=RepoFacts)


@dataclass(frozen=True)
class StepId:
    """Identity of one step, for checkpointing and cassette lookup.

    `scope_path` is empty at 1.0 and is the reason this is a structure rather than a string: when
    branches arrive, each needs its own key space, and a flat one would invalidate every cassette
    recorded before it.
    """

    scope_path: str
    call_site: str
    input_hash: str

    def __str__(self) -> str:
        prefix = f"{self.scope_path}/" if self.scope_path else ""
        return f"{prefix}{self.call_site}#{self.input_hash[:12]}"


def _hash_input(value: object) -> str:
    try:
        import json

        payload = json.dumps(value, sort_keys=True, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        payload = repr(value)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class RunContext:
    run_id: str
    repo: RepoInfo
    container: Container
    spend: Spend = field(default_factory=Spend)
    middleware: list[Middleware] = field(default_factory=list)
    tracer: Any = None
    scope_path: str = ""
    parent_run_id: str | None = None
    # Set to a StateStore to make steps resumable. Opt-out-able: without one the model is "just a
    # Python function", which is the simplicity the whole design trades on.
    state: StepStore | None = None
    recovering: bool = False
    #: Who asked for this run. Empty means nobody did, which `ApprovalGate` treats as no grant.
    approval: Approval = field(default_factory=Approval)
    #: The per-verb model routes, snapshotted from `lockstep.models.routes` at `context()` time.
    #: This is how an AI adapter bound with no explicit invoker finds its model: the snapshot
    #: happens after the whole module executed, so a `models.route(...)` line may appear before
    #: or after the bind that relies on it.
    models: dict[str, str] = field(default_factory=dict)
    _step_counts: dict[str, int] = field(default_factory=dict, repr=False)
    last_step: StepId | None = None
    last_capabilities: frozenset[Any] = frozenset()

    # -- declaring and running work ------------------------------------------------

    def call(
        self,
        request: object,
        *,
        step: str | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> ActionCall:
        """Declare a request without running it.

        The request object is the whole ask: its type is what the container resolves an adapter
        for, and its fields are the payload — `ctx.call(Review(base=..., head=...))`.
        """
        return ActionCall(request, step=step, middleware=middleware)

    async def run_call(self, call: ActionCall) -> Outcome[Any]:
        """Resolve the bound adapter, wrap it in the chain, and record the step."""
        # The kill switch is checked before anything else, including middleware. It is not part
        # of the chain, so `--no-middleware` cannot reach past it and neither can a bug in a
        # layer that would otherwise run first.
        if os.environ.get(DISABLE_ENV):
            return Outcome(
                status=Status.BLOCKED,
                reason="killswitch",
            )

        action: Any = self.container.resolve(call.iface)
        call.verb = verb_of(action)
        step_id = self._step_id(call)
        capabilities = capabilities_of(action)

        # A completed step is replayed rather than re-run. The checkpoint records the OUTCOME,
        # not merely that a file appeared: a step that wrote half its output before the runner
        # died must re-run, and presence alone cannot tell those apart.
        if self.recovering and self.state is not None:
            existing = self.state.load_step(self.run_id, str(step_id))
            if isinstance(existing, Outcome):
                self.last_step = step_id
                return existing

        started = time.monotonic()

        async def terminal() -> Outcome[Any]:
            result: Outcome[Any] = await action.invoke(self, call.input)
            # Charged here, innermost, rather than after the chain unwinds — otherwise a
            # middleware reconciling actual spend against its ceiling looks at the accumulator
            # before this call was ever added to it, and every overrun reads as within budget.
            if result.cost.wall_seconds == 0.0:
                result = result.with_cost(replace(result.cost, wall_seconds=time.monotonic() - started))
            self.spend.charge(result.cost)
            return result

        chain: Next = compose([*self.middleware, *call.middleware], terminal, self, call)
        outcome = await chain()

        if self.state is not None and outcome.terminal:
            self.state.save_step(self.run_id, str(step_id), outcome)

        self.last_step = step_id
        self.last_capabilities = capabilities
        return outcome

    async def do(
        self,
        request: object,
        *,
        step: str | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> Outcome[Any]:
        """Declare and run — `await ctx.do(Review(base=..., head=...))`.

        The composition of `call` and `run_call`, and nothing more.
        """
        return await self.run_call(self.call(request, step=step, middleware=middleware))

    # -- step identity -------------------------------------------------------------

    def _step_id(self, call: ActionCall) -> StepId:
        if call.step:
            call_site = call.step
        else:
            verb = call.verb.value if call.verb else call.iface.__name__.lower()
            seen = self._step_counts.get(verb, 0)
            self._step_counts[verb] = seen + 1
            call_site = verb if seen == 0 else f"{verb}.{seen}"
        return StepId(
            scope_path=self.scope_path,
            call_site=call_site,
            input_hash=_hash_input(call.input),
        )

    def bind_current(self) -> None:
        _current.set(self)


def killswitch_engaged() -> bool:
    return bool(os.environ.get(DISABLE_ENV))


__all__ = [
    "DISABLE_ENV",
    "RepoInfo",
    "RunContext",
    "StepId",
    "Verb",
    "current_context",
    "killswitch_engaged",
]
