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
from pathlib import Path
from typing import Any, ClassVar

from ...ai.builtins import CommandRunner, Workspace, read_write_execute
from ...ai.context import ContextCurator
from ...ai.invoker import InvocationBlocked, InvocationFailed, InvokePolicy
from ...ai.prompt import Composition, PromptLayers, compositions
from ...ai.structured import SchemaError, parse
from ...core.changes import ChangeGuard
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.verbs import Capability, Verb
from ...privileged.egress import EgressRefused

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
        adapter that walked past `ApprovalGate`, past `UndeclaredBudget` and past `Retry`'s
        re-invocation refusal, all three of which read this frozenset off the bound object. It
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
                f"{sorted(c.value for c in missing)}. ApprovalGate, the budget refusal and Retry "
                f"all read that set off the bound object, so an undeclared strategy is an ungated "
                f"one. Either subclass a per-verb base (ImplementStrategy, FixStrategy), which "
                f"declares it for you, or write:\n\n"
                f"    capabilities: ClassVar[frozenset[Capability]] = AGENCY\n"
            )

    #: Subclass hooks: the session dataclass, the shipped prompt map, the default layer stack.
    _session_cls: ClassVar[Any]
    _shipped_prompts: ClassVar[Mapping[str, Any]]
    _layers_factory: ClassVar[Any]

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
    ) -> None:
        self.invoker_factory = invoker_factory
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

    def _session(self, ctx: Any) -> Any:
        """The per-run bundle. Built fresh each invoke: the workspace accumulates staged writes,
        and the invoker's credential is resolved per call rather than at bind time."""
        from ...ai.bootstrap import routed_invoker

        root = self.repo_root or str(getattr(getattr(ctx, "repo", None), "root", "") or ".")
        workspace = Workspace(root=Path(root), guard=self.guard, workflow_id=self.workflow_id)
        tools, runner = read_write_execute(workspace, commands=self.commands)
        factory = self.invoker_factory or routed_invoker(type(self).verb)
        return type(self)._session_cls(
            invoker=factory(ctx),
            workspace=workspace,
            tools=tools,
            run_tool=runner,
            policy=self.policy,
            layers=self.layers if self.layers is not None else type(self)._layers_factory(),
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


async def run_phase(session: Any, system: str, messages: Any, package: Any, *, prefix: str) -> Any:
    """One model turn-loop, with its three failure modes mapped to a `PhaseError`.

    A refused control raises BLOCKED; infrastructure failure or a truncated answer, ERRORED — the
    handling every strategy repeated inline. Returns the Invocation otherwise. `prefix` namespaces
    the truncation reason (`implement.truncated`, `fix.truncated`).
    """
    try:
        invocation = await session.invoker.run(
            system=system,
            messages=messages,
            context=package,
            tools=session.tools,
            run_tool=session.run_tool,
            policy=session.policy,
        )
    except (InvocationBlocked, EgressRefused) as e:
        raise PhaseError(
            Outcome(
                status=Status.BLOCKED,
                reason=e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )
        ) from e
    except InvocationFailed as e:
        raise PhaseError(
            Outcome(
                status=Status.ERRORED,
                reason=e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )
        ) from e
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
) -> list[Finding]:
    """The findings that travel with a change: the staged paths, the gaps it named, a note if the
    cover note was not JSON, and anything the injection scanner saw. `prefix` namespaces the ids
    (`implement.staged`, `fix.staged`)."""
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
    return findings
