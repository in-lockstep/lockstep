"""`TDD` — write a failing test, watch it fail, then make it pass.

Bound as the Implement adapter: `lockstep.bind(Implement, TDD(...))`.

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

`ctx.do(Test(...))` needs a Test verb bound; without one there is no red and no green, so the strategy
refuses up front rather than degrade to an untested oneshot in disguise.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from ...ai.structured import schema_instruction as _schema_instruction
from ...core.outcome import Cost, Finding, Outcome, Severity, Status
from ...core.types import ChangeSet, Test
from ...prompts.implement import IMPLEMENT_SCHEMA, ImplementParams
from ..worktree import head_state, prepared, staged_refusal, staged_runner
from .implement import Implement, ImplementReport, ImplementSession, ImplementStrategy
from .strategy import (
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
    with_directive,
)


async def _uncollected(ctx: Any, tree: str, tests: ChangeSet, runner: Any = None) -> tuple[str, ...]:
    """The staged Python files, if running the suite over just those collects no tests at all.

    One extra pytest invocation scoped to the new files, which costs about a second because it runs
    only them. The alternative — a baseline run of the whole suite to compare totals against —
    doubles the slowest step of the strategy to learn the same fact.

    Empty means "collection is not the problem", which includes the case where the second run could
    not be scoped at all. This decides the wording of a failure, so a wrong guess would be a
    confident, specific, wrong diagnosis; when it cannot tell, it says nothing and the general
    message stands.

    **`runner` is the one the run being diagnosed used, and passing it is the whole point.** This
    dispatched `Test` with no runner at all, so it fell back to the BOUND adapter's own sandbox --
    `None` for this repository, meaning the host -- while the red run it was explaining had run in
    a container. The probe then answered truthfully about an environment nobody had asked about:
    the tests collect fine on a laptop that has a venv, so it reported "collection is not the
    problem" about a container where the suite had collected nothing at all. A diagnostic that
    inspects a different thing from the one that failed is the defect it exists to find, wearing
    its own clothes (#421 is the same shape a layer out).
    """
    paths = tuple(sorted(c.path for c in tests.changes if c.path.endswith(".py")))
    if not paths:
        return ()
    probe = await ctx.do(Test(root=tree, paths=paths, expect="fail", runner=runner))
    report = probe.value
    if report is None:
        return ()
    return paths if report.passed + report.failed == 0 else ()


def _not_red_finding(uncollected: tuple[str, ...], *, nothing: bool = False) -> Finding:
    """One of three messages, and the difference is what the next attempt does about it."""
    if nothing:
        return nothing_collected_finding("tdd.suite_collected_nothing", "red")
    if uncollected:
        return Finding(
            id="tdd.test_not_collected",
            message=(
                "the staged test was never run: pytest collected 0 tests from "
                f"{', '.join(uncollected)}. The suite is green because it did not execute your "
                "test, not because your test passes. Check this repository's collection settings "
                "before rewriting the assertions — `python_files`, `python_classes` and "
                "`python_functions` in pyproject.toml or pytest.ini decide which names are picked "
                "up, and `testpaths` decides which directories are searched at all. A class or "
                "file that does not match is skipped in silence."
            ),
            severity=Severity.ERROR,
            blocking=True,
        )
    return Finding(
        id="tdd.not_red",
        message="the staged test did not fail against the current code, so it "
        "specifies nothing to implement. A test-first change starts from a test "
        "that is red for the right reason.",
        severity=Severity.ERROR,
        blocking=True,
    )


_RED_DIRECTIVE = (
    "Step 1 of 2 — the failing test.\n\n"
    "Stage a test that captures the requirement above and fails against the code as it stands now. "
    "Stage only the test; do not implement the feature yet. When the test is staged, stop and reply "
    "with the summary. The framework will run it and confirm it is red before asking you to "
    "implement."
)


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


class TDD(ImplementStrategy):
    """Red then green: write a failing test, confirm red, implement, confirm green."""

    id: ClassVar[str] = "implement/tdd"

    async def invoke(self, ctx: Any, inp: Implement) -> Outcome[ImplementReport]:
        container = getattr(ctx, "container", None)
        if container is None or not container.has(Test):
            return _blocked(
                "tdd.no_test",
                "TDD writes a failing test and runs it to confirm red before implementing, "
                "so it needs a Test verb bound. Bind Test (e.g. PytestTest), or bind "
                "Oneshot, which does not require one.",
            )
        # Before the first model call, not before the first worktree: a red phase that will be
        # refused at its Test is a phase whose spend buys nothing (GATE-SANDBOX-2).
        if (why := staged_refusal(ctx)) is not None:
            return _blocked("sandbox.host_fallback", why)

        session = self._session(ctx)
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
            red_inv = await run_phase(
                session,
                system,
                with_directive(base, _RED_DIRECTIVE),
                package,
                prefix="implement",
                schema=IMPLEMENT_SCHEMA,
                stalled_report=lambda staged: ImplementReport(changeset=staged, strategy=self.id),
            )

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

            # `prepared`, not `materialize`: this dispatches the suite, so the tree needs the
            # environment the repository's own `Provision` builds. A bare worktree has none since
            # #419 dropped the mounted host `.venv`, and pytest in an image that never installed
            # this project collects ZERO tests -- which arrives here as a run that decided nothing
            # and is then reported as `tdd.not_red`, the sentence this phase exists to avoid.
            async with prepared(ctx, session.repo_root, tests, for_verb=Test) as (tree, _note):
                where = staged_runner(ctx, Test)
                red = await ctx.do(Test(root=tree, expect="fail", runner=where))
                # Red means the suite ran and failed, so a run that decided nothing -- collected
                # nothing, or never reported -- is not red however it exited. Which of the three
                # it was has to be decided while the worktree still exists.
                went_red = red.status is Status.SUCCEEDED and red.decided
                # No probe where the whole suite collected nothing: there is no "did YOUR test run"
                # question left to answer, and a second pytest invocation would cost a second to
                # learn the same fact. The probe runs under the runner the red run used, or it
                # answers about a different environment -- which is how this arrived.
                nothing = not went_red and collected_nothing(red)
                uncollected = (
                    await _uncollected(ctx, tree, tests, where) if not went_red and not nothing else ()
                )
            if (stopped := not_a_verdict(red, cost=red.cost)) is not None:
                # A ceiling or a broken runner, not a test that passed when it should have failed.
                return stopped
            if not went_red:
                # The test did not fail, and the three reasons for that are not one finding.
                #
                # It passed against the current code — the model tested something that already
                # works. Or the suite never ran it, because the file, the class or the directory
                # does not match what this repository collects. Both used to arrive as "the staged
                # test did not fail against the current code", which is a true sentence about the
                # first case and a misleading one about the second: a model told its test passed
                # rewrites the assertions, and the assertions were never the problem.
                #
                # It is not a hypothetical. Run 33566828825 spent $31.53 staging tests into a
                # repository whose `python_classes = ["*Tests"]` meant a `Test*` class was collected
                # by nothing; the suite ran 1581 tests, exactly the number it runs on a clean tree,
                # and reported that the staged test had passed.
                return Outcome(
                    status=Status.FAILED,
                    reason=(
                        "tdd.suite_collected_nothing"
                        if nothing
                        else "tdd.test_not_collected"
                        if uncollected
                        else "tdd.not_red"
                    ),
                    value=ImplementReport(changeset=tests, strategy=self.id, turns=red_inv.turn_count),
                    cost=red_inv.cost,
                    findings=(
                        _not_red_finding(uncollected, nothing=nothing),
                        *test_findings(red),
                    ),
                    decided=red.decided,
                )

            # -- Phase 2: green -----------------------------------------------------------------
            green_inv = await run_phase(
                session,
                system,
                with_directive(base, _green_directive(tests)),
                package,
                prefix="implement",
                schema=IMPLEMENT_SCHEMA,
                stalled_report=lambda staged: ImplementReport(changeset=staged, strategy=self.id),
            )
        except PhaseError as e:
            return e.outcome

        summary, notes, unfinished, malformed = read_reply(green_inv.content)
        # A reply that did not parse keeps its text on the REPORT, where the ledger and a person
        # reading the record can have it -- and out of the change set, which is what a pull-request
        # body renders. `read_reply` keeps the text so the work is not thrown away; publishing it
        # was never what that was for, and #389's opening paragraph was a model reasoning about its
        # own test mocks because this line did not exist (#398).
        staged_summary = "" if malformed else summary
        full = session.workspace.changeset(summary=staged_summary, ticket=ticket.key, notes=notes)

        # The repository's own checks, over what this session staged, BEFORE the green run below.
        # Order matters: a deterministic fix and a repair turn both change the code, so confirming
        # green first would prove it of bytes that no longer travel. This way both claims -- it
        # lints clean, it makes the test pass -- are claims about the same change.
        validation = await validated(
            ctx, session, full, system=system, messages=base, package=package, prefix="implement"
        )
        # Rebuilt, not patched: `validated` writes through the workspace, which is the one place a
        # staged set is assembled.
        full = session.workspace.changeset(summary=staged_summary, ticket=ticket.key, notes=notes)
        cost = red_inv.cost + green_inv.cost + sum((i.cost for i in validation.invocations), Cost())

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
            turns=red_inv.turn_count
            + green_inv.turn_count
            + sum(i.turn_count for i in validation.invocations),
            idle_turns=green_inv.idle_turns,
            validation=validation.report,
        )
        findings = reported(
            report.changeset,
            unfinished=report.unfinished,
            malformed=malformed,
            invocations=(red_inv, green_inv, *validation.invocations),
            prefix="implement",
            notes=search_notes(session) + validation.findings(),
        )

        async with prepared(ctx, session.repo_root, full, for_verb=Test) as (tree, _note):
            green = await ctx.do(Test(root=tree, expect="pass", runner=staged_runner(ctx, Test)))
        if (stopped := not_a_verdict(green, value=report, cost=cost)) is not None:
            return stopped
        foreign = elsewhere_only(green, full) if green.decided else ()
        if foreign:
            # Every failure is in a file this change did not touch, so the claim below -- that the
            # implementation did not make the staged test pass -- would be false. The change is
            # complete and its own tests are green; the suite is red for a reason this run did not
            # cause, which is a fact for a person and not grounds to destroy the work. It travels
            # with the failures named, and the propose half opens it as a draft (#405).
            findings.append(
                Finding(
                    id="tdd.suite_red_elsewhere",
                    message=(
                        f"the staged tests passed; {len(foreign)} failure(s) elsewhere in the suite, "
                        f"in files this change did not touch: {', '.join(foreign[:5])}"
                        + (f" …and {len(foreign) - 5} more" if len(foreign) > 5 else "")
                    ),
                    severity=Severity.WARNING,
                )
            )
        elif green.status is not Status.SUCCEEDED or not green.decided:
            # The same three-way split the red phase makes, for the same reason: "the implementation
            # did not make the staged test pass" is a claim about the implementation, and a suite
            # that collected nothing supports no claim about it at all.
            green_nothing = collected_nothing(green)
            return Outcome(
                status=Status.FAILED,
                reason="tdd.suite_collected_nothing" if green_nothing else "tdd.not_green",
                value=report,
                cost=cost,
                findings=(
                    nothing_collected_finding("tdd.suite_collected_nothing", "green")
                    if green_nothing
                    else Finding(
                        id="tdd.not_green",
                        message="the implementation did not make the staged test pass; the change is "
                        "returned unproposed so a red change does not open a pull request.",
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                    *findings,
                    *test_findings(green),
                ),
                decided=green.decided,
            )

        # -- Phase 3: does it answer the ticket? --------------------------------------------
        #
        # Green is not done. The suite proves a staged test went from failing to passing, which is
        # arithmetic; whether the change does what the ticket ASKED FOR is judgement, and until
        # this phase nothing asked it. #450 satisfied its own tests, skipped an acceptance
        # criterion outright, and wrote a test asserting the omission as a requirement -- green
        # throughout, caught by a person reading the issue beside the diff.
        #
        # A loop rather than a check, because a criterion a second reader can name is usually one
        # the author can fix: the unmet criteria go back to the SAME phase that wrote the code,
        # with the assessor's reasons attached, and the suite runs again over whatever comes back.
        # Bounded by `max_assess_rounds` and not by the budget -- this is the one loop here whose
        # exit condition is a model's judgement, and two models can disagree for as long as
        # somebody is paying (#452).
        assessment, findings, full, report, cost = await self._assess_rounds(
            ctx,
            session,
            inp,
            system=system,
            base=base,
            package=package,
            tests=tests,
            full=full,
            report=report,
            findings=findings,
            cost=cost,
            ticket=ticket,
            notes=notes,
            staged_summary=staged_summary,
        )

        # Revert-and-verify: red→green already proved the implementation load-bearing against the
        # phase-1 test, but nothing yet proves it against the *final* test — a phase-2 edit could
        # have weakened the test into passing on its own. Undo the implementation (keeping the test)
        # and confirm the suite returns to red. Advisory, not blocking: the change did pass its test.
        findings += await _revert_verify(ctx, session, tests, full)

        return Outcome(
            status=Status.SUCCEEDED,
            value=report,
            cost=cost,
            findings=tuple(findings),
            decided=not green_inv.exhausted,
            reason="exhausted" if green_inv.exhausted else None,
        )

    async def _assess_rounds(
        self,
        ctx: Any,
        session: ImplementSession,
        inp: Any,
        *,
        system: str,
        base: list[Any],
        package: Any,
        tests: ChangeSet,
        full: ChangeSet,
        report: ImplementReport,
        findings: list[Finding],
        cost: Cost,
        ticket: Any,
        notes: tuple[str, ...],
        staged_summary: str,
    ) -> tuple[Any, list[Finding], ChangeSet, ImplementReport, Cost]:
        """Assess, and correct while rounds remain. Returns what the caller must carry forward.

        Skipped entirely -- no finding, no cost -- where no `Assess` verb is bound or the ticket
        states no acceptance criteria. Both are ordinary: an adopter who has not bound an assessor
        gets exactly the TDD they had, and a ticket with nothing to check against cannot be checked
        against nothing. Neither is reported as an assessment that passed, which is the distinction
        `AssessReport.met` refuses to blur by being False over no verdicts.

        **Every round re-runs the suite.** A correction edits code to satisfy a criterion and can
        break the tests the green phase proved; without the re-run a change ships whose green was
        established two rounds ago, which is a verdict about a suite that did not run wearing a
        different hat (#435).
        """
        from ...core.types import Assess

        container = getattr(ctx, "container", None)
        criteria = tuple(getattr(ticket, "acceptance_criteria", ()) or ())
        if container is None or not container.has(Assess):
            # No assessor bound is a configuration choice, not a surprise, and a note on every
            # run of every repository that has not bound one would be noise.
            return None, findings, full, report, cost
        if not criteria:
            # Said out loud, because somebody bound an assessor and therefore wants assessment
            # -- and because the likeliest cause is a PARSE rather than an absence.
            #
            # `criteria_from` reads an `Acceptance` or `Acceptance criteria` heading and nothing
            # else. #443's `/implement` ran with zero criteria against a ticket whose acceptance
            # section a reader can see, and skipping in silence meant $17.55 produced no
            # evidence about the one phase it was meant to exercise. A reader of that record saw
            # nothing and would reasonably conclude the change had been assessed and passed.
            #
            # Absent is not met, and this is the third reading of it in this feature: the report
            # is False over no verdicts, an unanswered criterion is unmet, and now a run that
            # found nothing to assess against says so instead of looking like one that did.
            findings.append(
                Finding(
                    id="assess.no_criteria",
                    message="an assessor is bound and this ticket states no acceptance criteria "
                    "that could be read, so the change was NOT assessed against it. Criteria are "
                    "read from an `Acceptance` or `Acceptance criteria` heading, or from a task "
                    "list; a section under any other heading is not found.",
                    severity=Severity.WARNING,
                )
            )
            return None, findings, full, report, cost

        assessment = None
        rounds = max(1, session.policy.max_assess_rounds)
        for round_number in range(1, rounds + 1):
            outcome = await _assess(ctx, inp, full, round_number)
            assessment = outcome.value
            if outcome.status is not Status.SUCCEEDED or assessment is None:
                # A refused or errored assessor is not a failed change. The control said nothing,
                # and saying nothing is recorded as such rather than folded into the change's
                # verdict -- `blocked` is the control working, and an unrouted assessor is a line
                # somebody has not written yet rather than a defect in the work.
                findings.append(
                    Finding(
                        id="assess.not_assessed",
                        message=f"the change was not assessed against its acceptance criteria: "
                        f"{outcome.reason or 'the assessor reported nothing'}",
                        severity=Severity.WARNING,
                    )
                )
                return None, findings, full, replace(report, assessment=None), cost + (outcome.cost or Cost())
            cost = cost + (outcome.cost or Cost())
            if assessment.met:
                findings.append(
                    Finding(
                        id="assess.met",
                        # The COUNT, because it is the cheapest check on the parse. An issue with
                        # two acceptance sections yields the first one, and a reader who sees
                        # "all 4" beside a ticket showing seven knows to look (#443 has both).
                        message=f"all {len(assessment.verdicts)} acceptance criteria met, assessed by "
                        f"{assessment.assessor or 'the routed assessor'}"
                        + (f" after {round_number} rounds" if round_number > 1 else ""),
                        severity=Severity.NOTE,
                    )
                )
                break
            if round_number >= rounds:
                # Exhausted. The change travels as a DRAFT with the gap named -- the same answer
                # `implement/propose` already gives a change whose suite is red elsewhere. Failing
                # here would destroy an implementation that may meet four criteria of five, and
                # marking it ready would be a green-looking pull request whose own assessor said
                # it missed the point.
                findings.append(
                    Finding(
                        id="assess.unmet",
                        message=f"{len(assessment.unmet)} of {len(assessment.verdicts)} acceptance "
                        f"criteria still unmet after {rounds} round(s): "
                        + "; ".join(f"{v.criterion} — {v.reason}" for v in assessment.unmet[:3])
                        + ("; …" if len(assessment.unmet) > 3 else ""),
                        severity=Severity.WARNING,
                    )
                )
                break

            # -- correct -------------------------------------------------------------------
            try:
                correction = await run_phase(
                    session,
                    system,
                    with_directive(base, _correction_directive(assessment.unmet)),
                    package,
                    prefix="implement",
                    # The phase that WROTE the code corrects it: same job, same route, so a
                    # repository routing `implement/green` reaches both.
                    aspect="green",
                    schema=IMPLEMENT_SCHEMA,
                    stalled_report=lambda staged: ImplementReport(changeset=staged, strategy=self.id),
                )
            except PhaseError as e:
                # A ceiling reached mid-correction keeps what the earlier rounds produced: the
                # change is real and its tests pass, and the reason it stopped improving is the
                # control working rather than a broken change.
                findings.append(
                    Finding(
                        id="assess.correction_stopped",
                        message=f"a correcting round stopped: {e.outcome.reason or 'no reason given'}",
                        severity=Severity.WARNING,
                    )
                )
                break
            cost = cost + correction.cost
            full = session.workspace.changeset(summary=staged_summary, ticket=ticket.key, notes=notes)

            # The repository's own checks and then the suite, over what the correction staged.
            # Both, and in that order, for the reason the first pass states: a validator that fixes
            # rewrites the bytes, so confirming green first would prove it of code that no longer
            # travels.
            validation = await validated(
                ctx, session, full, system=system, messages=base, package=package, prefix="implement"
            )
            full = session.workspace.changeset(summary=staged_summary, ticket=ticket.key, notes=notes)
            cost = cost + sum((i.cost for i in validation.invocations), Cost())
            async with prepared(ctx, session.repo_root, full, for_verb=Test) as (tree, _note):
                again = await ctx.do(Test(root=tree, expect="pass", runner=staged_runner(ctx, Test)))
            if again.status is not Status.SUCCEEDED or not again.decided:
                # The correction broke what the green phase proved. Stop and say so: the earlier
                # change was green and this one is not, and continuing would assess code whose
                # tests fail.
                findings.append(
                    Finding(
                        id="assess.correction_broke_the_suite",
                        message="a correcting round left the suite red, so the assessment stopped "
                        "there; the change is returned with its tests failing rather than "
                        "corrected further.",
                        severity=Severity.ERROR,
                    )
                )
                report = replace(report, changeset=full, validation=validation.report)
                return assessment, findings, full, report, cost
            report = replace(report, changeset=full, validation=validation.report)

        return assessment, findings, full, replace(report, assessment=assessment), cost


def _correction_directive(unmet: tuple[Any, ...]) -> str:
    """What the correcting round is told: which criteria are unmet, and WHY the assessor said so.

    The reason travels with the criterion, and that is the whole design. `attempts.py` makes the
    same argument for the cross-run case: *handed only its own diff, with no reason attached, a
    model defends it.* A criterion restated on its own invites exactly that -- the session already
    concluded it was met, and asking again without the objection gets the same conclusion back with
    more confidence. Handed the objection, it has something to fix.
    """
    lines = [
        "Step 3 of 3 — the acceptance criteria.",
        "",
        "Your tests pass. A second reader, which did not write this change, assessed it against "
        "the ticket's acceptance criteria and found these unmet:",
        "",
    ]
    for verdict in unmet:
        lines.append(f"- {verdict.criterion}")
        if verdict.reason:
            lines.append(f"  Why not: {verdict.reason}")
        for quote in verdict.evidence[:3]:
            lines.append(f"  Looked at: {quote}")
    lines += [
        "",
        "Change the code so those criteria are met. Do not argue that they already are -- if the "
        "assessor is wrong about what the change does, the change is not saying what it does "
        "clearly enough, and that is also something to fix. Keep the existing tests passing, and "
        "add a test for any behaviour you add.",
    ]
    return "\n".join(lines)


async def _assess(ctx: Any, inp: Any, full: ChangeSet, round_number: int) -> Any:
    """Ask the bound assessor whether this change meets the ticket's criteria.

    Rendered from the change set rather than a git diff: the change is staged and not on disk, so
    there is nothing to diff against yet -- the same reason `attempts.py` renders files rather than
    a patch.
    """
    from ...core.types import Assess
    from ...core.verbs import Verb  # noqa: F401  (documents the routed verb this dispatches)
    from .assess import MAX_CHANGE_CHARS
    from .attempts import file_blocks

    ticket = inp.ticket
    return await ctx.do(
        Assess(
            criteria=tuple(ticket.acceptance_criteria),
            change=file_blocks(full, MAX_CHANGE_CHARS),
            ticket=ticket.key,
            title=ticket.title,
            round_number=round_number,
        )
    )


async def _revert_verify(
    ctx: Any, session: ImplementSession, tests: ChangeSet, full: ChangeSet
) -> list[Finding]:
    """Undo the implementation and confirm the suite goes red again — proof the fix carries its test.

    The implementation is the part of `full` that is not one of the test files staged in phase 1.
    Its inverse (read from HEAD) applied over `full` leaves HEAD-plus-the-test, which should be red;
    if it is still green the test does not depend on the change and may have been weakened.
    """
    test_paths = set(tests.paths())
    fix = tuple(c for c in full.changes if c.path not in test_paths)
    if not fix:
        return []  # green reached with no implementation change — nothing to revert
    before = await head_state(session.repo_root, [c.path for c in fix])
    undo = ChangeSet(changes=fix).inverse(before)
    reverted = _merge(full, undo)  # full with the implementation undone -> HEAD + the test
    async with prepared(ctx, session.repo_root, reverted, for_verb=Test) as (tree, _note):
        recheck = await ctx.do(Test(root=tree, expect="fail", runner=staged_runner(ctx, Test)))
    if recheck.status is Status.SUCCEEDED:  # expect="fail" satisfied -> red again
        return [
            Finding(
                id="tdd.fix_verified",
                message="reverting the implementation returns the suite to red, so the change is "
                "load-bearing for its test.",
                severity=Severity.NOTE,
            )
        ]
    return [
        Finding(
            id="tdd.fix_not_load_bearing",
            message="the suite still passed with the implementation reverted, so the staged test "
            "does not depend on the change — the test may have been weakened. Review the test.",
            severity=Severity.WARNING,
        )
    ]


def _merge(base: ChangeSet, over: ChangeSet) -> ChangeSet:
    """`base` with `over` layered on top, last-write-wins per path — the same rule the Workspace and
    `apply` use, so a path appearing in both resolves to `over` rather than to two entries."""
    by_path = {c.path: c for c in base.changes}
    for change in over.changes:
        by_path[change.path] = change
    return ChangeSet(changes=tuple(by_path.values()), summary=base.summary, ticket=base.ticket)


def _blocked(reason: str, message: str, *, staged: list[Any] | None = None) -> Outcome[ImplementReport]:
    return Outcome(
        status=Status.BLOCKED,
        reason=reason,
        value=ImplementReport(changeset=ChangeSet(changes=tuple(staged or ()))),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
