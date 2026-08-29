"""`implement/tdd` — write a failing test, watch it fail, then make it pass.

Not a prompt that asks a model to "do TDD"; a strategy that enforces it. The difference is the
whole argument of this framework: a rule stated only in prose is a request, and a model under
pressure to finish will write the test and the implementation together and call the result red→green
without either half ever having been run.

So the loop here is two model steps with a real, deterministic `Test` run standing between them:

1. **Red.** Ask for a failing test, and only the test. Materialise HEAD-plus-that-test in a
   throwaway worktree (slice 13a) and run the suite with `expect="fail"`. A test that passes here —
   or errors during collection, or collects nothing — has captured nothing to implement, and the
   run stops with `tdd.not_red` rather than pretending. This is the step a prompt cannot make
   honest on its own.
2. **Green.** Show the model the confirmed-red test, ask for the implementation, and run the suite
   again over test-plus-implementation with `expect="pass"`. If it is still red, the implementation
   did not satisfy the test, and the change comes back `FAILED` with the verdict rather than opening
   a pull request that does not work.

`ctx.do(Test, …)` needs a Test verb bound; without one there is no red and no green, so the strategy
refuses up front rather than degrade to an untested oneshot in disguise.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from ...ai.invoker import InvocationBlocked, InvocationFailed
from ...ai.structured import SchemaError, parse
from ...ai.structured import schema_instruction as _schema_instruction
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.types import ChangeSet, TestSpec
from ...core.verbs import Verb
from ...privileged.egress import EgressRefused
from ...prompts.implement import IMPLEMENT_SCHEMA, ImplementParams
from ..pytest_adapter import Test
from ..worktree import materialize
from .implement import ImplementReport, ImplementSession, ImplementSpec

_RED_DIRECTIVE = (
    "Step 1 of 2 — the failing test.\n\n"
    "Stage a test that captures the requirement above and fails against the code as it stands now. "
    "Stage only the test; do not implement the feature yet. When the test is staged, stop and reply "
    "with the summary. The framework will run it and confirm it is red before asking you to "
    "implement."
)


def _with_directive(base: list[Any], directive: str) -> list[Any]:
    """The rendered messages with a phase directive folded into the last (user) message.

    `replace` clones the message with new content, so this appends the step's instruction without
    importing the `Message` type from the `llm` layer — which `adapters` may not reach — and without
    a second consecutive user message some providers dislike.
    """
    last = base[-1]
    return [*base[:-1], replace(last, content=f"{last.content}\n\n{directive}")]


def _green_directive(tests: ChangeSet) -> str:
    listing = "\n\n".join(
        f"`{c.path}`:\n```\n{c.contents}\n```" for c in tests.changes if c.contents is not None
    )
    return (
        "Step 2 of 2 — the implementation.\n\n"
        "The test below is staged, and the framework has run it: it fails, as intended. Now stage "
        "the implementation that makes it pass. Do not edit, weaken, skip or delete the test — it is "
        "the specification now; change the code under test instead. When the change is staged, reply "
        "with the summary.\n\n" + listing
    )


class TddImplement:
    """Registered as `implement/tdd`. Holds no run state; everything arrives in the session."""

    id: ClassVar[str] = "implement/tdd"
    verb: ClassVar[Verb] = Verb.IMPLEMENT

    async def execute(
        self, ctx: Any, session: ImplementSession, inp: ImplementSpec
    ) -> Outcome[ImplementReport]:
        container = getattr(ctx, "container", None)
        if container is None or not container.has(Test):
            return _blocked(
                "tdd.no_test",
                "implement/tdd writes a failing test and runs it to confirm red before implementing, "
                "so it needs a Test verb bound. Bind Test (e.g. PytestTest), or select "
                "implement/oneshot, which does not require one.",
            )

        lens = session.prompts.get(self.id)
        if lens is None:
            return _blocked(
                "implement.no_prompt",
                f"no prompt registered for {self.id!r}; have {sorted(session.prompts)}",
            )
        prompt = lens()
        package = session.context(inp)
        ticket = inp.ticket
        system = prompt.system(session.layers) + "\n\n" + _schema_instruction(IMPLEMENT_SCHEMA)
        base = prompt.render(
            ImplementParams(
                ticket=ticket.key,
                title=ticket.title,
                criteria=tuple(ticket.acceptance_criteria),
            ),
            package,
        )

        try:
            # -- Phase 1: red -------------------------------------------------------------------
            red_inv = await session.invoker.run(
                system=system,
                messages=_with_directive(base, _RED_DIRECTIVE),
                context=package,
                tools=session.tools,
                run_tool=session.run_tool,
                policy=session.policy,
            )
            if red_inv.truncated:
                return _truncated(session, red_inv.cost)

            tests = session.workspace.changeset(ticket=ticket.key)
            if not tests.changes:
                return Outcome(
                    status=Status.FAILED,
                    reason="exhausted" if red_inv.exhausted else "tdd.no_test",
                    cost=red_inv.cost,
                    findings=(
                        Finding(
                            id="tdd.no_test",
                            message="the red step staged no test, so there was nothing to run — tdd "
                            "needs a failing test before it can implement.",
                            severity=Severity.ERROR,
                            blocking=True,
                        ),
                    ),
                    decided=not red_inv.exhausted,
                )

            async with materialize(session.repo_root, tests) as tree:
                red = await ctx.do(Test, TestSpec(root=tree, expect="fail"))
            if red.status is not Status.SUCCEEDED:
                # The test did not fail: it passed against the current code, collected nothing, or
                # errored on collection. Any of those means it captures nothing to implement.
                return Outcome(
                    status=Status.FAILED,
                    reason="tdd.not_red",
                    value=ImplementReport(changeset=tests, strategy=self.id, turns=red_inv.turn_count),
                    cost=red_inv.cost,
                    findings=(
                        Finding(
                            id="tdd.not_red",
                            message="the staged test did not fail against the current code, so it "
                            "specifies nothing to implement. A test-first change starts from a test "
                            "that is red for the right reason.",
                            severity=Severity.ERROR,
                            blocking=True,
                        ),
                        *_test_findings(red),
                    ),
                    decided=red.decided,
                )

            # -- Phase 2: green -----------------------------------------------------------------
            green_inv = await session.invoker.run(
                system=system,
                messages=_with_directive(base, _green_directive(tests)),
                context=package,
                tools=session.tools,
                run_tool=session.run_tool,
                policy=session.policy,
            )
            if green_inv.truncated:
                return _truncated(session, red_inv.cost + green_inv.cost)
        except (InvocationBlocked, EgressRefused) as e:
            return _blocked(e.reason, str(e), staged=session.workspace.changes)
        except InvocationFailed as e:
            return Outcome(
                status=Status.ERRORED,
                reason=e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )

        summary, notes, unfinished, malformed = _read_reply(green_inv.content)
        full = session.workspace.changeset(summary=summary, ticket=ticket.key)
        cost = red_inv.cost + green_inv.cost

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

        report = ImplementReport(
            changeset=full,
            summary=summary,
            notes=notes,
            unfinished=unfinished,
            strategy=self.id,
            turns=red_inv.turn_count + green_inv.turn_count,
        )
        findings = [*_reported(report, malformed, (red_inv, green_inv))]

        async with materialize(session.repo_root, full) as tree:
            green = await ctx.do(Test, TestSpec(root=tree, expect="pass"))
        if green.status is not Status.SUCCEEDED:
            return Outcome(
                status=Status.FAILED,
                reason="tdd.not_green",
                value=report,
                cost=cost,
                findings=(
                    Finding(
                        id="tdd.not_green",
                        message="the implementation did not make the staged test pass; the change is "
                        "returned unproposed so a red change does not open a pull request.",
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
            decided=not green_inv.exhausted,
            reason="exhausted" if green_inv.exhausted else None,
        )


def _test_findings(outcome: Any) -> tuple[Finding, ...]:
    """The Test verb's own blocking findings, carried up so a red/green failure explains itself."""
    return tuple(
        Finding(id=f.id, message=f.message, severity=Severity.NOTE)
        for f in outcome.findings
        if getattr(f, "blocking", False)
    )


def _truncated(session: ImplementSession, cost: Any) -> Outcome[ImplementReport]:
    return Outcome(
        status=Status.ERRORED,
        reason="implement.truncated",
        cost=cost,
        findings=(
            Finding(
                id="implement.truncated",
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
    """The cover note, leniently. Returns (summary, notes, unfinished, malformed)."""
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


def _reported(report: ImplementReport, malformed: bool, invocations: tuple[Any, ...]) -> list[Finding]:
    """What travels with the outcome: the staged paths, the gaps, and any injection signals."""
    findings = [
        Finding(
            id="implement.staged",
            message=f"{'deleted' if change.deleted else 'wrote'} {change.path}",
            severity=Severity.NOTE,
            path=change.path,
        )
        for change in report.changeset.changes
    ]
    findings += [
        Finding(id="implement.unfinished", message=gap, severity=Severity.WARNING)
        for gap in report.unfinished
    ]
    if malformed:
        findings.append(
            Finding(
                id="implement.unstructured",
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


def _blocked(reason: str, message: str, *, staged: list[Any] | None = None) -> Outcome[ImplementReport]:
    return Outcome(
        status=Status.BLOCKED,
        reason=reason,
        value=ImplementReport(changeset=ChangeSet(changes=tuple(staged or ()))),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
