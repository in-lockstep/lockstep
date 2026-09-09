"""Shared machinery for the model-backed strategies — oneshot, tdd, fix.

A strategy IS the bound adapter: `lockstep.bind(Implement, TDD(...))`. The `AiStrategy` base here
holds the plumbing they all share — the invoker seam, the workspace and tool assembly, the policy
defaults — and each subclass holds its *idea*: one session, or red→green, or reproduce-then-fix.
Alongside it live the helpers every strategy body repeats: run a model turn-loop and turn its
failure modes into an Outcome, parse the JSON cover note leniently, and render the
staged-and-injection findings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

from ...ai.builtins import CodeSearch, CommandRunner, Workspace, read_write_execute
from ...ai.context import ContextCurator
from ...ai.invoker import InvocationBlocked, InvocationFailed, InvokePolicy
from ...ai.prompt import Composition, PromptLayers, compositions
from ...ai.structured import SchemaError, parse
from ...core.changes import ChangeGuard
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.types import ChangeSet, Validate, ValidationFinding, ValidationReport
from ...core.verbs import Capability, Verb
from ...privileged.egress import EgressRefused
from .instructions import house_rules

#: Enough turns to look before writing, which is the whole premise of an implementing session. The
#: ceiling is not free and the cost is not linear: every turn re-sends the accumulated history, so
#: turn N pays for everything read in turns 1..N-1. Forty is chosen against that curve — and it is
#: the backstop, not the budget: `Spend.would_exceed` is what actually stops a run, checked before
#: each turn against the projected cost of making it.
DEFAULT_TURNS = 40

#: Big enough to write a whole file in one tool call, since `write_file` replaces a path's entire
#: contents and a truncated write is a corrupted file rather than a short answer. It is also the
#: number the per-turn spend projection bounds output by, so raising it raises the headroom every
#: turn must be able to afford.
DEFAULT_MAX_TOKENS = 8192


#: What `_session` hands every subclass, unconditionally: `read_write_execute` grants `write_file`,
#: `delete_file` and `run_script`, and the turn loop pays for a model call. Named because three
#: strategies hand-copied this exact frozenset, and a copy is a chance to trim one.
AGENCY = frozenset(
    {
        Capability.READS_REPO,
        Capability.SPENDS_BUDGET,
        Capability.WRITES_FILES,
        Capability.EXECUTES_CODE,
    }
)


class UndeclaredAgency(Exception):
    """A strategy holds write and execute tools its `capabilities` does not admit to.

    Beside `UngatedAgency` in spirit, and raised for the same reason: both are refusals about the
    shape of a lifecycle, made before a run rather than during one.
    """


class AiStrategy:
    """The constructor and per-run assembly shared by the bindable strategies.

    Subclasses declare `id` (the label their reports carry), `verb`, `capabilities` — the
    load-bearing declaration every gate reads off the bound object — plus their session type and
    prompt/layer defaults, and implement `invoke(ctx, request)` starting from `self._session(ctx)`.

    Prefer a per-verb base — `ImplementStrategy`, `FixStrategy` — which sets `verb`,
    `capabilities` and the three session hooks for you. Subclass this directly only for a verb the
    framework does not ship.

    No invoker by default: the model comes from `lockstep.models.route(<verb>, ...)`, resolved per
    run off the context. Passing `invoker_factory=` is the seam for a custom `ProviderRegistry`,
    gateway, or cassette provider.
    """

    id: ClassVar[str] = ""
    verb: ClassVar[Verb]
    #: The request type this strategy serves, and so the key it binds under. Set by the
    #: per-verb bases; `Lockstep.use` refuses a strategy that does not name one, because
    #: guessing a container key from a verb is how a bind lands somewhere nobody reads.
    request: ClassVar[Any] = None
    capabilities: ClassVar[frozenset[Capability]] = frozenset()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Refuse a subclass that declares less agency than `_session` gives it.

        `capabilities` defaulted to the empty set while every subclass was handed write, delete and
        execute tools plus a paid model call — so a strategy that simply omitted the line got an
        adapter that walked past `ApprovalGate`, past `UndeclaredBudget` and past the mandatory-
        egress trigger, all three of which read this frozenset off the bound object. It
        failed OPEN, silently, on the one population most likely to hit it: somebody writing their
        first strategy by following `docs/extending.md`.

        A refusal, not an inference. `Capability` is declared and never inferred, and inference
        could not do this job anyway — a `ToolSet` exists only inside a run, so the check would
        move from startup to first call, and `SPENDS_BUDGET` belongs to no tool at all. What this
        asserts is narrower and checkable at import: you may not declare LESS than you hold.

        Declaring more stays legal and is sometimes correct — `read_write_execute` declares
        `EXECUTES_CODE` even with no runner bound, because a set that could execute elsewhere must
        not read as harmless here.
        """
        super().__init_subclass__(**kwargs)
        missing = AGENCY - cls.capabilities
        if missing:
            raise UndeclaredAgency(
                f"{cls.__name__} subclasses AiStrategy, so it is handed write_file, delete_file "
                f"and run_script and it pays for a model call — but `capabilities` omits "
                f"{sorted(c.value for c in missing)}. ApprovalGate, the budget refusal and the "
                f"egress trigger all read that set off the bound object, so an undeclared strategy "
                f"is an ungated "
                f"one. Either subclass a per-verb base (ImplementStrategy, FixStrategy), which "
                f"declares it for you, or write:\n\n"
                f"    capabilities: ClassVar[frozenset[Capability]] = AGENCY\n"
            )

    #: Subclass hooks: the session dataclass, the shipped prompt map, the default layer stack.
    _session_cls: ClassVar[Any]
    _shipped_prompts: ClassVar[Mapping[str, Any]]
    _layers_factory: ClassVar[Any]

    #: Whether this verb reads the repository's `AGENTS.md`/`CLAUDE.md` into its system prompt.
    #:
    #: OFF here and set on the implement and fix bases only, because the answer is about the
    #: checkout rather than the verb's usefulness. Those two run from `issue_comment` and `issues`
    #: events, which GitHub executes on the default branch — the reviewed file. Review runs from
    #: `pull_request`, where the checkout is the merge ref, so its `CLAUDE.md` is whatever the
    #: contributor put there. Opting in per verb keeps that distinction greppable; a global switch
    #: would make it one edit away from feeding attacker-authored text into a system prompt.
    reads_house_rules: ClassVar[bool] = False

    def __init__(
        self,
        invoker_factory: Callable[[Any], Any] | None = None,
        *,
        repo_root: str = "",
        policy: InvokePolicy | None = None,
        curator: ContextCurator | None = None,
        commands: CommandRunner | None = None,
        guard: ChangeGuard | None = None,
        workflow_id: str = "",
        prompts: Mapping[str, Any] | None = None,
        layers: PromptLayers | None = None,
        delegation: bool = False,
        code_search: bool | CodeSearch = False,
    ) -> None:
        self.invoker_factory = invoker_factory
        #: Whether the session holds `search_code` (#375): `True` binds Graft in the framework's
        #: cache, provisioned by `in-lockstep provision` and built before the first model call; a
        #: `CodeSearch` of the caller's is used as given. Off unless the module says so, because a
        #: tool's name is part of every recorded request's key -- see `read_only`.
        self.code_search = code_search
        self._graft: Any = None
        #: Whether the session may hand a task to a nested session over a narrower tool set
        #: (`delegate`, #332). Off unless the module says so: it is the one builtin that turns a
        #: turn cap into a bound on this loop rather than on every model call the run makes.
        self.delegation = delegation
        #: Empty defaults to the run's own repository (`ctx.repo.root`) at session time.
        self.repo_root = repo_root
        self.policy = policy or InvokePolicy(max_turns=DEFAULT_TURNS, max_tokens=DEFAULT_MAX_TOKENS)
        #: Whether the caller named one. `Lockstep.use` completes an unset policy from the
        #: module's resolved floor and must not overwrite one somebody wrote down.
        self._policy_declared = policy is not None
        self.curator = curator or ContextCurator()
        # No runner by default, so `run_script` refuses until a caller supplies one. The tool is
        # still declared and the capability is still visible to policy — see `read_write_execute`.
        self.commands = commands
        self.guard = guard or ChangeGuard()
        # Keyed on the workflow id, never the strategy id: a Tier-2 grant reachable through
        # strategy selection is a grant a ticket label can steer.
        self.workflow_id = workflow_id
        # Copied rather than aliased, so a later mutation of the shipped map cannot reach a bound
        # adapter, and an adapter's prompt map cannot leak back into the shipped one.
        self.prompts: Mapping[str, Any] = (
            dict(prompts) if prompts is not None else dict(type(self)._shipped_prompts)
        )
        # The layer stack around every prompt this adapter runs — a repository's own guardrails go
        # here, usually as `<verb>_layers().plus(guardrails=...)` so the shipped baseline stays
        # underneath.
        self.layers = layers

    def complete_for(self, lockstep: Any) -> Any:
        """Finish constructing this strategy from the module binding it, and return the key it
        wants. Called by `Lockstep.use`, which cannot reach into `adapters` itself — the layering
        contract forbids it — so the knowledge lives here and the facade calls a method.

        Two of the values filled in here are why this exists rather than a docs example. Both were
        optional keyword arguments a hand-written bind could omit, and omitting either was silent:

          * `InvokePolicy.under(policy.resolve(), ...)`. A strategy constructed without it ignores
            the contributed policy floor — `deny_tools` and `scan_input` are simply dropped, one
            bind at a time, with nothing to see in `ls`.
          * The `WorktreeRunner` wrap. An unwrapped `Sandbox` bind-mounts the live tree
            read-write; the wrap is what makes the container mount a throwaway copy instead.
            `adapters/worktree.py` calls the unwrapped case "goal 8's one confirmed
            non-bypassability hole".

        Anything the caller named is left alone. This completes; it does not override.
        """
        from ..worktree import WorktreeRunner

        workshop = getattr(lockstep, "workshop", None)
        if workshop is not None:
            if not self._policy_declared:
                self.policy = InvokePolicy.under(
                    lockstep.policy.resolve(),
                    max_turns=workshop.max_turns,
                    max_idle_turns=workshop.max_idle_turns,
                    max_tokens=workshop.max_tokens,
                    deadline_seconds=workshop.deadline_seconds,
                )
            if self.commands is None:
                self.commands = workshop.commands
        if self.commands is not None and not isinstance(self.commands, WorktreeRunner):
            self.commands = WorktreeRunner(self.commands, lockstep.repo.root)
        if not self.repo_root:
            self.repo_root = lockstep.repo.root
        return type(self).request

    def compositions(self) -> dict[str, Composition]:
        """This strategy's prompts, for `show-prompt` and `ls`. See `AiReview.compositions`.

        On the base rather than on `Oneshot`, `TDD` and `DiagnoseThenFix` separately: the three
        session hooks a per-verb base sets are exactly what this needs, so a strategy somebody
        writes tomorrow is inspectable without being told to implement anything.
        """
        return compositions(
            self.prompts,
            self.layers if self.layers is not None else type(self)._layers_factory(),
            verb=str(type(self).verb),
            source=type(self).__name__,
        )

    @property
    def provisions(self) -> tuple[Any, ...]:
        """What `in-lockstep provision` installs for this adapter: Graft, when `code_search=True`."""
        return (self._graft_backend(),) if self.code_search is True else ()

    def _graft_backend(self) -> Any:
        """One `Graft` per adapter, so a run's `_session` and `provision` name the same cache."""
        if self._graft is None:
            from ..graft import Graft

            self._graft = Graft()
        return self._graft

    def _code_search(self, root: str) -> CodeSearch | None:
        """The backend for this run, or none. A `GraftSearch` is per run because it keeps what
        the run searched for the record."""
        if self.code_search is True:
            from ..graft import GraftSearch

            return GraftSearch(graft=self._graft_backend(), repo_root=root)
        return self.code_search or None

    def _session(self, ctx: Any) -> Any:
        """The per-run bundle. Built fresh each invoke: the workspace accumulates staged writes,
        and the invoker's credential is resolved per call rather than at bind time."""
        root = self.repo_root or str(getattr(getattr(ctx, "repo", None), "root", "") or ".")
        workspace = Workspace(root=Path(root), guard=self.guard, workflow_id=self.workflow_id)
        tools, runner = read_write_execute(
            workspace,
            commands=self.commands,
            # THE one place this is wired, and every code-writing verb goes through it. Implement
            # and fix both reach `_session`, so neither has to opt in and neither can forget to —
            # the same argument `AGENCY` makes about a frozenset that was hand-copied three times.
            tests=_test_runner(ctx, root, workspace),
            max_test_runs=self.policy.max_test_runs,
            delegation=self.delegation,
            code_search=self._code_search(root),
            validates=_validate_runner(ctx, root, workspace),
        )
        layers: PromptLayers = self.layers if self.layers is not None else type(self)._layers_factory()
        if type(self).reads_house_rules:
            # Appended, so the repository's conventions land after the framework's guardrails
            # and the strategy body. `plus` is the only spelling that guarantees that ordering.
            layers = layers.plus(contexts=house_rules(root))
        return type(self)._session_cls(
            invoker=resolve_invoker(self.invoker_factory, type(self).verb, ctx),
            workspace=workspace,
            tools=tools,
            run_tool=runner,
            policy=self.policy,
            layers=layers,
            prompts=self.prompts,
            curator=self.curator,
            guard=self.guard,
            repo_root=root,
        )


class PhaseError(Exception):
    """A model phase could not proceed. Carries the Outcome the strategy should return, so a caller
    wraps however many phases it runs in one `except PhaseError` rather than repeating the mapping."""

    def __init__(self, outcome: Outcome[Any]) -> None:
        super().__init__(outcome.reason or "phase failed")
        self.outcome = outcome


async def run_phase(
    session: Any,
    system: str,
    messages: Any,
    package: Any,
    *,
    prefix: str,
    schema: dict[str, Any] | None = None,
    stalled_report: Callable[[ChangeSet], Any] | None = None,
) -> Any:
    """One model turn-loop, with its four failure modes mapped to a `PhaseError`.

    A refused control raises BLOCKED; infrastructure failure or a truncated answer, ERRORED — the
    handling every strategy repeated inline. Returns the Invocation otherwise. `prefix` namespaces
    the truncation reason (`implement.truncated`, `fix.truncated`). `schema` is the shape the
    phase's cover note must take, handed to the invoker so a model registered as not answering
    with one is refused before the loop starts rather than after every turn of it is paid for.

    The fourth: a loop that stopped because `max_idle_turns` turns in a row moved nothing is
    BLOCKED as `<prefix>.no_progress` -- a ceiling stopping a run is the control working -- and
    the outcome carries what the session had staged, through `stalled_report`, which is the
    verb's own report over a change set: a run stopped for idling still hands a person its
    attempt, as one stopped at the turn cap does (#337).
    """
    # The index a session searches is built here, before the first model call, rather than on the
    # first query (GATE-SEARCH-1): a refusal is kept for the record and every query returns it,
    # so a phase never waits on a cold cache mid-loop and a run goes on without the tool.
    prepare = getattr(getattr(getattr(session, "run_tool", None), "code_search", None), "prepare", None)
    if callable(prepare):
        await prepare()
    try:
        invocation = await session.invoker.run(
            system=system,
            messages=messages,
            context=package,
            tools=session.tools,
            run_tool=session.run_tool,
            policy=session.policy,
            schema=schema,
        )
    except (InvocationBlocked, EgressRefused, InvocationFailed) as e:
        # `failure_outcome` rather than two inline constructions, so this and the backport resolver
        # cannot come to disagree about whether a refused control is BLOCKED or FAILED.
        raise PhaseError(failure_outcome(e)) from e

    if invocation.stalled:
        staged = session.workspace.changeset() if hasattr(session, "workspace") else ChangeSet()
        raise PhaseError(
            Outcome(
                status=Status.BLOCKED,
                reason=f"{prefix}.no_progress",
                value=stalled_report(staged) if stalled_report is not None else None,
                cost=invocation.cost,
                findings=(
                    Finding(
                        id=f"{prefix}.no_progress",
                        message=(
                            f"stopped after {invocation.idle_turns} turn(s) in a row that staged "
                            f"nothing and tested nothing new (the ceiling is "
                            f"{session.policy.max_idle_turns}, and twice that before anything is "
                            f"staged); the last productive one was "
                            f"{invocation.last_progress or 'none: nothing was ever staged'}. "
                            f"{len(staged.changes)} staged change(s) are returned unproposed."
                        ),
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )
        )
    if invocation.truncated:
        raise PhaseError(
            Outcome(
                status=Status.ERRORED,
                reason=f"{prefix}.truncated",
                cost=invocation.cost,
                findings=(
                    Finding(
                        id=f"{prefix}.truncated",
                        message=(
                            f"the model stopped at the {session.policy.max_tokens}-token output cap "
                            f"mid-answer. A write cut off there is a truncated file, so nothing staged "
                            f"in this session is returned. Raise `InvokePolicy.max_tokens` and re-run."
                        ),
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )
        )
    return invocation


#: How many model turns a run may spend repairing what the repository's own validator found. One,
#: deliberately: the findings are named with their rule and location, so a model that cannot act on
#: them in a turn is not going to be helped by a second, and every round is paid for out of the
#: ceiling the run declared. Unrepaired findings are not lost -- they travel with the change and
#: keep it out of a reviewer's queue.
VALIDATE_REPAIR_ROUNDS = 1


def with_directive(base: list[Any], directive: str) -> list[Any]:
    """The rendered messages with a phase directive folded into the last (user) message.

    `replace` clones the message with new content, so this appends the step's instruction without
    importing the `Message` type from the `llm` layer -- which `adapters` may not reach -- and
    without a second consecutive user message some providers dislike.
    """
    last = base[-1]
    return [*base[:-1], replace(last, content=f"{last.content}\n\n{directive}")]


@dataclass(frozen=True)
class Validation:
    """What the repository's own validator said about what a run staged, and what was done about it.

    `report is None` is the honest absent: no Validate verb is bound, or the validator could not
    report. Absent is not clean -- `clean` says so -- because a change nothing checked and a change
    that passed its checks are different facts, and only one of them has earned a reviewer's queue.
    """

    #: None when nothing checked this change. Never an empty report standing in for one.
    report: ValidationReport | None = None
    #: Paths the deterministic pass rewrote, before any model call was made.
    fixed: tuple[str, ...] = ()
    #: Why the change does not build, or why nothing built it. Empty when it built, and when no
    #: Build verb is bound -- which is most repositories, and not a fact worth a finding.
    build: str = ""
    #: The repair turns actually made, so a strategy can add their cost and turns to its own.
    invocations: tuple[Any, ...] = ()
    #: Why a repair did not happen, when findings remained and no turn was made.
    unrepaired: str = ""

    @property
    def checked(self) -> bool:
        return self.report is not None

    @property
    def clean(self) -> bool:
        return self.report is not None and self.report.clean

    def findings(self) -> tuple[Finding, ...]:
        """What travels on the outcome. Notes, not blocking: the change is still the run's product,
        and whether an unclean change may ask for review is the propose half's decision to make."""
        out: list[Finding] = []
        if self.build:
            out.append(Finding(id="validate.build", message=self.build, severity=Severity.WARNING))
        if self.fixed:
            out.append(
                Finding(
                    id="validate.fixed",
                    message=(
                        f"the repository's own validator fixed {len(self.fixed)} file(s) with no "
                        f"model call: {', '.join(self.fixed)}"
                    ),
                    severity=Severity.NOTE,
                )
            )
        if self.report is None:
            return tuple(out)
        out += [
            Finding(
                id=f"validate.{f.rule.lower()}" if f.rule else "validate.finding",
                message=f"{f.message} (unrepaired)",
                severity=Severity.WARNING,
                path=f.path,
                line=f.line,
            )
            for f in self.report.findings[:25]
        ]
        if self.unrepaired:
            out.append(Finding(id="validate.unrepaired", message=self.unrepaired, severity=Severity.WARNING))
        return tuple(out)


async def validated(
    ctx: Any,
    session: Any,
    changeset: ChangeSet,
    *,
    system: str,
    messages: list[Any],
    package: Any,
    prefix: str,
    rounds: int = VALIDATE_REPAIR_ROUNDS,
) -> Validation:
    """Build what this session staged, check it, and repair what either one says.

    The half `Test` already had. A strategy does not ask the model to run its tests -- it stages
    them into a worktree and runs them itself, between phases, and hands the result back. Nothing
    did that for the repository's own build and lint, so a run's checks were whatever the model
    chose to run with `run_validate`, and CI was the first thing to see the answer twice (run
    34294139197, and the four ruff errors on the pull request #389 opened).

    **The build comes first**, where the repository declares one. A change that does not compile is
    the more important failure and the one that makes the rest secondary: findings about a tree
    that does not build are answers to a question nobody has got to yet.

    **Then the check, cheapest tier first.** Where the bound adapter says it can fix (`fixes`;
    `RuffValidate` does, and a `CommandValidate` bound with the repository's own `make fmt` does),
    the validator runs over the materialised tree with its fixer on and the result is restaged.
    Import sorting is arithmetic wearing a prompt, and a turn spent on it is a turn bought at model
    prices.

    **One repair budget across both gates, spent on the first thing that failed.** Two budgets
    would be two turns, and O13 asks for a bound declared before the spend rather than one per
    gate. The findings go back naming each one's rule, path and line -- `GATE-VERDICT-2`'s rule
    about a red suite: a verdict that says a check failed and not which one sends the session back
    to run it again to learn what the run already knew.

    **This mutates the session's workspace and returns no change set.** Every tier writes there --
    the fixer through `restage`, the repair turn through the tool boundary like any other write --
    so the caller rebuilds its own change set from the workspace afterwards, exactly as it built it
    before. A second return value would be a second place the staged set is assembled, and the two
    would eventually disagree about which one travels.
    """
    paths = tuple(c.path for c in changeset.changes if not c.deleted)
    if not paths or getattr(ctx, "container", None) is None:
        return Validation()

    invocations: list[Any] = []
    fixed: tuple[str, ...] = ()
    report: ValidationReport | None = None
    build = ""
    unrepaired = ""
    for attempt in range(rounds + 1):
        build, note = await _built(ctx, session, changeset)
        problem = build
        if not build:
            if not fixed and _fixes(ctx):
                fixed = await _fix_pass(ctx, session, changeset, paths)
                if fixed:
                    changeset = replace(changeset, changes=tuple(session.workspace.changes))
            # `why`, not `note`: the build's note is still in flight below, and a second name
            # for it here would silently drop a sandbox refusal in favour of an empty string.
            report, said, why = await _checked(ctx, session, changeset, paths)
            problem = said
            unrepaired = why or unrepaired
        build = build or note
        if not problem or attempt == rounds:
            if problem and attempt == rounds and rounds:
                unrepaired = f"{rounds} repair turn(s) did not settle it"
            break
        try:
            invocation = await run_phase(
                session,
                system,
                with_directive(messages, problem),
                package,
                prefix=f"{prefix}.validate",
                # No schema: this turn's product is its writes, not a cover note, and requiring
                # structured output would refuse a model registered without it over a step that
                # never reads the reply.
            )
        except PhaseError as e:
            # A refused or exhausted repair is not this run's failure. The change was complete
            # before the repair was attempted, and returning the ceiling's refusal here would
            # throw away a finished change to report an optional turn that could not be made --
            # so what was found travels instead, and the propose half keeps the change a draft.
            reason = getattr(e.outcome, "reason", None) or e.outcome.status.value
            unrepaired = f"the repair turn was not made ({reason}); what was found stands"
            break
        invocations.append(invocation)
        changeset = replace(changeset, changes=tuple(session.workspace.changes))
    return Validation(
        report=report, fixed=fixed, build=build, invocations=tuple(invocations), unrepaired=unrepaired
    )


async def _built(ctx: Any, session: Any, changeset: ChangeSet) -> tuple[str, str]:
    """Build the staged tree. Returns (what the model can fix, what it cannot) -- at most one.

    A repository with no build target gets neither, and that is not a finding: most do not have
    one, and inventing a build command would be the guess O1 refuses.
    """
    from ...core.types import Build
    from ..worktree import materialize, staged_refusal

    container = ctx.container
    if not container.has(Build):
        return "", ""
    # The same question `Test` is asked, of the verb being run rather than of `Test` by name: a
    # build executes the repository's own build scripts over model-authored source, which is the
    # exposure GATE-SANDBOX-2 is about with a different file extension.
    if (why := staged_refusal(ctx, Build)) is not None:
        return "", f"the change was not built: {why}"
    async with materialize(session.repo_root, changeset) as tree:
        outcome = await ctx.do(Build(root=tree))
    if outcome.status is Status.SUCCEEDED:
        return "", ""
    said = "\n".join(f.message for f in outcome.findings) or (outcome.reason or "the build did not succeed")
    return (
        "This repository's own build ran over what you staged and failed:\n\n"
        f"{said}\n\nMake it build, and then stop. Fix the error rather than removing the code "
        "that raised it, and change only what the failure names.",
        "",
    )


def _fixes(ctx: Any) -> bool:
    """Whether the bound validator says `Validate(fix=True)` means something to it."""
    container = ctx.container
    return bool(container.has(Validate) and getattr(container.resolve(Validate), "fixes", False))


async def _fix_pass(ctx: Any, session: Any, changeset: ChangeSet, paths: tuple[str, ...]) -> tuple[str, ...]:
    from ..worktree import materialize

    async with materialize(session.repo_root, changeset) as tree:
        await ctx.do(Validate(root=tree, paths=_scoped(ctx, paths), fix=True))
        return _restaged(session.workspace, changeset, tree)


async def _checked(
    ctx: Any, session: Any, changeset: ChangeSet, paths: tuple[str, ...]
) -> tuple[ValidationReport | None, str, str]:
    """Run the repository's own checks over the staged tree.

    Returns the report, what to send a repair turn, and a note about why there is no report --
    at most one of the last two says anything.

    A read after the fixer rather than the fixer's own output: `--fix` reports what it could not
    fix, and reading that as the whole answer would make this depend on one tool's choice of what
    to print. A second pass costs a subprocess and says exactly what stands.
    """
    from ..worktree import materialize

    if not ctx.container.has(Validate):
        # O1's rule: nothing is invented where the repository declared nothing.
        return None, "", ""
    async with materialize(session.repo_root, changeset) as tree:
        outcome = await ctx.do(Validate(root=tree, paths=_scoped(ctx, paths)))
    report = outcome.value if isinstance(outcome.value, ValidationReport) else None
    if report is None:
        # Absent is not clean, and it is not this run's failure either: the change stands,
        # unchecked, and says so.
        return None, "", f"the validator did not report: {outcome.reason or outcome.status.value}"
    return report, "" if report.clean else _repair_directive(report), ""


def _scoped(ctx: Any, paths: tuple[str, ...]) -> tuple[str, ...]:
    """The staged paths, where the bound validator can take paths at all.

    A tool lints what it is given, and scoping to the change keeps a run off the repository's
    existing debt -- a run that spends turns on code it never touched widens its own diff to
    answer for findings nobody asked it about. A repository's own TARGET takes no paths (`make
    lint src/a.py` reads the path as a target), so there the whole tree is checked: that is what
    such a target is, a gate the repository keeps green, which is what makes a failure after a
    change attributable to the change.
    """
    return paths if getattr(ctx.container.resolve(Validate), "takes_paths", False) else ()


def _restaged(workspace: Any, changeset: ChangeSet, tree: str) -> tuple[str, ...]:
    """Fold a fixer's edits back into the session's staged writes. Returns the paths it changed.

    Only paths the session already staged, and only their contents: a fixer that created a file
    did not create one this session is proposing, and a fixer that deleted one is not how a
    deletion gets into a change set.
    """
    changed: list[str] = []
    for change in changeset.changes:
        if change.deleted:
            continue
        try:
            after = (Path(tree) / change.path).read_text()
        except OSError:
            continue
        if after != change.contents and workspace.restage(change.path, after):
            changed.append(change.path)
    return tuple(changed)


def _repair_directive(report: ValidationReport) -> str:
    """What the model is told about its own findings, and the two rules that make fixing them work.

    The prose is here rather than in a prompt body on purpose. A body is read before there is
    anything to fix; this is read at the moment of the act, and it is the only moment either rule
    means anything -- a model handed "fix these four findings" can satisfy that by deleting the
    code that produced them.
    """
    listed = "\n".join(_finding_line(f) for f in report.findings[:40])
    more = f"\n  …[{len(report.findings) - 40} more]" if len(report.findings) > 40 else ""
    return (
        f"This repository's own validator ran over what you staged and reported "
        f"{len(report.findings)} finding(s):\n{listed}{more}\n\n"
        "Fix them, and then stop. Two rules, because both failures look like success:\n"
        "- Fix the finding, not the code that caused it. Deleting the code, weakening what it "
        "checks, or silencing the rule makes the finding go away without doing the work.\n"
        "- Change only what these findings name. This is not an opportunity to revise the rest of "
        "the change."
    )


def _finding_line(finding: ValidationFinding) -> str:
    where = f"{finding.path}:{finding.line}" if finding.line is not None else finding.path
    return f"  {where}: {finding.rule} {finding.message}".rstrip()


def read_reply(content: str) -> tuple[str, tuple[str, ...], tuple[str, ...], bool]:
    """The cover note, leniently: (summary, notes, unfinished, malformed). A reply that is not the
    JSON the schema asked for is not thrown away — the change already came through the tool boundary
    — so its text becomes the summary and `malformed` says so."""
    try:
        value = parse(content).value
    except SchemaError:
        return content.strip()[:1000], (), (), True
    if not isinstance(value, dict):
        return content.strip()[:1000], (), (), True
    return (
        str(value.get("summary", "")).strip(),
        _strings(value.get("notes")),
        _strings(value.get("unfinished")),
        False,
    )


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(v) for v in value if isinstance(v, (str, int, float)))


def test_findings(outcome: Any) -> tuple[Finding, ...]:
    """A Test verb's own blocking findings, carried up so a red/green failure explains itself."""
    return tuple(
        Finding(id=f.id, message=f.message, severity=Severity.NOTE)
        for f in outcome.findings
        if getattr(f, "blocking", False)
    )


def reported(
    changeset: Any,
    *,
    unfinished: tuple[str, ...] = (),
    malformed: bool = False,
    invocations: tuple[Any, ...] = (),
    prefix: str,
    notes: tuple[Finding, ...] = (),
) -> list[Finding]:
    """The findings that travel with a change: the staged paths, the gaps it named, a note if the
    cover note was not JSON, anything the injection scanner saw, and `notes` -- what the session's
    tools want on the record, which today is what `search_code` searched (`search_notes`).
    `prefix` namespaces the ids (`implement.staged`, `fix.staged`)."""
    findings = [
        Finding(
            id=f"{prefix}.staged",
            message=f"{'deleted' if change.deleted else 'wrote'} {change.path}",
            severity=Severity.NOTE,
            path=change.path,
        )
        for change in changeset.changes
    ]
    findings += [
        Finding(id=f"{prefix}.unfinished", message=gap, severity=Severity.WARNING) for gap in unfinished
    ]
    if malformed:
        findings.append(
            Finding(
                id=f"{prefix}.unstructured",
                message=(
                    "the final message was not the JSON the schema asked for; its text was kept as "
                    "the summary. The staged change came through the tool boundary and is unaffected."
                ),
                severity=Severity.WARNING,
            )
        )
    findings += [
        Finding(
            id=f"injection.{f.name}",
            message=f"{f.severity}: {f.excerpt}",
            severity=Severity.ERROR if f.severity == "critical" else Severity.WARNING,
        )
        for inv in invocations
        for f in inv.findings
    ]
    findings += list(notes)
    return findings


def search_notes(session: Any) -> tuple[Finding, ...]:
    """What the session's code search wants on the record, or nothing when there was none."""
    backend = getattr(getattr(session, "run_tool", None), "code_search", None)
    notes = getattr(backend, "notes", None)
    return tuple(notes()) if callable(notes) else ()


def _test_runner(ctx: Any, root: str, workspace: Workspace) -> Any:
    """A `TestRunner` over HEAD plus whatever this session has staged so far.

    Lives here rather than in `ai/builtins.py` because materialising a change set is
    `adapters/worktree.py`, which `ai` may not import. That is not an inconvenience being worked
    around — it is why `TestRunner` is a Protocol and this is the composition root filling it.

    The staged set is read at CALL time, not at session build time. A model runs the suite after
    writing, so binding the changeset earlier would test the tree as it was before the edit it is
    asking about — which is exactly the confusion `run_script` already causes by running against
    HEAD, and the reason this tool exists at all.
    """
    from ...core.types import Test
    from ..worktree import materialize, staged_refusal

    async def run(paths: tuple[str, ...] = ()) -> str:
        container = getattr(ctx, "container", None)
        if container is None or not container.has(Test):
            return "refused: no Test verb is bound, so there is nothing to run."
        staged = workspace.changeset()
        if not staged.changes:
            return (
                "refused: nothing is staged yet, so this would test the code exactly as it already "
                "is. Write your change first, then run."
            )
        # Before the worktree exists. The model reads this as a tool result and can carry on
        # without the suite; the strategy's own final run refuses the same way, so a session that
        # ends here is not a session that quietly ran its test on the host (GATE-SANDBOX-2).
        if (why := staged_refusal(ctx)) is not None:
            return f"refused (sandbox.host_fallback): {why}"
        async with materialize(root, staged) as tree:
            outcome = await ctx.do(Test(root=tree, paths=paths))
        return _rendered(outcome)

    return run


def _validate_runner(ctx: Any, root: str, workspace: Workspace) -> Any:
    """A `ValidateRunner` over HEAD plus whatever this session has staged so far.

    The sibling of `_test_runner`, and it exists because the asymmetry between them cost a run: a
    session could test its staged writes and could not check them, so `run_script ruff check` --
    which runs in a worktree of HEAD by design -- answered about code the model had not touched.
    Run 34294139197 opened a pull request whose suite passed and whose lint failed on four errors
    a validator would have named in a second.

    No `staged_refusal` here, and the difference from `_test_runner` is the point: a suite
    EXECUTES what the model wrote, which is why GATE-SANDBOX-2 requires a container for it, and a
    validator READS it. Both shipped Validate adapters declare `READS_REPO` and nothing else. A
    binding whose validator does execute the tree would need the same rule, and the row says so.
    """
    from ...core.types import Validate
    from ..worktree import materialize

    async def run(paths: tuple[str, ...] = ()) -> str:
        container = getattr(ctx, "container", None)
        if container is None or not container.has(Validate):
            return "refused: no Validate verb is bound, so there is nothing to run."
        staged = workspace.changeset()
        if not staged.changes:
            return (
                "refused: nothing is staged yet, so this would check the code exactly as it "
                "already is. Write your change first, then run."
            )
        async with materialize(root, staged) as tree:
            outcome = await ctx.do(Validate(root=tree, paths=paths))
        return _validation(outcome)

    return run


def _validation(outcome: Any) -> str:
    """What the validator said, as the model needs to read it: the rule, where, and what.

    Findings first and a count, never a bare status. `run_tests` learned this the expensive way
    (GATE-VERDICT-2): a verdict that says a check failed and not which one sends the session back
    to run it again to learn what the run already knew.
    """
    report = outcome.value
    findings = tuple(getattr(report, "findings", ()) or ())
    if outcome.status is not Status.SUCCEEDED and not findings:
        reason = outcome.reason or outcome.status.value
        return f"the validator did not report: {reason}"
    if not findings:
        return "clean: the validator found nothing."
    listed = "\n".join(
        f"  {getattr(f, 'path', '')}:{getattr(f, 'line', '')}: {getattr(f, 'rule', '')} "
        f"{getattr(f, 'message', '')}".rstrip()
        for f in findings[:50]
    )
    more = f"\n  …[{len(findings) - 50} more]" if len(findings) > 50 else ""
    return f"{len(findings)} finding(s):\n{listed}{more}"


def _rendered(outcome: Any) -> str:
    """What the model is told about a suite run.

    A suite that COLLECTED NOTHING is reported as having decided nothing, never as passing. That
    distinction has cost this repository two runs already — a green suite that ran none of the new
    tests looks exactly like a green suite that ran them — and a tool that blurred it here would
    hand the model the same lie in a friendlier format.
    """
    report = getattr(outcome, "value", None)
    if report is None:
        return f"the suite did not produce a report ({getattr(outcome, 'reason', None) or 'no reason given'})"
    total = getattr(report, "total", 0)
    if not total:
        return (
            "NOTHING WAS COLLECTED, so nothing was decided. This is not a pass. Check that your "
            "test file and class names match what this repository collects before assuming the "
            "code is right."
        )
    failed = getattr(report, "failed", 0)
    passed, skipped = getattr(report, "passed", 0), getattr(report, "skipped", 0)
    head = f"{passed} passed, {failed} failed, {skipped} skipped of {total}"
    if not failed:
        return f"{head}\n\nEverything that ran, passed."
    # Failures first and passes never: the failing tests are the entire reason to have run this,
    # and a result truncated by `max_tool_result_chars` must not lose them to a list of passes.
    # Name and message both: what a test said is what the model has to act on, and a name alone
    # sends it back to rerun the suite to learn what it already paid to be told (GATE-VERDICT-2).
    names = [
        f"  {getattr(case, 'id', '?')}"
        + (f" - {message}" if (message := getattr(case, "message", "")) else "")
        for case in getattr(report, "cases", ())
        if getattr(case, "outcome", "") in ("failed", "error")
    ]
    listed = "\n".join(names[:50]) or "  (the report named no individual failures)"
    return f"{head}\n\nfailed:\n{listed}"


def blocked(reason: str, message: str) -> Outcome[Any]:
    """A control said no. BLOCKED, never FAILED — a run a ceiling or a gate stopped is the control
    working, and folding it into a failure rate makes every control look like a defect."""
    return Outcome.blocked_by(
        reason,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


def errored(reason: str, message: str, cost: Any = None) -> Outcome[Any]:
    """Infrastructure, not a verdict. ERRORED is the class transport retry (`RetryPolicy`) targets."""
    from ...core.outcome import Cost

    return Outcome(
        status=Status.ERRORED,
        reason=reason,
        cost=cost if cost is not None else Cost(),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


def failure_outcome(error: Exception, *, cost: Any = None) -> Outcome[Any]:
    """The three ways a model call fails, mapped in one place.

    `InvocationBlocked` and `EgressRefused` are a control refusing; `InvocationFailed` is the
    provider or the transport breaking. Every model-backed adapter needs exactly this mapping —
    three of them got it from `run_phase` and the fourth, `AiBackportResolver`, wrote it out again
    with its own local helpers. Two spellings of one decision is one of them drifting, and the
    drift here would be a control refusal recorded as a failure.
    """
    reason = getattr(error, "reason", "") or type(error).__name__
    if isinstance(error, (InvocationBlocked, EgressRefused)):
        return blocked(reason, str(error))
    return errored(reason, str(error), cost)


def not_a_verdict(outcome: Any, *, value: Any = None, cost: Any = None) -> Outcome[Any] | None:
    """The Test did not reach a verdict — pass its own status through. None when it did.

    Every red/green check in a strategy was `if status is not SUCCEEDED`, which is a two-way split
    over a six-member enum: a budget ceiling on the third Test of a TDD run produced
    `tdd.not_green` — *the implementation did not make the staged test pass* — about a suite that
    never ran (#256). Same for an ERRORED runner, and `_revert_verify` runs the Test most likely
    to meet a ceiling.

    FAILED passes through as None because FAILED is exactly the case those findings are about:
    the suite ran and disagreed. What this catches is the other four members, where the sentence
    would be an assertion about a test run that did not happen.

    The refusal's own findings and reason travel with it, so a `cost.budget_exceeded` arrives at
    the caller as itself rather than as a claim about the change.

    `Outcome[Any]` rather than a bare `Any`, and not a TypeVar: the callers return this where an
    `Outcome[ImplementReport]` or an `Outcome[FixReport]` is declared, and a bare `Any` return
    makes each of those a `no-any-return` under `--strict`. `Any` as the parameter is honest here
    -- what travels is whatever the caller passed, and a refusal carries no report of its own.
    """
    if outcome.status in (Status.SUCCEEDED, Status.FAILED):
        return None
    return Outcome(
        status=outcome.status,
        reason=outcome.reason,
        value=value,
        cost=cost if cost is not None else outcome.cost,
        findings=outcome.findings,
        decided=outcome.decided,
    )


def resolve_invoker(invoker_factory: Any, verb: Any, ctx: Any, *, aspect: str = "") -> Any:
    """The run's invoker: an injected factory, or the one routed from `lockstep.models.route`.

    `aspect` is the lens, for the one verb that has them: `review/<aspect>` is tried before
    `review`, and the strategies pass nothing because their prompts are not routed apart.

    It is the seam a repository substitutes for a gateway or a cassette provider, so every AI
    adapter goes through this one function rather than spelling it out — six had, and the
    docstring here already warned that two spellings have to agree about what "no factory given"
    means. They also have to agree about recording, which is why the count matters.

    **The recording is attached here, to what the factory returned.** `ai.bootstrap` wraps the
    provider it builds itself, which covers every adapter that lets the framework build the
    invoker — and an adapter constructed with its own `invoker_factory=` builds the provider
    inside a lambda nothing can reach, so for that repository a recording kept nothing (#243).
    Nothing can reach inside the lambda; the framework holds the object the lambda returned, and
    that object names its provider. So the wrap moves one step later and the hole closes without
    touching the escape hatch.

    That is the third answer to the choice the issue framed. Making the factory signature carry
    the tape moves the obligation onto whoever takes the escape hatch, and an adapter that ignores
    it is back where we started, silently; refusing a custom factory during a recording run
    removes an extension point O8 exists to protect. Wrapping the return value gives up neither:
    the factory keeps its signature and its freedom, and the recording is not something it can
    decline. `recorded` is idempotent, so a factory that wrapped already is left alone.
    """
    from ...ai.bootstrap import recorded, routed_invoker

    factory = invoker_factory or routed_invoker(verb, aspect=aspect)
    invoker = factory(ctx)
    log = getattr(ctx, "recording", None)
    if log is not None and getattr(invoker, "provider", None) is not None:
        invoker.provider = recorded(invoker.provider, log)
    return invoker
