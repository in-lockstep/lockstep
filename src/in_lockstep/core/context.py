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


#: The standing-instruction files a repository writes for whatever agent is working in it, in the
#: order they are read. Declared once so detection and reading cannot drift apart — they did: the
#: names were a literal inside `Lockstep.detect` and the only consumer was `ls`, so the framework
#: reported finding a CLAUDE.md and then never opened it.
#:
#: All present files are read, not the first match. `AGENTS.md` is the vendor-neutral spelling that
#: opencode and other clients read, `CLAUDE.md` is Claude Code's, and a repository that supports
#: both keeps both — often with different content. Picking a winner would silently drop half of
#: what somebody wrote down.
AGENT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules")

#: How many Makefile targets `RepoFacts.summary()` names before saying "+N more".
MAKE_TARGETS_SHOWN = 8


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
    build_command: tuple[str, ...] = ()  # e.g. ("make", "build") or ("npm", "run", "build")
    run_command: tuple[str, ...] = ()  # e.g. ("make", "run") or ("npm", "start")
    #: The steps that build the repository's own environment, in order, e.g.
    #: (("uv", "sync", "--locked"), ("npm", "ci")). Plural because a Python service with a Node
    #: front end has both, and each is one lockfile's own install.
    provision_commands: tuple[tuple[str, ...], ...] = ()
    dockerfile: bool = False
    makefile: bool = False
    make_targets: tuple[str, ...] = ()
    coverage: bool = False
    ci_host: str = ""  # "github" | "gitlab" | ""
    readme: bool = False
    docs: bool = False
    agent_instructions: tuple[str, ...] = ()  # names only; the contents are read per run

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
        if self.build_command:
            out.append(f"build: {' '.join(self.build_command)}")
        if self.run_command:
            out.append(f"run: {' '.join(self.run_command)}")
        if self.provision_commands:
            # `then`, because the line `ls` prints joins these facts with semicolons.
            steps = " then ".join(" ".join(step) for step in self.provision_commands)
            out.append(f"provision: {steps}")
        if self.coverage:
            out.append("coverage config")
        if self.dockerfile:
            out.append("Dockerfile")
        if self.makefile:
            # The first few, for a line a person reads. The fact itself is the whole list, because
            # a `build` target that happens to be ninth in the file is still a build target.
            shown = list(self.make_targets[:MAKE_TARGETS_SHOWN])
            more = len(self.make_targets) - len(shown)
            if more > 0:
                shown.append(f"+{more} more")
            targets = f" ({', '.join(shown)})" if shown else ""
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


@dataclass(frozen=True)
class StepOutcome:
    """One step's outcome, kept on the context so the run's record can be derived from what ran.

    A workflow returns whatever it likes, and most return a dict or nothing. The record used to
    stamp those runs `"completed"`, a word the status set does not contain, so a selfcheck whose
    test step went red was reported as not failed: the failure was not misclassified, it was not on
    the record at all. The steps are the evidence; this is where they wait to be written down.
    """

    step: str
    verb: str
    outcome: Outcome[Any]

    def as_record(self, *, max_findings: int = 20) -> dict[str, object]:
        outcome = self.outcome
        record: dict[str, object] = {
            "step": self.step,
            "verb": self.verb,
            "status": outcome.status.value,
            "decided": outcome.decided,
            "wall_seconds": round(outcome.cost.wall_seconds, 3),
            "findings": {
                "count": len(outcome.findings),
                "items": [f.as_record() for f in outcome.findings[:max_findings]],
            },
        }
        if outcome.reason:
            record["reason"] = outcome.reason
        return record


#: How many `run_call`s deep the current task is. Zero means the workflow itself is calling.
_call_depth: ContextVar[int] = ContextVar("in_lockstep_call_depth", default=0)

#: The order in which a step's status decides the run's: a control stopping a step is the fact
#: about the run, above infrastructure breaking, above the domain saying no. A step that
#: succeeded decides nothing on its own.
_VERDICT_PRECEDENCE = (Status.BLOCKED, Status.ERRORED, Status.FAILED)


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
    #: The steps the workflow itself asked for, in order, replayed checkpoints included. What the
    #: record derives its verdict from when the workflow returned no `Outcome` of its own. Top
    #: level only: a step an adapter runs inside one of these (a strategy's mid-loop test probe,
    #: a reproducer expected to fail) is the adapter's business, and the adapter's own outcome is
    #: its statement about it. Counting those would fail a run whose implementing step succeeded.
    steps: list[StepOutcome] = field(default_factory=list, repr=False)

    def verdict(self) -> tuple[Status, str | None, bool]:
        """How this run ended, read off its steps: status, the deciding step's reason, decided.

        For a workflow that returned no `Outcome`. Any blocked step makes the run blocked, else any
        errored step makes it errored, else any failed step makes it failed, else it succeeded; the
        reason is the deciding step's; and the run decided something only if every step did. A run
        that ran no steps succeeded at nothing in particular, which is still not a failure.
        """
        for status in _VERDICT_PRECEDENCE:
            for step in self.steps:
                if step.outcome.status is status:
                    return status, step.outcome.reason, all(s.outcome.decided for s in self.steps)
        return Status.SUCCEEDED, None, all(s.outcome.decided for s in self.steps)

    # -- declaring and running work ------------------------------------------------

    def call(
        self,
        request: object,
        *,
        via: object | None = None,
        step: str | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> ActionCall:
        """Declare a request without running it.

        The request object is the whole ask: its type is what the container resolves an adapter
        for, and its fields are the payload — `ctx.call(Review(base=..., head=...))`. `via=`
        names the adapter for this call instead — see `do`.
        """
        return ActionCall(request, via=via, step=step, middleware=middleware)

    async def run_call(self, call: ActionCall) -> Outcome[Any]:
        """Resolve the bound adapter, wrap it in the chain, and record the step."""
        # The kill switch is checked before anything else, including middleware. It is not part
        # of the chain, so `--no-middleware` cannot reach past it and neither can a bug in a
        # layer that would otherwise run first.
        # Whether this call is the workflow's own or one an adapter makes inside it. A contextvar
        # rather than a counter on the context, because a fan-out runs top-level steps in
        # concurrent tasks and each task carries its own copy.
        top_level = _call_depth.get() == 0
        if os.environ.get(DISABLE_ENV):
            refused: Outcome[Any] = Outcome(status=Status.BLOCKED, reason="killswitch")
            if top_level:
                self.steps.append(
                    StepOutcome(step=call.step or call.iface.__name__.lower(), verb="", outcome=refused)
                )
            return refused

        # A call-scoped adapter wins over the container binding — the call site said `via=`, and
        # code in the lifecycle module is exactly who may decide that. The container is never
        # touched, so the choice cannot leak into later calls.
        action: Any = call.via if call.via is not None else self.container.resolve(call.iface)
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
                if top_level:
                    self.steps.append(
                        StepOutcome(step=step_id.call_site, verb=_verb_name(call), outcome=existing)
                    )
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
        depth = _call_depth.set(_call_depth.get() + 1)
        try:
            outcome = await chain()
        finally:
            _call_depth.reset(depth)

        if self.state is not None and outcome.terminal:
            self.state.save_step(self.run_id, str(step_id), outcome)

        if top_level:
            self.steps.append(StepOutcome(step=step_id.call_site, verb=_verb_name(call), outcome=outcome))
        self.last_step = step_id
        self.last_capabilities = capabilities
        return outcome

    async def do(
        self,
        request: object,
        *,
        via: object | None = None,
        step: str | None = None,
        middleware: Sequence[Middleware] | None = None,
    ) -> Outcome[Any]:
        """Declare and run — `await ctx.do(Review(base=..., head=...))`.

        `via=` binds at the call, for this call only: `ctx.do(Implement(...), via=TDD())` says
        right at the execution site what serves the request, without consulting or mutating the
        container. The same capability-keyed middleware gates it either way. The startup
        refusals (`UngatedAgency`, the budget checks) scan *bound* adapters, so `via=` is an
        override for a verb the module binds, not a way to run a spender the module never
        declared.

        The composition of `call` and `run_call`, and nothing more.
        """
        return await self.run_call(self.call(request, via=via, step=step, middleware=middleware))

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


def _verb_name(call: ActionCall) -> str:
    return call.verb.value if call.verb else call.iface.__name__.lower()


def killswitch_engaged() -> bool:
    return bool(os.environ.get(DISABLE_ENV))


__all__ = [
    "DISABLE_ENV",
    "RepoInfo",
    "RunContext",
    "StepId",
    "StepOutcome",
    "Verb",
    "current_context",
    "killswitch_engaged",
]
