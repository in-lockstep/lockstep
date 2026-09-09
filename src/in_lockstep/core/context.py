"""RunContext — the single seam through which all capability flows.

Which is also the single place to fake everything in tests. Context is passed explicitly; an
ambient contextvar exists for library authors, but user code threads `ctx`.

`ctx.do` is defined as `ctx.call` followed by `ctx.run_call`, not the other way round. Branches
have to be describable before they run, and an entry point that only ever executes eagerly would
have to be re-plumbed later to allow it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

from .container import Container
from .human import (
    HumanBoundary,
    HumanBranch,
    LocalStoreCannotPark,
    Parked,
    barrier_key,
    barrier_record,
    dumps,
)
from .middleware import ActionCall, Middleware, Next, compose
from .outcome import VERDICT_PRECEDENCE, Finding, JoinResult, Outcome, Severity, Status
from .ports import InferenceLog, LedgerStore, StepStore
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

#: The files in which a repository states how it builds, tests and runs itself, and which
#: `_detect_facts` therefore opens.
#:
#: Named here rather than only in the reader because O1's second half needs it: *detection that
#: guesses is worse than detection that declines*, and a decline that does not say what was looked
#: for leaves an adopter unable to tell an unsupported stack from a misconfigured one. This is the
#: list `RepoFacts.declined()` prints, and `test_detect.py` holds it to being true — every name
#: here, alone in a directory, must produce a fact, or the decline is advertising a file nothing
#: reads.
BUILD_MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "package.json",
    "Makefile",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    # The seven `GATE-TOOLING-3` named, read since Phase 7 (PR-17). Two are globs, because a
    # .NET project file carries the project's own name; `manifest_present` reads either spelling.
    "Gemfile",
    "Rakefile",
    "composer.json",
    "mix.exs",
    "*.csproj",
    "*.sln",
    "CMakeLists.txt",
    "Package.swift",
    "BUILD.bazel",
)


def manifest_present(directory: Path, name: str) -> bool:
    """Whether one advertised manifest is in `directory`: a file by that name, or for the two
    glob entries any file matching. One reader for the root and for the one-level-down scan, so
    the decline cannot advertise a spelling detection does not read."""
    if "*" in name:
        return any(directory.glob(name))
    return (directory / name).exists()


#: What a stack that is here and bound nothing is told, beside the generic sentence. Each names
#: the one line in the tree that would have made it servable, because a repository that DID
#: state how it builds must not be sent to fix the thing that is not broken.
STACK_HINTS = {
    "jvm": "a Maven or Gradle repository needs its ./mvnw or ./gradlew committed",
    "ruby": "a Rakefile with a `test` task (`task :test` or `Rake::TestTask`) binds `bundle exec rake test`",
    "php": "a `test` entry under composer.json `scripts` binds `composer test`",
    "cpp": "`enable_testing()` in CMakeLists.txt binds `ctest`",
    "bazel": "a WORKSPACE or MODULE.bazel beside BUILD.bazel binds `bazel test //...`",
}


@dataclass(frozen=True)
class RepoFacts:
    """What detection found in the tree, so the drop-in defaults fit the repository instead of
    assuming Python.

    Pure data: the filesystem read happens in the lockstep layer (`Lockstep.detect`), and this is
    the result it hands to the composition root, which decides what to bind. A default, never a
    verdict — everything here is overridable, and an explicit bind in `lockstep.py` wins over any
    binding derived from it.
    """

    #: The ecosystems whose manifests are in the tree, comma-joined in a fixed order:
    #: `"python"`, `"node"`, `"rust"`, `"go"`, `"jvm"`, `"ruby"`, `"php"`, `"elixir"`,
    #: `"dotnet"`, `"swift"`, `"bazel"`, `"cpp"`, or `""` when none is recognised. A list
    #: rather than a winner, because a Go service with a package.json front end is two true facts
    #: and picking one would report a repository as something it is only half of. Display only —
    #: what gets bound is decided per verb below, where the precedence is written down.
    stack: str = ""
    pytest: bool = False
    test_command: tuple[str, ...] = ()  # a generic runner argv, e.g. ("npm", "test")
    ruff: bool = False
    eslint: bool = False
    lint_command: tuple[str, ...] = ()  # a generic linter argv, e.g. ("npx", "eslint", ".")
    #: What this repository runs to REPAIR what `lint_command` reports -- its own `make fmt`, or
    #: whatever it calls that. Bound alongside the check so a strategy can repair a formatting
    #: finding without spending a model turn on it; empty where the repository declares no such
    #: target, which is not something to invent (`--fix` means nothing to `make lint`).
    fix_command: tuple[str, ...] = ()
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
    #: Build manifests found one directory below the root, as `dir/name`. Detection reads the
    #: root and nothing else, and for as long as the decline did not say so a monorepo whose
    #: service lives in `backend/` read as an unsupported stack (#316).
    below: tuple[str, ...] = ()

    def declined(self) -> str:
        """What was looked for, when nothing found here can serve a verb. Empty otherwise.

        O1's second half — *detection that guesses is worse than detection that declines* — is
        only half a rule while the decline is silent. `ls` printed what was found and never what
        was sought, so a Rust repository before `BUILD_MANIFESTS` grew a `Cargo.toml` entry and a
        genuinely unsupported one produced the same output: nothing. An adopter could not tell
        which they had, and the honest answer costs one line.

        The predicate is *can any verb be served*, not *was anything found at all*. A repository
        with a Dockerfile, a README and no build manifest has facts worth printing and still
        cannot be bound, and that is exactly the case where the decline is the useful half.

        Three cases rather than one sentence, because one sentence was wrong in two of them. A
        `pom.xml` with no `./mvnw` beside it *does* state how the repository builds, and telling
        that adopter "nothing here states how this repository builds" would send them to fix the
        thing that is not broken. What they need to hear is that the wrapper is missing.
        """
        servable = (
            self.pytest
            or self.test_command
            or self.ruff
            or self.lint_command
            or self.build_command
            or self.run_command
            or self.provision_commands
        )
        if servable:
            return ""
        if self.stack:
            # Reaching here with `jvm` in the stack means no usable wrapper: `pom.xml` and
            # `build.gradle` bind their commands only when `./mvnw` or `./gradlew` is on disk, so
            # had one been there this would have been servable. The same shape for every stack
            # whose manifest binds only on a line inside it: the hint names that line.
            hints = [STACK_HINTS[name] for name in self.stack.split(", ") if name in STACK_HINTS]
            hint = f" ({'; '.join(hints)})" if hints else ""
            return f"{self.stack} is here and named no test, lint, build or run command{hint}"
        if self.makefile:
            return "a Makefile is here with no test, lint, build or run target"
        found = (
            f"; found {', '.join(self.below)} one level down, which detection does not read -- run from "
            f"that directory, or bind the verbs by hand"
            if self.below
            else ""
        )
        return (
            f"nothing at the repository root states how this repository builds; looked for "
            f"{', '.join(BUILD_MANIFESTS)}{found}"
        )

    def summary(self) -> tuple[str, ...]:
        """A human-readable list of what was found, for `ls` and `doctor`."""
        out: list[str] = []
        if self.stack:
            out.append(f"stack: {self.stack}")
        if self.pytest:
            out.append("tests: pytest")
        elif self.test_command:
            out.append(f"tests: {' '.join(self.test_command)}")
        if self.lint_command:
            out.append(f"lint: {' '.join(self.lint_command)}")
            if self.fix_command:
                out.append(f"fix: {' '.join(self.fix_command)}")
        elif self.ruff:
            out.append("lint: ruff")
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
            # The step's own bill, so a step that is a comparable unit on its own -- one lens of
            # a fan-out -- can be compared without borrowing the run's total. Measured, not
            # inferred: the invoker charged this outcome, and a deterministic step's zero is a
            # measured zero.
            "cost_usd": round(outcome.cost.usd, 6),
            "tokens": outcome.cost.total_tokens,
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
    #: Where this run's model calls are kept, when `--record` asked for them to be. One object for
    #: the whole run: a workflow's steps each build their own invoker, and two of them saving the
    #: same tape would lose whichever wrote first. `None` is a run that records nothing, which is
    #: every run that did not ask.
    recording: InferenceLog | None = None
    #: The store this run's record goes to, as the port and never an implementation: what a
    #: branch that parks will write its barrier record through, and what `fan_out` refuses to
    #: park on when its `scope` is LOCAL (`GATE-OUT-5`, `GATE-OUT-6`). None on a hand-built
    #: context, which is a run that records nowhere and can park nowhere.
    ledger: LedgerStore | None = None
    recovering: bool = False
    #: Who asked for this run. Empty means nobody did, which `ApprovalGate` treats as no grant.
    approval: Approval = field(default_factory=Approval)
    #: The per-verb model routes, snapshotted from `lockstep.models.routes` at `context()` time.
    #: This is how an AI adapter bound with no explicit invoker finds its model: the snapshot
    #: happens after the whole module executed, so a `models.route(...)` line may appear before
    #: or after the bind that relies on it.
    models: dict[str, str] = field(default_factory=dict)
    #: How many times a workflow may re-attempt a ticket before it stops asking. Snapshotted from
    #: the facade at `context()` time for the same reason `models` is: a workflow shipped BY the
    #: framework cannot reach a module-level `lockstep`, and the alternative — resolving the facade
    #: out of the container — would let a workflow reach every binding when it needs one number.
    max_attempts: int = 3
    #: How many change requests one workflow may have open at once, snapshotted like
    #: `max_attempts`: the proposing workflow enforces it where a proposal is opened
    #: (`GATE-IMPROVE-8`), and a preflight command a pipeline can forget is not enforcement.
    max_open_proposals: int = 1
    #: Which prompt bodies a recurring finding may be attributed to. Empty means none is, and
    #: a proposal is refused rather than guessed from the shape of a finding id.
    improvable: tuple[Any, ...] = ()
    #: The lifecycle's guard, so a workflow can ask whether a path is writable BY GRANT before
    #: spending on a change to it. None on a hand-built context; the workflow that needs one
    #: refuses when it is absent, because absence of a guard is not permission.
    guard: Any = None
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
        for status in VERDICT_PRECEDENCE:
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
            # What the run's `Spend` held before the adapter ran. An AI adapter charges that same
            # `Spend` turn by turn through the invoker it was handed (`routed_invoker` passes
            # `ctx.spend`, and GATE-COST-6 depends on that sharing), and its outcome reports the
            # same money again. Charging the outcome in full counted every model call twice: the
            # ledger carried `cost_usd` at exactly 2x `outcome_cost_usd` on every fix, implement
            # and judge record, and every `CostBudget` ceiling tripped at half its stated value
            # (GATE-COST-7). Only what the outcome reports BEYOND what the `Spend` moved is charged
            # here: the whole cost of a deterministic adapter, and the wall clock of an AI one.
            already = self.spend.charged
            result: Outcome[Any] = await action.invoke(self, call.input)
            # Charged here, innermost, rather than after the chain unwinds — otherwise a
            # middleware reconciling actual spend against its ceiling looks at the accumulator
            # before this call was ever added to it, and every overrun reads as within budget.
            if result.cost.wall_seconds == 0.0:
                result = result.with_cost(replace(result.cost, wall_seconds=time.monotonic() - started))
            self.spend.charge(result.cost.beyond(self.spend.charged.beyond(already)))
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

    # -- fan-out over machine branches ------------------------------------------------

    def human(self, boundary: HumanBoundary, *, expires_seconds: float | None = None) -> HumanBranch:
        """Declare a fan-out branch a person completes (`ctx.human(HumanBoundary.pr_review(41))`)."""
        return HumanBranch(boundary=boundary, expires_seconds=expires_seconds)

    def _shared_store(self) -> Any | None:
        """The bound store if a person can park on it, else None. LOCAL is the one scope a park
        must refuse: a claim on a ref one machine can see is a claim nobody else can answer."""
        store = self.ledger
        if store is None or getattr(store, "scope", "local") != "shared":
            return None
        return store

    async def park(
        self,
        boundary: HumanBoundary,
        *,
        resume: str,
        payload: dict[str, Any] | None = None,
        expires_seconds: float | None = None,
    ) -> Outcome[Parked]:
        """End this run at a human boundary; a continuation starts when the person acts (§13.1).

        Writes the barrier record -- the boundary, the continuation id, the head this run stood
        on, the payload -- into the shared store under `barrier/<run_id>` as a create-if-absent,
        and returns `PARKED`. The process exits on that status, the CLI prints the resume command
        and places the host marker, and `resume` applies the person's event through the tick.
        On a LOCAL store, or none, `BLOCKED`/`park.local_store` naming the store (`GATE-OUT-6`):
        a park nobody on another machine could resume is a run that would wait forever.
        """
        store = self._shared_store()
        if store is None:
            return self._cannot_park(boundary)
        record = barrier_record(
            self.run_id,
            resume=resume,
            head=self.repo.head,
            payload=payload,
            human={"": HumanBranch(boundary=boundary, expires_seconds=expires_seconds)},
        )
        if not await store.compare_and_set(barrier_key(self.run_id), None, dumps(record)):
            return Outcome.blocked_by(
                "park.already_parked",
                findings=(
                    Finding(
                        id="park.already_parked",
                        message=f"run {self.run_id} already holds a barrier record; a run parks once",
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )
        parked = Parked(
            run_id=self.run_id, resume=resume, boundaries=(("", boundary),), key=barrier_key(self.run_id)
        )
        outcome: Outcome[Parked] = Outcome(
            status=Status.PARKED, reason=f"human.{boundary.kind}", value=parked
        )
        self.steps.append(StepOutcome(step="park", verb="", outcome=outcome))
        return outcome

    def _cannot_park(self, boundary: HumanBoundary) -> Outcome[Any]:
        store = self.ledger
        name = type(store).__name__ if store is not None else "no LedgerStore"
        scope = getattr(store, "scope", "none") if store is not None else "none"
        return Outcome.blocked_by(
            "park.local_store",
            findings=(
                Finding(
                    id="park.local_store",
                    message=(
                        f"cannot park on {boundary.describe()}: the run's ledger is {name} at "
                        f"scope {scope!r}, which only this machine can see. Bind a SHARED store "
                        f"(GitLedger(shared=True)) as LedgerStore so a person's event on another "
                        f"machine can resume this run."
                    ),
                    severity=Severity.ERROR,
                    blocking=True,
                ),
            ),
        )

    async def fan_out(
        self,
        *,
        resume: str | None = None,
        max_parallel: int | None = None,
        branches: Mapping[str, ActionCall | HumanBranch] | None = None,
        **named: ActionCall | HumanBranch,
    ) -> JoinResult:
        """Run declared branches concurrently and return when every one is terminal.

        Machine branches only (design §4.7; `GATE-OUT-6` records that nothing here parks): a
        branch is an `ActionCall` from `ctx.call`, each runs as its own task over a shallow copy
        of this context -- its own `scope_path` and its own step list, the SAME `spend`,
        `recording`, `container`, `state` and middleware -- so four branches share one budget,
        one tape and one kill switch rather than multiplying any of them. The budget is joint by
        construction (`GATE-COST-6`): `Spend.reserve` checks and reserves in one synchronous step,
        and a branch that would cross the ceiling is refused before its call, the same primitive a
        nested session a model delegates to will use (#332). Bounded by `max_parallel` through a
        semaphore; unbounded means every branch at once.

        The kill switch reaches in-flight work (`GATE-ASYNC-3b`): a watcher polls
        `killswitch_engaged()` and cancels what has not finished, and a cancelled branch joins as
        `BLOCKED`/`killswitch` rather than as an exception, because the switch is a control
        working and a join has to be able to say so per branch. A branch not yet started when the
        switch is thrown is refused by `run_call` the way any step is.

        Nothing is written to a ledger here (`GATE-OUT-7`): an all-machine join returns inline
        and the run continues; the barrier record is for a branch that parks, which is the
        deferred half. `resume=` names that continuation and is refused until then rather than
        accepted and ignored. No task outlives the call: the watcher is cancelled and awaited,
        every branch is awaited, and what returns is the whole result or a raised cancellation.
        """
        # Two spellings, one set: `**named` reads at a call site with a fixed set of branches,
        # and `branches=` is for a set computed at run time (every lens an adapter declares)
        # without fighting the keyword-only arguments beside it.
        branches = {**(branches or {}), **named}
        human = {name: b for name, b in branches.items() if isinstance(b, HumanBranch)}
        machine = {name: b for name, b in branches.items() if not isinstance(b, HumanBranch)}
        if human:
            # Pre-flight, before any branch starts (GATE-OUT-5): a human branch on a store one
            # machine can see is a barrier nobody on another machine could complete, and finding
            # that out after three machine branches have spent is finding it out too late. Raised
            # at the call site rather than returned, because it is a programming error about the
            # lifecycle's bindings, not an outcome of the run.
            if self._shared_store() is None:
                store = self.ledger
                raise LocalStoreCannotPark(
                    f"fan_out declares human branch(es) {sorted(human)} but the run's ledger is "
                    f"{type(store).__name__ if store is not None else 'no LedgerStore'} at scope "
                    f"{getattr(store, 'scope', 'none') if store is not None else 'none'!r}; a "
                    f"barrier needs a SHARED store (GitLedger(shared=True)) bound as LedgerStore. "
                    f"No branch was started."
                )
            if resume is None:
                raise ValueError(
                    f"fan_out with human branch(es) {sorted(human)} needs resume=<continuation id>: "
                    f"the run ends PARKED and that is what the person's event starts"
                )
        elif resume is not None:
            raise ValueError(
                f"resume={resume!r} names a continuation for a fan-out that parks, and every branch "
                f"here is a machine branch: the join is returned to this call"
            )
        if not branches:
            return JoinResult()
        joined = await self._run_machine_branches(machine, max_parallel)
        if not human:
            return JoinResult(branches=joined)
        return await self._park_join(joined, human, resume or "")

    async def _park_join(
        self,
        joined: tuple[tuple[str, Outcome[Any]], ...],
        human: dict[str, HumanBranch],
        resume: str,
    ) -> JoinResult:
        """Write the barrier record with every machine branch terminal and every human branch
        parked, and return the join with the human branches `PARKED` so the run ends there."""
        store = self._shared_store()
        assert store is not None  # the pre-flight above refused otherwise  # noqa: S101
        record = barrier_record(
            self.run_id,
            resume=resume,
            head=self.repo.head,
            machine={
                name: {"status": o.status.value, "reason": o.reason, "decided": o.decided}
                for name, o in joined
            },
            human=human,
        )
        if not await store.compare_and_set(barrier_key(self.run_id), None, dumps(record)):
            refused: Outcome[Any] = Outcome.blocked_by("park.already_parked")
            return JoinResult(branches=(*joined, *((name, refused) for name in human)))
        parked = Parked(
            run_id=self.run_id,
            resume=resume,
            boundaries=tuple((name, b.boundary) for name, b in human.items()),
            key=barrier_key(self.run_id),
        )
        waiting = tuple(
            (name, Outcome(status=Status.PARKED, reason=f"human.{b.boundary.kind}", value=parked))
            for name, b in human.items()
        )
        for name, outcome in waiting:
            self.steps.append(StepOutcome(step=name, verb="", outcome=outcome))
        return JoinResult(branches=(*joined, *waiting))

    async def _run_machine_branches(
        self, branches: dict[str, ActionCall], max_parallel: int | None
    ) -> tuple[tuple[str, Outcome[Any]], ...]:
        if not branches:
            return ()
        gate = asyncio.Semaphore(max_parallel) if max_parallel and max_parallel > 0 else None

        async def branch(name: str, call: ActionCall) -> Outcome[Any]:
            scoped = replace(
                self,
                scope_path=f"{self.scope_path}/{name}" if self.scope_path else name,
                steps=[],
                _step_counts={},
            )
            try:
                if gate is None:
                    return await scoped.run_call(call)
                async with gate:
                    return await scoped.run_call(call)
            except asyncio.CancelledError:
                if killswitch_engaged():
                    return Outcome.blocked_by("killswitch")
                raise

        tasks = {name: asyncio.create_task(branch(name, call)) for name, call in branches.items()}

        async def watch() -> None:
            while True:
                if killswitch_engaged():
                    for task in tasks.values():
                        if not task.done():
                            task.cancel()
                    return
                await asyncio.sleep(_KILLSWITCH_POLL_SECONDS)

        watcher = asyncio.create_task(watch())
        try:
            results = await asyncio.gather(*tasks.values())
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
        joined = tuple(zip(tasks.keys(), results, strict=True))
        for name, outcome in joined:
            call = branches[name]
            self.steps.append(StepOutcome(step=name, verb=_verb_name(call), outcome=outcome))
        return joined

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


#: How often a fan-out's watcher asks whether the switch has been thrown. `GATE-ASYNC-3b` asks
#: for a terminal join within two seconds of the switch; fifty milliseconds is far inside that
#: and costs nothing a branch would notice.
_KILLSWITCH_POLL_SECONDS = 0.05


def killswitch_engaged() -> bool:
    return bool(os.environ.get(DISABLE_ENV))


__all__ = [
    "DISABLE_ENV",
    "HumanBoundary",
    "HumanBranch",
    "JoinResult",
    "LocalStoreCannotPark",
    "Parked",
    "RepoInfo",
    "RunContext",
    "StepId",
    "StepOutcome",
    "Verb",
    "current_context",
    "killswitch_engaged",
]
