"""Fix, backed by a model — reproduce the bug, then fix it, and prove both.

The shape Bug Fix has that Implement does not is that the *bug* has to be captured before the fix
is, and captured as something that fails. So this is two model steps with a real Test run between
them, the same spine `implement/tdd` uses, but the halves mean different things and are reported
apart:

1. **Reproduce.** Ask (with the reproducer-writer prompt) for a test that fails *because of the
   bug*. Materialise HEAD-plus-that-test and run the suite with `expect="fail"`. A reproducer that
   passes has captured nothing, and the run stops with `fix.not_reproduced` rather than going on to
   a fix nobody can verify.
2. **Fix.** Hand the model the failure the reproducer produced and ask (with the fix-writer prompt)
   for the change that makes it pass, without editing the test. Run the suite again over
   reproducer-plus-fix with `expect="pass"`; still red comes back `fix.not_fixed`.

The reproducer and the fix travel in the report as separate change sets, because a reviewer reads
them as two things: here is the bug, made executable; here is the line that mattered.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ...ai.builtins import CommandRunner, ToolRunnerImpl, Workspace, read_write_execute
from ...ai.context import ContextCurator, ContextItem, ContextNeed, ContextPackage
from ...ai.invoker import AiInvoker, InvocationBlocked, InvocationFailed, InvokePolicy
from ...ai.prompt import PromptLayers
from ...ai.strategy import Registration, StrategyRefused, StrategyRegistry, UnknownStrategy
from ...ai.structured import SchemaError, parse
from ...ai.structured import schema_instruction as _schema_instruction
from ...ai.tools import ToolSet
from ...core.changes import ChangeGuard
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.types import ChangeSet
from ...core.verbs import Capability, Verb
from ...privileged.egress import EgressRefused
from ...prompts.fix import FIX_PROMPTS, FIX_SCHEMA, FixParams, FixPrompt, fix_layers
from ..pytest_adapter import Test
from ..worktree import materialize

DEFAULT_TURNS = 40
DEFAULT_MAX_TOKENS = 8192


class Fix:
    """The verb interface. Workflows ask for this; a binding decides what serves it."""


@dataclass(frozen=True)
class FixSpec:
    """Which bug to fix, and how the strategy was chosen. Frozen: it is hashed for step identity."""

    #: The bug report, untrusted by construction — anyone who can file one writes into this prompt.
    ticket: Any
    strategy: str = ""
    untrusted_selection: bool = False
    token_budget: int = 60_000


@dataclass(frozen=True)
class FixReport:
    """A reproducer, a fix, and the cover note. The two change sets are kept apart on purpose."""

    reproducer: ChangeSet = field(default_factory=ChangeSet)
    fix: ChangeSet = field(default_factory=ChangeSet)
    summary: str = ""
    notes: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()
    strategy: str = ""
    turns: int = 0

    @property
    def changeset(self) -> ChangeSet:
        """The whole change, reproducer and fix together — what `apply`/`open_change` writes."""
        return _merge(self.reproducer, self.fix)

    @property
    def empty(self) -> bool:
        return not self.reproducer.changes and not self.fix.changes


@dataclass
class FixSession:
    """Everything a fix strategy needs, assembled once by the adapter. Mirrors `ImplementSession`;
    a fix strategy writes files, so it carries a workspace and the write/execute tools."""

    invoker: AiInvoker
    workspace: Workspace
    tools: ToolSet
    run_tool: ToolRunnerImpl
    policy: InvokePolicy
    layers: PromptLayers
    prompts: Mapping[str, type[FixPrompt]]
    curator: ContextCurator
    guard: ChangeGuard
    repo_root: str = "."

    def context(self, spec: FixSpec) -> ContextPackage:
        items: list[ContextItem] = list(spec.ticket.as_context())
        return self.curator.curate(items, ContextNeed(token_budget=spec.token_budget))


class AiFix:
    verb: ClassVar[Verb] = Verb.FIX
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
        repo_root: str = ".",
        policy: InvokePolicy | None = None,
        curator: ContextCurator | None = None,
        commands: CommandRunner | None = None,
        guard: ChangeGuard | None = None,
        workflow_id: str = "",
        prompts: Mapping[str, type[FixPrompt]] | None = None,
    ) -> None:
        self.invoker_factory = invoker_factory
        self.registry = registry
        self.repo_root = repo_root
        self.policy = policy or InvokePolicy(max_turns=DEFAULT_TURNS, max_tokens=DEFAULT_MAX_TOKENS)
        self.curator = curator or ContextCurator()
        self.commands = commands
        self.guard = guard or ChangeGuard()
        self.workflow_id = workflow_id
        self.prompts: Mapping[str, type[FixPrompt]] = (
            dict(prompts) if prompts is not None else dict(FIX_PROMPTS)
        )

    async def invoke(self, ctx: Any, inp: FixSpec) -> Outcome[FixReport]:
        try:
            registration = self.registry.select(
                Verb.FIX,
                explicit=inp.strategy or None,
                from_untrusted_input=inp.untrusted_selection,
            )
        except StrategyRefused as e:
            return _blocked("fix.strategy_refused", str(e))
        except UnknownStrategy as e:
            return _blocked("fix.unknown_strategy", str(e))

        strategy = registration.factory()
        if not hasattr(strategy, "execute"):
            return _blocked(
                "fix.strategy_not_executable",
                f"{registration.id!r} is registered as a catalogue entry, not an executable "
                f"strategy: its factory returned {type(strategy).__name__}, which has no `execute`. "
                f"Executable today: {', '.join(_executable(self.registry))}.",
            )
        outcome: Outcome[FixReport] = await strategy.execute(ctx, self._session(ctx, registration), inp)
        return outcome

    def _session(self, ctx: Any, registration: Registration) -> FixSession:
        workspace = Workspace(root=Path(self.repo_root), guard=self.guard, workflow_id=self.workflow_id)
        tools, runner = read_write_execute(workspace, commands=self.commands)
        return FixSession(
            invoker=self.invoker_factory(ctx),
            workspace=workspace,
            tools=tools,
            run_tool=runner,
            policy=self.policy,
            layers=fix_layers(),
            prompts=self.prompts,
            curator=self.curator,
            guard=self.guard,
            repo_root=self.repo_root,
        )


class DiagnoseThenFix:
    """Registered as `fix/diagnose-then-fix`. Reproduce the bug as a failing test, then fix it."""

    id: ClassVar[str] = "fix/diagnose-then-fix"
    verb: ClassVar[Verb] = Verb.FIX

    async def execute(self, ctx: Any, session: FixSession, inp: FixSpec) -> Outcome[FixReport]:
        container = getattr(ctx, "container", None)
        if container is None or not container.has(Test):
            return _blocked(
                "fix.no_test",
                "fix/diagnose-then-fix writes a reproducer and runs it to confirm the bug before "
                "fixing, so it needs a Test verb bound. Bind Test (e.g. PytestTest).",
            )

        ticket = inp.ticket
        params = FixParams(
            ticket=ticket.key,
            title=ticket.title,
            criteria=tuple(ticket.acceptance_criteria),
        )
        package = session.context(inp)

        try:
            # -- Reproduce ----------------------------------------------------------------------
            repro_inv = await self._run(session, "fix/reproducer", params, package)
            if repro_inv.truncated:
                return _truncated(session, repro_inv.cost)
            reproducer = session.workspace.changeset(ticket=ticket.key)
            if not reproducer.changes:
                return Outcome(
                    status=Status.FAILED,
                    reason="exhausted" if repro_inv.exhausted else "fix.no_reproducer",
                    cost=repro_inv.cost,
                    findings=(
                        Finding(
                            id="fix.no_reproducer",
                            message="the reproduce step staged no test, so the bug was never made "
                            "executable and there is nothing to fix against.",
                            severity=Severity.ERROR,
                            blocking=True,
                        ),
                    ),
                    decided=not repro_inv.exhausted,
                )

            async with materialize(session.repo_root, reproducer) as tree:
                red = await ctx.do(Test, _test_spec(tree, "fail"))
            if red.status is not Status.SUCCEEDED:
                return Outcome(
                    status=Status.FAILED,
                    reason="fix.not_reproduced",
                    value=FixReport(reproducer=reproducer, strategy=self.id, turns=repro_inv.turn_count),
                    cost=repro_inv.cost,
                    findings=(
                        Finding(
                            id="fix.not_reproduced",
                            message="the reproducer did not fail against the current code, so it has "
                            "not captured the bug. A fix run starts from a test that is red for the "
                            "reason the report describes.",
                            severity=Severity.ERROR,
                            blocking=True,
                        ),
                        *_test_findings(red),
                    ),
                    decided=red.decided,
                )

            # -- Fix ----------------------------------------------------------------------------
            fix_params = FixParams(
                ticket=ticket.key,
                title=ticket.title,
                criteria=tuple(ticket.acceptance_criteria),
                failure=_failure_text(red),
            )
            fix_inv = await self._run(session, "fix/fix-writer", fix_params, package)
            if fix_inv.truncated:
                return _truncated(session, repro_inv.cost + fix_inv.cost)
        except (InvocationBlocked, EgressRefused) as e:
            return _blocked(e.reason, str(e), staged=session.workspace.changes)
        except InvocationFailed as e:
            return Outcome(
                status=Status.ERRORED,
                reason=e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )

        summary, notes, unfinished, malformed = _read_reply(fix_inv.content)
        full = session.workspace.changeset(summary=summary, ticket=ticket.key)
        cost = repro_inv.cost + fix_inv.cost

        refusals = session.guard.check(full, workflow_id=session.workspace.workflow_id)
        if refusals:
            return Outcome(
                status=Status.BLOCKED,
                reason="guard.refused",
                cost=cost,
                findings=tuple(
                    Finding(
                        id="guard.refused",
                        message=f"{r.path} is refused (tier {r.tier}, rule {r.rule})",
                        severity=Severity.ERROR,
                        path=r.path,
                        blocking=True,
                    )
                    for r in refusals
                ),
            )

        repro_paths = set(reproducer.paths())
        fix_only = ChangeSet(changes=tuple(c for c in full.changes if c.path not in repro_paths))
        report = FixReport(
            reproducer=reproducer,
            fix=fix_only,
            summary=summary,
            notes=notes,
            unfinished=unfinished,
            strategy=self.id,
            turns=repro_inv.turn_count + fix_inv.turn_count,
        )
        findings = [*_reported(full, malformed, (repro_inv, fix_inv))]

        async with materialize(session.repo_root, full) as tree:
            green = await ctx.do(Test, _test_spec(tree, "pass"))
        if green.status is not Status.SUCCEEDED:
            return Outcome(
                status=Status.FAILED,
                reason="fix.not_fixed",
                value=report,
                cost=cost,
                findings=(
                    Finding(
                        id="fix.not_fixed",
                        message="the change did not make the reproducer pass; it is returned "
                        "unproposed so a fix that does not fix the bug does not open a pull request.",
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                    *findings,
                    *_test_findings(green),
                ),
                decided=green.decided,
            )

        return Outcome(
            status=Status.SUCCEEDED,
            value=report,
            cost=cost,
            findings=tuple(findings),
            decided=not fix_inv.exhausted,
            reason="exhausted" if fix_inv.exhausted else None,
        )

    async def _run(
        self, session: FixSession, prompt_id: str, params: FixParams, package: ContextPackage
    ) -> Any:
        lens = session.prompts[prompt_id]()
        system = lens.system(session.layers) + "\n\n" + _schema_instruction(FIX_SCHEMA)
        messages = lens.render(params, package)
        return await session.invoker.run(
            system=system,
            messages=messages,
            context=package,
            tools=session.tools,
            run_tool=session.run_tool,
            policy=session.policy,
        )


def _test_spec(tree: str, expect: str) -> Any:
    from ...core.types import TestSpec

    return TestSpec(root=tree, expect=expect)


def _failure_text(outcome: Any) -> str:
    """The reproducer's failure, handed to the fix step as its specification."""
    for finding in outcome.findings:
        if getattr(finding, "blocking", False):
            return str(finding.message)[:1000]
    return "the reproducer failed."


def _merge(base: ChangeSet, over: ChangeSet) -> ChangeSet:
    by_path = {c.path: c for c in base.changes}
    for change in over.changes:
        by_path[change.path] = change
    return ChangeSet(changes=tuple(by_path.values()), summary=over.summary or base.summary)


def _test_findings(outcome: Any) -> tuple[Finding, ...]:
    return tuple(
        Finding(id=f.id, message=f.message, severity=Severity.NOTE)
        for f in outcome.findings
        if getattr(f, "blocking", False)
    )


def _truncated(session: FixSession, cost: Any) -> Outcome[FixReport]:
    return Outcome(
        status=Status.ERRORED,
        reason="fix.truncated",
        cost=cost,
        findings=(
            Finding(
                id="fix.truncated",
                message=(
                    f"the model stopped at the {session.policy.max_tokens}-token output cap "
                    f"mid-answer. A write cut off there is a truncated file, so nothing staged in "
                    f"this session is returned. Raise `InvokePolicy.max_tokens` and re-run."
                ),
                severity=Severity.ERROR,
                blocking=True,
            ),
        ),
    )


def _read_reply(content: str) -> tuple[str, tuple[str, ...], tuple[str, ...], bool]:
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


def _reported(full: ChangeSet, malformed: bool, invocations: tuple[Any, ...]) -> list[Finding]:
    findings = [
        Finding(
            id="fix.staged",
            message=f"{'deleted' if change.deleted else 'wrote'} {change.path}",
            severity=Severity.NOTE,
            path=change.path,
        )
        for change in full.changes
    ]
    if malformed:
        findings.append(
            Finding(
                id="fix.unstructured",
                message="the final message was not the JSON the schema asked for; its text was kept "
                "as the summary. The staged change came through the tool boundary and is unaffected.",
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


def _executable(registry: StrategyRegistry) -> list[str]:
    names = []
    for registration in registry.for_verb(Verb.FIX):
        try:
            if hasattr(registration.factory(), "execute"):
                names.append(registration.id)
        except Exception:  # noqa: BLE001
            continue
    return names or ["(none)"]


def _blocked(reason: str, message: str, *, staged: list[Any] | None = None) -> Outcome[FixReport]:
    return Outcome(
        status=Status.BLOCKED,
        reason=reason,
        value=FixReport(reproducer=ChangeSet(changes=tuple(staged or ()))),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
