"""`implement/oneshot` — one session, tools, and whatever the model can work out from the repo.

The premise is that most tickets do not need a pipeline. Read the ticket, look at the code, write
the change. The compiler-era arrangement ran that as four chained agents — requirements, plan,
tests, change — each starting from nothing but the previous one's prose, which is a lot of
machinery for "add the missing null check". This is the other end of that scale, and the one the
others should have to beat: `implement/direct` is described in the registry as "the baseline the
others are measured against", and until now no such baseline existed to measure against.

What makes it a *strategy* rather than a prompt is what it does with the answer. Three decisions:

**Files come from the tool boundary, never from the reply.** The model stages writes with
`write_file`, so every one of them passes the path guard individually, in the turn that asked, and
a refusal comes back as something it can act on. A schema carrying file contents would route the
whole change around that check and deliver the guard's answer after the fact.

**A reply that is not JSON does not throw the work away.** The change is already staged and has
already been checked. Losing it because the cover note came back as prose would be an expensive
way to enforce a formatting rule, so the prose becomes the summary and a finding says so.

**Staging nothing is an answer, not an absence.** A session that explored and wrote nothing has
failed to do what it was asked, and `FAILED` is what a workflow can branch on — where `SUCCEEDED`
with an empty change set is a green run that shipped nothing.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ...ai.structured import schema_instruction as _schema_instruction
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.types import ChangeSet
from ...core.verbs import Verb
from ...prompts.implement import IMPLEMENT_SCHEMA, ImplementParams
from ._strategy import PhaseError, read_reply, reported, run_phase
from .implement import ImplementReport, ImplementSession, ImplementSpec


class OneshotImplement:
    """Registered as `implement/oneshot`. Holds no run state; everything arrives in the session."""

    id: ClassVar[str] = "implement/oneshot"
    verb: ClassVar[Verb] = Verb.IMPLEMENT

    async def execute(
        self, ctx: Any, session: ImplementSession, inp: ImplementSpec
    ) -> Outcome[ImplementReport]:
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
        messages = prompt.render(
            ImplementParams(
                ticket=ticket.key,
                title=ticket.title,
                criteria=tuple(ticket.acceptance_criteria),
            ),
            package,
        )

        # A refused control (egress is the one most meet, since EXECUTES_CODE makes it mandatory),
        # an infrastructure failure, or a truncated answer all come back as `PhaseError` carrying
        # the Outcome to return — the mapping every strategy shared, now in one place.
        try:
            invocation = await run_phase(session, system, messages, package, prefix="implement")
        except PhaseError as e:
            return e.outcome

        summary, notes, unfinished, malformed = read_reply(invocation.content)

        changeset = session.workspace.changeset(summary=summary, ticket=ticket.key)
        # The whole change set, checked as a unit. The per-file check already ran at the tool
        # boundary, and this is not a repeat of it: `check_test_shape` is a rule about the shape
        # of a change rather than a path, so it is not expressible one file at a time. It passes
        # trivially while a ticket is set — which is the honest reading, because implementing from
        # a ticket is precisely the case where silencing a test is something a person signed for.
        refusals = session.guard.check(changeset, workflow_id=session.workspace.workflow_id)
        if refusals:
            return Outcome(
                status=Status.BLOCKED,
                reason="guard.refused",
                cost=invocation.cost,
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
            changeset=changeset,
            summary=summary,
            notes=notes,
            unfinished=unfinished,
            strategy=self.id,
            turns=invocation.turn_count,
        )

        findings = reported(
            report.changeset,
            unfinished=report.unfinished,
            malformed=malformed,
            invocations=(invocation,),
            prefix="implement",
        )

        if report.empty:
            # The domain answering no. A workflow can branch on it — escalate, re-run with a
            # different strategy, ask a person — where a SUCCEEDED with nothing in it is a green
            # run that shipped nothing and reads as one that worked.
            #
            # Exhaustion outranks emptiness as the reason, because it explains it: a session cut
            # off at the turn cap staged nothing *because it was stopped*, and grouping that with
            # "looked at the ticket and concluded no change was needed" in the ledger would file
            # a turn ceiling that is too low under a heading nobody would look for it.
            return Outcome(
                status=Status.FAILED,
                reason="exhausted" if invocation.exhausted else "implement.no_changes",
                value=report,
                cost=invocation.cost,
                findings=tuple(findings),
                decided=not invocation.exhausted,
            )

        return Outcome(
            status=Status.SUCCEEDED,
            value=report,
            cost=invocation.cost,
            findings=tuple(findings),
            # Exhaustion is not success and not failure: the turn cap stopped a session that had
            # not said it was done, so the staged change is whatever it had got to. `decided` is
            # the field that says so without inventing a seventh status.
            decided=not invocation.exhausted,
            reason="exhausted" if invocation.exhausted else None,
        )


def _blocked(reason: str, message: str, *, staged: list[Any] | None = None) -> Outcome[ImplementReport]:
    """A refusal, carrying whatever had been staged before it so the run is not a black box."""
    return Outcome(
        status=Status.BLOCKED,
        reason=reason,
        value=ImplementReport(changeset=ChangeSet(changes=tuple(staged or ()))),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
