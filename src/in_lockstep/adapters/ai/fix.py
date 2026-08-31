"""Fix, backed by a model — reproduce the bug, then fix it, and prove both.

The shape Bug Fix has that Implement does not is that the *bug* has to be captured before the fix
is, and captured as something that fails. So this is two model steps with a real Test run between
them, the same spine `TDD` uses, but the halves mean different things and are reported
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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ...ai.builtins import ToolRunnerImpl, Workspace
from ...ai.context import ContextCurator, ContextItem, ContextNeed, ContextPackage
from ...ai.invoker import AiInvoker, InvokePolicy
from ...ai.prompt import PromptLayers
from ...ai.structured import schema_instruction as _schema_instruction
from ...ai.tools import ToolSet
from ...core.changes import ChangeGuard
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.types import ChangeSet, Test
from ...core.verbs import Capability, Verb
from ...prompts.fix import FIX_PROMPTS, FIX_SCHEMA, FixParams, FixPrompt, fix_layers
from ..worktree import materialize
from .strategy import AGENCY, AiStrategy, PhaseError, read_reply, reported, run_phase, test_findings


@dataclass(frozen=True)
class Fix:
    """The Fix request: which bug to fix. Workflows do `ctx.do(Fix(...))`; the binding decides
    which strategy runs it. Frozen: it is hashed for step identity."""

    #: The bug report, untrusted by construction — anyone who can file one writes into this prompt.
    ticket: Any
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
    """Everything one run needs, assembled per invoke by `AiStrategy._session`. Mirrors
    `ImplementSession`; a fix strategy writes files, so it carries a workspace and the
    write/execute tools."""

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

    def context(self, spec: Fix) -> ContextPackage:
        items: list[ContextItem] = list(spec.ticket.as_context())
        return self.curator.curate(items, ContextNeed(token_budget=spec.token_budget))


class FixStrategy(AiStrategy):
    """The base for anything serving `Fix`. Subclass this, not `AiStrategy`.

    `ImplementStrategy`'s reasoning, for the fixing verb: the verb, the capability set, the session
    type, the prompt map and the layers, declared once where a subclass cannot narrow them.
    """

    verb: ClassVar[Verb] = Verb.FIX
    request: ClassVar[Any] = Fix
    capabilities: ClassVar[frozenset[Capability]] = AGENCY
    _session_cls = FixSession
    _shipped_prompts = FIX_PROMPTS
    _layers_factory = staticmethod(fix_layers)


class DiagnoseThenFix(FixStrategy):
    """Bound as the Fix adapter: `lockstep.bind(Fix, DiagnoseThenFix(...))`. Reproduce the bug as
    a failing test, then fix it, and prove both."""

    id: ClassVar[str] = "fix/diagnose-then-fix"

    async def invoke(self, ctx: Any, inp: Fix) -> Outcome[FixReport]:
        container = getattr(ctx, "container", None)
        if container is None or not container.has(Test):
            return _blocked(
                "fix.no_test",
                "DiagnoseThenFix writes a reproducer and runs it to confirm the bug before "
                "fixing, so it needs a Test verb bound. Bind Test (e.g. PytestTest).",
            )

        session = self._session(ctx)
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
                red = await ctx.do(_test_spec(tree, "fail"))
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
                        *test_findings(red),
                    ),
                    decided=red.decided,
                )

            # -- Fix ----------------------------------------------------------------------------
            # The fix step has to see the reproducer it must make pass: it is staged, not on disk,
            # so `read_file` cannot reach it — it travels in the prompt, the way tdd hands its test
            # to the implement step. It is the model's own prior output, tagged untrusted like the
            # ticket, so echoing it back crosses no new trust boundary.
            fix_params = FixParams(
                ticket=ticket.key,
                title=ticket.title,
                criteria=tuple(ticket.acceptance_criteria),
                failure=_fix_specification(reproducer, red),
            )
            fix_inv = await self._run(session, "fix/fix-writer", fix_params, package)
        except PhaseError as e:
            return e.outcome

        summary, notes, unfinished, malformed = read_reply(fix_inv.content)
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
        findings = reported(full, malformed=malformed, invocations=(repro_inv, fix_inv), prefix="fix")

        async with materialize(session.repo_root, full) as tree:
            green = await ctx.do(_test_spec(tree, "pass"))
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
                    *test_findings(green),
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
        """One phase: compose the prompt's system + schema, render the user message, and run the
        model loop through `run_phase` — which raises `PhaseError` on a refusal, failure or
        truncation, caught once around both phases in `execute`."""
        lens = session.prompts[prompt_id]()
        system = lens.system(session.layers) + "\n\n" + _schema_instruction(FIX_SCHEMA)
        return await run_phase(session, system, lens.render(params, package), package, prefix="fix")


def _test_spec(tree: str, expect: str) -> Test:
    return Test(root=tree, expect=expect)


def _fix_specification(reproducer: ChangeSet, red: Any) -> str:
    """What the fix step is fixing against: the reproducer test that fails, and how it failed.

    The reproducer is the specification now, so the fix step needs to see it in full — it is staged,
    not on disk. A confirmed-red run satisfies `expect="fail"` and so carries no blocking finding,
    which is why the failure line falls back to a plain statement rather than pretending to detail.
    """
    listing = "\n\n".join(
        f"`{c.path}`:\n```\n{c.contents}\n```" for c in reproducer.changes if c.contents is not None
    )
    return f"The reproducer, which fails against the current code:\n\n{listing}\n\n{_failure_text(red)}"


def _failure_text(outcome: Any) -> str:
    for finding in outcome.findings:
        if getattr(finding, "blocking", False):
            return str(finding.message)[:1000]
    return "It fails as intended; make it pass by fixing the cause, not by editing the test."


def _merge(base: ChangeSet, over: ChangeSet) -> ChangeSet:
    by_path = {c.path: c for c in base.changes}
    for change in over.changes:
        by_path[change.path] = change
    return ChangeSet(changes=tuple(by_path.values()), summary=over.summary or base.summary)


def _blocked(reason: str, message: str, *, staged: list[Any] | None = None) -> Outcome[FixReport]:
    return Outcome(
        status=Status.BLOCKED,
        reason=reason,
        value=FixReport(reproducer=ChangeSet(changes=tuple(staged or ()))),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
