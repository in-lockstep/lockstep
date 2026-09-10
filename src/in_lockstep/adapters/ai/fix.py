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
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from ...ai.builtins import ToolRunnerImpl, Workspace
from ...ai.context import ContextCurator, ContextItem, ContextNeed, ContextPackage
from ...ai.invoker import AiInvoker, InvokePolicy
from ...ai.prompt import PromptLayers
from ...ai.structured import schema_instruction as _schema_instruction
from ...ai.tools import ToolSet
from ...core.changes import ChangeGuard
from ...core.outcome import Cost, Finding, Outcome, Severity, Status
from ...core.types import ChangeSet, Test, ValidationReport
from ...core.verbs import Capability, Verb
from ...prompts.fix import FIX_PROMPTS, FIX_SCHEMA, FixParams, FixPrompt, fix_layers
from ..worktree import prepared, staged_refusal, staged_runner
from .strategy import (
    AGENCY,
    AiStrategy,
    PhaseError,
    collected_nothing,
    elsewhere_only,
    not_a_verdict,
    nothing_collected_finding,
    read_reply,
    reported,
    run_phase,
    search_notes,
    test_findings,
    validated,
)


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
    #: Consecutive turns at the end that staged nothing and tested nothing new (#337).
    idle_turns: int = 0
    #: What the repository's own validator said about the whole change, or None when nothing
    #: checked it. On the report rather than on either half, because the validator was pointed at
    #: the set that travels and a finding does not belong to the reproducer or the fix by itself.
    validation: ValidationReport | None = None

    @property
    def changeset(self) -> ChangeSet:
        """The whole change, reproducer and fix together — what `apply`/`open_change` writes.

        The cover note is the report's, whichever half it was stored on: a report built by hand
        with a bare `fix` set and a `summary` still proposes under that summary."""
        merged = _merge(self.reproducer, self.fix)
        return replace(merged, summary=self.summary or merged.summary, notes=self.notes or merged.notes)

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
    # As for implement: `fix.yml` and `ai-generated.yml` trigger on `issue_comment` and
    # `issues`, both of which run on the default branch.
    reads_house_rules: ClassVar[bool] = True


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
        # As in TDD: refused before the reproducer is asked for, so nothing is spent on a test that
        # would then be refused a runner (GATE-SANDBOX-2).
        if (why := staged_refusal(ctx)) is not None:
            return _blocked("sandbox.host_fallback", why)

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
            staged = session.workspace.changeset(ticket=ticket.key)
            # The reproducer is the TEST-shaped part of what the reproduce step staged. The step
            # is asked for a test and nothing else, and with a cheap edit at hand a model fixes
            # the bug in the same breath: the eighth `/fix` on this repository's own #319 staged
            # the reproducer, the fix and a ledger row together, the red run passed over all
            # three, and the run ended `fix.not_reproduced` about a bug it had reproduced and
            # fixed (#337). The red run is over the tests alone, which is what "red" is a claim
            # about; whatever else was staged stays staged and reaches the fix step as a head
            # start, where the green run over everything is what decides.
            reproducer = ChangeSet(
                changes=tuple(c for c in staged.changes if session.guard.is_test(c.path)),
                ticket=ticket.key,
            )
            early = tuple(c.path for c in staged.changes if not session.guard.is_test(c.path))
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

            # `prepared`, as in `tdd`: a tree with no environment collects nothing, and a
            # reproducer that never ran reads here as a reproducer that did not reproduce.
            async with prepared(ctx, session.repo_root, reproducer, for_verb=Test) as (tree, _note):
                red = await ctx.do(_test_spec(tree, "fail", runner=staged_runner(ctx, Test)))
            if (stopped := not_a_verdict(red, cost=repro_inv.cost)) is not None:
                return stopped
            if red.status is not Status.SUCCEEDED:
                # "The reproducer did not fail" is a claim about the reproducer, and a suite that
                # collected nothing supports no claim about it -- the same split `tdd` makes in both
                # of its phases. A model told its reproducer did not reproduce rewrites a test that
                # was never executed.
                nothing = collected_nothing(red)
                return Outcome(
                    status=Status.FAILED,
                    reason="fix.suite_collected_nothing" if nothing else "fix.not_reproduced",
                    value=FixReport(reproducer=reproducer, strategy=self.id, turns=repro_inv.turn_count),
                    cost=repro_inv.cost,
                    findings=(
                        nothing_collected_finding("fix.suite_collected_nothing", "reproducer")
                        if nothing
                        else Finding(
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
                failure=_fix_specification(reproducer, red, early=early),
            )
            fix_inv = await self._run(session, "fix/fix-writer", fix_params, package)
        except PhaseError as e:
            return e.outcome

        summary, notes, unfinished, malformed = read_reply(fix_inv.content)
        # A reply that did not parse keeps its text on the REPORT, where the ledger and a person
        # reading the record can have it -- and out of the change set, which is what a pull-request
        # body renders. `read_reply` keeps the text so the work is not thrown away; publishing it
        # was never what that was for, and #389's opening paragraph was a model reasoning about its
        # own test mocks because this line did not exist (#398).
        staged_summary = "" if malformed else summary
        full = session.workspace.changeset(summary=staged_summary, ticket=ticket.key)

        # The repository's own checks, before the green run below rather than after it: a
        # deterministic fix and a repair turn both change the code, and a green proved of bytes
        # that no longer travel is a claim about something else.
        repair_system, repair_messages = self._compose(session, "fix/fix-writer", fix_params, package)
        validation = await validated(
            ctx,
            session,
            full,
            system=repair_system,
            messages=repair_messages,
            package=package,
            prefix="fix",
        )
        # Rebuilt from the workspace, which is where both tiers wrote -- and the split below keys
        # on the reproducer's paths, so a repair turn's edits land on the fix half where they
        # belong without this having to say so.
        full = session.workspace.changeset(summary=staged_summary, ticket=ticket.key)
        cost = repro_inv.cost + fix_inv.cost + sum((i.cost for i in validation.invocations), Cost())

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
        # The cover note rides on the fix half, not only on the report: `changeset` below merges
        # the two halves for `write_changeset`, and a merge of two sets that carried no summary is
        # a set with none — which is how the first fix this loop opened here was titled by its
        # ticket number and described by nothing (#343).
        fix_only = ChangeSet(
            changes=tuple(c for c in full.changes if c.path not in repro_paths),
            summary=staged_summary,
            notes=notes,
            ticket=ticket.key,
        )
        report = FixReport(
            reproducer=reproducer,
            fix=fix_only,
            summary=summary,
            notes=notes,
            unfinished=unfinished,
            strategy=self.id,
            turns=repro_inv.turn_count
            + fix_inv.turn_count
            + sum(i.turn_count for i in validation.invocations),
            idle_turns=fix_inv.idle_turns,
            validation=validation.report,
        )
        findings = reported(
            full,
            malformed=malformed,
            invocations=(repro_inv, fix_inv, *validation.invocations),
            prefix="fix",
            notes=search_notes(session) + validation.findings(),
        )

        async with prepared(ctx, session.repo_root, full, for_verb=Test) as (tree, _note):
            green = await ctx.do(_test_spec(tree, "pass", runner=staged_runner(ctx, Test)))
        if (stopped := not_a_verdict(green, value=report, cost=cost)) is not None:
            return stopped
        foreign = elsewhere_only(green, full) if green.decided else ()
        if foreign:
            # As in TDD, and for the same reason: every failure is in a file this change did not
            # touch, so "the change did not make the reproducer pass" would be false. The
            # reproducer went red and then green; the suite is red for a reason this run did not
            # cause. It travels with the failures named (#405).
            findings.append(
                Finding(
                    id="fix.suite_red_elsewhere",
                    message=(
                        f"the reproducer passed; {len(foreign)} failure(s) elsewhere in the suite, "
                        f"in files this change did not touch: {', '.join(foreign[:5])}"
                        + (f" …and {len(foreign) - 5} more" if len(foreign) > 5 else "")
                    ),
                    severity=Severity.WARNING,
                )
            )
        elif green.status is not Status.SUCCEEDED:
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

    def _compose(
        self, session: FixSession, prompt_id: str, params: FixParams, package: ContextPackage
    ) -> tuple[str, Any]:
        """The system prompt and rendered messages for one phase.

        Its own function because two callers need the same pair and neither may derive it
        separately: `_run` makes the phase, and the validate step's repair turn continues the same
        conversation. Composing it twice is how the repair would end up asking a differently
        instructed model than the one that wrote the code it is repairing.
        """
        lens = session.prompts[prompt_id]()
        return (
            lens.system(session.layers) + "\n\n" + _schema_instruction(FIX_SCHEMA),
            lens.render(params, package),
        )

    async def _run(
        self, session: FixSession, prompt_id: str, params: FixParams, package: ContextPackage
    ) -> Any:
        """One phase: compose the prompt's system + schema, render the user message, and run the
        model loop through `run_phase` — which raises `PhaseError` on a refusal, failure or
        truncation, caught once around both phases in `execute`."""
        system, messages = self._compose(session, prompt_id, params, package)
        return await run_phase(
            session,
            system,
            messages,
            package,
            prefix="fix",
            schema=FIX_SCHEMA,
            # Whatever is staged is the reproducer or the reproducer plus the fix; `changeset`
            # merges the two, so the whole attempt travels under `reproducer` alone.
            stalled_report=lambda staged: FixReport(reproducer=staged, strategy=self.id),
        )


def _test_spec(tree: str, expect: str, *, runner: object = None) -> Test:
    return Test(root=tree, expect=expect, runner=runner)


def _fix_specification(reproducer: ChangeSet, red: Any, *, early: tuple[str, ...] = ()) -> str:
    """What the fix step is fixing against: the reproducer test that fails, and how it failed.

    The reproducer is the specification now, so the fix step needs to see it in full — it is staged,
    not on disk. A confirmed-red run satisfies `expect="fail"` and so carries no blocking finding,
    which is why the failure line falls back to a plain statement rather than pretending to detail.
    """
    listing = "\n\n".join(
        f"`{c.path}`:\n```\n{c.contents}\n```" for c in reproducer.changes if c.contents is not None
    )
    head_start = (
        (
            "\n\nYou already staged changes to "
            + ", ".join(f"`{p}`" for p in early)
            + " in the reproduce step. They are in the change set and `read_file` shows them; the "
            "suite has not yet run over them together with the test. Finish the fix from there "
            "rather than starting over."
        )
        if early
        else ""
    )
    return (
        f"The reproducer, which fails against the current code:\n\n{listing}\n\n{_failure_text(red)}"
    ) + head_start


def _failure_text(outcome: Any) -> str:
    for finding in outcome.findings:
        if getattr(finding, "blocking", False):
            return str(finding.message)[:1000]
    return "It fails as intended; make it pass by fixing the cause, not by editing the test."


def _merge(base: ChangeSet, over: ChangeSet) -> ChangeSet:
    by_path = {c.path: c for c in base.changes}
    for change in over.changes:
        by_path[change.path] = change
    return ChangeSet(
        changes=tuple(by_path.values()),
        summary=over.summary or base.summary,
        notes=over.notes or base.notes,
        ticket=over.ticket or base.ticket,
    )


def _blocked(reason: str, message: str, *, staged: list[Any] | None = None) -> Outcome[FixReport]:
    return Outcome(
        status=Status.BLOCKED,
        reason=reason,
        value=FixReport(reproducer=ChangeSet(changes=tuple(staged or ()))),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
