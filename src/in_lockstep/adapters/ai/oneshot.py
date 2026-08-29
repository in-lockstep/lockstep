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

from ...ai.invoker import InvocationBlocked, InvocationFailed
from ...ai.structured import SchemaError, parse
from ...ai.structured import schema_instruction as _schema_instruction
from ...core.outcome import Finding, Outcome, Severity, Status
from ...core.types import ChangeSet
from ...core.verbs import Verb
from ...privileged.egress import EgressRefused
from ...prompts.implement import IMPLEMENT_SCHEMA, ImplementParams
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
            # Both are a control refusing, which is what BLOCKED means. Routed through an Outcome
            # rather than allowed to escape so the run still leaves a ledger record: a refusal for
            # a real reason deserves the same trace as a run that was allowed.
            #
            # The one most people will meet here is egress. This tool set declares EXECUTES_CODE,
            # which makes enforcement mandatory — so a laptop with an open network refuses until
            # the run is under something that constrains egress, or `UnsandboxedEgress` is bound
            # deliberately. That is the design working, and the message says which.
            return _blocked(e.reason, str(e), staged=session.workspace.changes)
        except InvocationFailed as e:
            # ERRORED, not BLOCKED: infrastructure broke. The message arrives already redacted.
            return Outcome(
                status=Status.ERRORED,
                reason=e.reason,
                findings=(Finding(id=e.reason, message=str(e), severity=Severity.ERROR, blocking=True),),
            )

        if invocation.truncated:
            # Diagnosed before parsing, because the parse failure it causes is a misdiagnosis: the
            # JSON is not malformed, it is unfinished. Worse here than in review — a `write_file`
            # cut off at the token cap is a truncated *file*, so the staged change is not merely
            # incomplete, it is corrupt, and it is deliberately not returned.
            return Outcome(
                status=Status.ERRORED,
                reason="implement.truncated",
                cost=invocation.cost,
                findings=(
                    Finding(
                        id="implement.truncated",
                        message=(
                            f"the model stopped at the {session.policy.max_tokens}-token output "
                            f"cap mid-answer. A write that was cut off is a truncated file, so "
                            f"nothing staged in this session is returned. Raise "
                            f"`InvokePolicy.max_tokens` and re-run."
                        ),
                        severity=Severity.ERROR,
                        blocking=True,
                    ),
                ),
            )

        summary, notes, unfinished, malformed = _read_reply(invocation.content)

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

        findings = list(_reported(report, malformed, invocation))

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


def _read_reply(content: str) -> tuple[str, tuple[str, ...], tuple[str, ...], bool]:
    """The cover note, leniently. Returns (summary, notes, unfinished, malformed)."""
    try:
        parsed = parse(content)
    except SchemaError:
        return content.strip()[:1000], (), (), True
    value = parsed.value
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


def _reported(report: ImplementReport, malformed: bool, invocation: Any) -> list[Finding]:
    """What travels with the outcome: the gaps, the scanner's hits, and the staged paths."""
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
        # A WARNING rather than a note: an unsatisfied requirement is the thing most likely to be
        # missed when a change otherwise looks finished.
        Finding(id="implement.unfinished", message=gap, severity=Severity.WARNING)
        for gap in report.unfinished
    ]
    if malformed:
        findings.append(
            Finding(
                id="implement.unstructured",
                message=(
                    "the final message was not the JSON the schema asked for; its text was kept "
                    "as the summary. The staged change is unaffected — it came through the tool "
                    "boundary, not through this reply."
                ),
                severity=Severity.WARNING,
            )
        )
    # Anything the injection scanner saw in the ticket or in a tool result travels with the
    # outcome: a ticket that tried to talk to the implementer is a fact about the ticket.
    findings += [
        Finding(
            id=f"injection.{f.name}",
            message=f"{f.severity}: {f.excerpt}",
            severity=Severity.ERROR if f.severity == "critical" else Severity.WARNING,
        )
        for f in invocation.findings
    ]
    return findings


def _blocked(reason: str, message: str, *, staged: list[Any] | None = None) -> Outcome[ImplementReport]:
    """A refusal, carrying whatever had been staged before it so the run is not a black box."""
    return Outcome(
        status=Status.BLOCKED,
        reason=reason,
        value=ImplementReport(changeset=ChangeSet(changes=tuple(staged or ()))),
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )
