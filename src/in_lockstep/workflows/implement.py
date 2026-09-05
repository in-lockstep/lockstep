"""The `/implement` process: read a ticket, write the change, stage it, say what happened.

Shipped by the framework rather than copied into an adopter's module, which is what `init` used to
do. The copy was the defect: every fix had to be made twice — once in the scaffold string literal,
once in this repository's own module — and could never reach a repository that had already run
`init`. Eleven commits in this repository edited both halves; #253's ticket-comment bug is frozen
forever in any tree scaffolded before it.

Nothing here registers on import. `@workflow` raises `DuplicateWorkflow` on a repeated id, so a
module still carrying its own ejected copy would fail to load ENTIRELY the moment the framework
claimed the same ids — an upgrade that breaks every existing adopter. Registration is a call
somebody makes:

    from in_lockstep.workflows import implement

    implement.register()

which is also the shape this project asks for elsewhere: a workflow that appeared because of an
import is the hidden configuration surface `CLAUDE.md` refuses in prompt headers for the same
reason.

The bodies reach the RUN CONTEXT and never a module-level `lockstep` — `ctx.container`,
`ctx.repo.root`, `ctx.max_attempts`. That is what makes them movable at all, and why
`max_attempts` is snapshotted onto `RunContext` beside `models`. What stays in the adopter's file
is the half that is genuinely theirs: which adapters are bound, which model is routed, what the
workshop grants, what the middleware chain is.
"""

from __future__ import annotations

from typing import Any

from ..adapters.ai import Implement
from ..adapters.worktree import verdict_over_staged
from ..core.context import RunContext
from ..core.outcome import Outcome, Status
from ..core.workflow import workflow
from ..platform.artifacts import ATTEMPT, CHANGESET, read_changeset, read_verdict, write_changeset
from ..platform.conversation import ticket_for, with_review
from ..platform.propose import escalate, open_reviewable
from ..platform.report import implement_body
from ..platform.scm import Scm
from ..platform.tickets import TicketSource
from ._shared import last_unsuccessful


async def implement_from_ticket(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, actor: str = ""
) -> Outcome[Any]:
    """Read the ticket and the review of the last attempt, implement it, leave it staged.

    `tickets` and `scm` arrive from the bindings above — the signature names the ports, the
    dispatcher fills them. Writes nothing. The change set travels to the job that holds a write
    token, and crosses the guard again when it gets there.

    `with_review` is what makes a second `/implement` a reply rather than a retry: it gathers what
    people said on the open pull request this workflow opened last time — including the notes
    pinned to a line, which are the most specific thing a reviewer ever writes — and hands them
    over on the ticket, untrusted like the ticket body.
    """
    # `--approved-by` in CLI terms: a named human asked for this specific run, and the actor gate
    # verified them before this job started. Recorded, because a grant nobody can be traced to is
    # not much of a grant.
    #
    # `via=tdd` says at the execution site what serves this request — the same adapter the module
    # binds above, named here so the reader of this line knows Implement means red-then-green
    # without scrolling to the binding.
    key, where = await ticket_for(ticket, scm)
    print(where)
    source, note = await with_review(await tickets.get(key), scm)
    print(note)
    outcome = await ctx.do(Implement(ticket=source))

    # `SUCCEEDED` and not merely "there are changes", which is what this used to check — and the
    # difference is a real run that cost $21 and would have opened a pull request containing a
    # test that tested nothing. A test-first strategy that refuses in its red phase still returns
    # the test it staged, so `changeset.changes` is truthy on precisely the outcome that must not
    # travel. The fixing verb's own half has always guarded on the status; this now matches it.
    report = outcome.value
    # A run that FAILED still wrote something worth keeping, and this used to throw it away.
    #
    # `tdd.not_green` returns the change deliberately — `test_implement_tdd.py` asserts it, in
    # those words: "the change is still carried so a person can see what it tried". Run
    # 33582850420 reached that state on #150 (13 failing tests of 1644, $13.84 spent, a
    # diagnosable near-miss) and the artifact came back holding nothing but a history bundle,
    # because the guard below stages only on SUCCEEDED. The strategy handed the work over and the
    # workflow dropped it.
    #
    # So it is written to a DIFFERENT path. `propose` reads `changeset/` and nothing else, so a
    # red change still cannot become a pull request — that rule is untouched, and it is enforced by
    # the path rather than by a condition somebody could relax later. `attempt/` is evidence: CI
    # uploads it, a person downloads it, and the next run starts from a diff instead of from
    # nothing.
    if outcome.status is not Status.SUCCEEDED and report is not None and report.changeset.changes:
        written = write_changeset(ATTEMPT, report.changeset)
        print(f"attempt   {len(report.changeset.changes)} change(s) -> {written}  (not proposed)")
    if outcome.status is Status.SUCCEEDED and report is not None and report.changeset.changes:
        # The suite, run against a throwaway worktree of HEAD plus the staged change, before any
        # of it travels. The verdict rides the artifact so the privileged half can decide what to
        # open — a reviewer should learn whether the change passed from the pull request, not by
        # waiting for CI on a branch a model wrote.
        verdict = await verdict_over_staged(ctx, ctx.repo.root, report.changeset)
        written = write_changeset(CHANGESET, report.changeset, verdict=verdict)
        print(f"staged    {len(report.changeset.changes)} change(s) -> {written}")
    return outcome


async def implement_propose(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, artifact: str = CHANGESET
) -> Outcome[Any]:
    """Open a change from a staged artifact, and say on the ticket what happened.

    Runs in the job that holds a write token and no provider credential. Everything it reads came
    from another job, so none of it is trusted: `Scm.open_change` runs `ChangeGuard` over the set
    before it writes a byte, and refuses any branch outside the run-scoped prefix.
    """
    # The same resolution the unprivileged half did, run again rather than threaded between
    # jobs: both halves are handed the number the comment was left on, and a fact both can
    # derive is not one to carry across an artifact boundary where it would arrive untrusted.
    ticket, where = await ticket_for(ticket, scm)
    print(where)
    changeset = read_changeset(artifact)
    verdict = read_verdict(artifact)

    if not changeset.changes:
        # Still a comment. A trigger that answers only on success leaves somebody watching a
        # thread that never got a reply, and "it found nothing to change" is an answer.
        await tickets.comment(await tickets.get(ticket), "`/implement` staged no change.")
        return Outcome(status=Status.FAILED, reason="implement.no_changes")

    if verdict is not None and verdict.red:
        # `red`, not `not green`: an errored suite — the runner never started — is not evidence
        # that this change is broken, and escalating on it files a bug report about code nobody
        # tested and then spends the loop's attempts on it. A change whose tests actually RAN and
        # failed does not become a pull request; it becomes the next `ai-generated` ticket, which
        # the label trigger routes to the fixing verb — and because `escalate` counts attempts off
        # the source ticket's labels, the loop stops at `ctx.max_attempts` without any store
        # to keep count in.
        failure = f"Tests failed: {verdict.failed} of {verdict.total} against the staged change."
        opened = await escalate(tickets, await tickets.get(ticket), failure, max_attempts=ctx.max_attempts)
        reason = "implement.tests_failed" if opened is not None else "implement.attempts_exhausted"
        if opened is not None:
            print(f"escalated {opened.key}")
        return Outcome(status=Status.FAILED, reason=reason, value=opened)

    # Draft unless the suite went green. An unverified change — no verdict at all, because nothing
    # was staged to run against or the Test verb refused — is not a failure, but it has not earned
    # a place in somebody's review queue either.
    ready = verdict is not None and verdict.green
    # Fetched before the change is opened, because the title comes from it now.
    issue = await tickets.get(ticket)
    change = await open_reviewable(
        scm,
        changeset,
        ready=ready,
        # The ticket's own title, not the model's `summary`. A summary is free prose: run
        # 33578430422 put a thousand characters of the model's running commentary here, and the
        # host refused the pull request after the work was done and green. The issue title is a
        # person's one-line statement of the same thing, which is what a title wants.
        title=issue.title or changeset.summary or f"Implement {ticket}",
        body=implement_body(changeset, verdict),
        ticket=ticket,
        workflow="implement",
        run_id=ctx.run_id,
    )
    await tickets.comment(
        issue,
        f"`/implement` opened {change.url or change.branch} as "
        f"{'ready for review' if ready else 'a draft — its tests have not passed'}. "
        "Nobody has read it yet.",
    )
    print(f"change    {change.url or change.branch}")
    return Outcome(status=Status.SUCCEEDED, value=change)


async def implement_report(ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm) -> Outcome[None]:
    """Say on the ticket that the run failed, when the half that would have said so never ran.

    `implement/propose` answers on every outcome it sees — a change opened, no change staged, tests
    failed. It only sees the outcomes that reach it, and a strategy refusing in its first phase
    never gets there: the work job exits non-zero, `needs:` skips propose, and the person who typed
    `/implement` is left watching a thread that never replies.

    Which is the one failure a chat-ops trigger cannot afford. The alternative to an answer is not
    "no answer" — it is somebody assuming it worked, because the last thing the tool said was that
    it had started.

    Reads the record the run already wrote rather than being handed a reason by the CI file: the
    reason, the cost and the findings are all in the ledger, and a workflow that took them as
    arguments would be a workflow whose YAML had to know what happened.
    """
    key, where = await ticket_for(ticket, scm)
    print(where)
    source = await tickets.get(key)
    record = last_unsuccessful(ctx, key, "implement/")

    if record is None:
        body = (
            "`/implement`: no run for this ticket reached the ledger. Nothing was staged and "
            "nothing was opened; the job log is the only account of it."
        )
    else:
        reason = record.get("reason")
        cost = record.get("cost_usd")
        spent = f" ${float(cost):.2f} spent." if isinstance(cost, (int, float)) else ""
        findings = [
            f"- `{f.get('id')}`: {f.get('message')}"
            for f in (record.get("findings") or {}).get("items", [])[:5]
            if isinstance(f, dict)
        ]
        detail = ("\n\n" + "\n".join(findings)) if findings else ""
        # Three verbs, because `Status` keeps three things apart on purpose and this sentence is
        # posted publicly on the ticket, where a wrong one is a false claim in the place a person
        # reads it. `blocked` is a control working -- a budget ceiling or an approval gate --
        # and calling it a failure teaches everyone the ceiling is a fault rather than a decision
        # somebody made. `errored` is infrastructure breaking, which is the class `Retry` targets
        # and not something the change under review did wrong.
        #
        # Written as a map rather than a two-way branch so the statuses this does NOT special-case
        # are visible instead of implied: `Status` has six members and a boolean covers two.
        #
        # And the reason is no longer defaulted to the status. `Outcome(status=Status.BLOCKED)`
        # with no reason is legal, and the old fallback rendered it "stopped by `blocked`" -- a
        # sentence that says nothing twice.
        told = {
            "blocked": ("was stopped by", "was stopped by a control"),
            "errored": ("could not run —", "could not run"),
        }
        with_reason, without = told.get(str(record.get("status") or ""), ("failed with", "failed"))
        what = f"{with_reason} `{reason}`" if reason else without
        body = (
            f"`/implement` did not produce a change — the run {what}.{spent} "
            f"Nothing was staged and no pull request was opened.{detail}"
        )

    await tickets.comment(source, body)
    print(f"commented {key}")
    # SUCCEEDED: this job's job was to say what happened, and it did. Failing here would put a
    # second red mark on a run whose failure is already recorded, and hide whether the answer
    # actually reached the ticket.
    return Outcome(status=Status.SUCCEEDED, reason=None)


def register() -> None:
    """Claim the `implement/*` workflow ids for this module's implementations.

    Called by an adopter's `lockstep.py`, never on import. Raises `DuplicateWorkflow` if the
    module already defines one of these ids itself, which is the correct and informative failure:
    a repository that ejected the source and then also called this asked for two different things
    under one name.
    """
    workflow(id="implement/from-ticket")(implement_from_ticket)
    workflow(id="implement/propose")(implement_propose)
    workflow(id="implement/report")(implement_report)
