"""The `/fix` process: reproduce the bug, fix it, stage it, say what happened.

Shipped by the framework rather than copied into an adopter's module, which is what `init` used to
do. The copy was the defect: every fix had to be made twice — once in the scaffold string literal,
once in this repository's own module — and could never reach a repository that had already run
`init`. Eleven commits in this repository edited both halves; #253's ticket-comment bug is frozen
forever in any tree scaffolded before it.

Nothing here registers on import. `@workflow` raises `DuplicateWorkflow` on a repeated id, so a
module still carrying its own ejected copy would fail to load ENTIRELY the moment the framework
claimed the same ids — an upgrade that breaks every existing adopter. Registration is a call
somebody makes:

    from in_lockstep.workflows import fix

    fix.register()

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

from ..adapters.ai import Fix
from ..adapters.worktree import verdict_over_staged
from ..core.context import RunContext
from ..core.outcome import Outcome, Status
from ..core.workflow import workflow
from ..platform.artifacts import (
    ATTEMPT,
    FIX_CHANGESET,
    read_changeset,
    read_verdict,
    write_changeset,
)
from ..platform.conversation import ticket_for, with_review
from ..platform.propose import escalate, open_reviewable
from ..platform.report import fix_body
from ..platform.scm import Scm
from ..platform.tickets import TicketSource
from ._shared import last_unsuccessful


async def fix_from_ticket(ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm) -> Outcome[Any]:
    """Read the bug and the review of the last attempt, reproduce it, fix it, leave it staged.

    Writes nothing to the tree. A fix that did not go green stages nothing — a broken fix must not
    travel — and the propose half says so on the ticket rather than opening a pull request.

    `with_review` gathers what people said on the open pull request this workflow opened last time,
    so replying to a reviewer is running the verb again rather than explaining yourself twice.
    """
    key, where = await ticket_for(ticket, scm)
    print(where)
    source, note = await with_review(await tickets.get(key), scm)
    print(note)
    outcome = await ctx.do(Fix(ticket=source))

    report = outcome.value
    # The same evidence path as the implementing verb: a failed fix still wrote a reproducer, and
    # sometimes an attempt at the fix, and both are worth more than a bill. `propose` reads
    # `FIX_CHANGESET` and nothing else, so its "no changeset means escalate" hinge is untouched.
    if outcome.status is not Status.SUCCEEDED and report is not None and not report.empty:
        written = write_changeset(ATTEMPT, report.changeset)
        print(f"attempt   {len(report.changeset.changes)} change(s) -> {written}  (not proposed)")
    if outcome.status is Status.SUCCEEDED and report is not None and not report.empty:
        # `report.changeset` is the reproducer and the fix merged. They are kept apart inside the
        # report so a reader can see which is which; what gets applied is both.
        #
        # Then the whole suite, against a throwaway worktree of HEAD plus that change. The
        # strategy has already proved the reproducer goes red and then green — but that is a fact
        # about the bug, not about the rest of the repository, and the two can disagree. The first
        # fix this loop ever produced passed its own reproducer and broke a test elsewhere; it was
        # proposed as ready for review on the strength of the half that passed.
        verdict = await verdict_over_staged(ctx, ctx.repo.root, report.changeset)
        written = write_changeset(FIX_CHANGESET, report.changeset, verdict=verdict)
        print(f"staged    reproducer + fix -> {written}")
    return outcome


async def fix_propose(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, artifact: str = FIX_CHANGESET
) -> Outcome[Any]:
    """Open the verified fix from the staged artifact, and say on the ticket what happened.

    Runs in the job that holds a write token and no provider credential. What it reads came from
    another job, so none of it is trusted: `Scm.open_change` runs `ChangeGuard` over the set before
    it writes a byte, and refuses any branch outside the run-scoped prefix.
    """
    # The same resolution the unprivileged half did, run again rather than threaded between
    # jobs: both halves are handed the number the comment was left on, and a fact both can
    # derive is not one to carry across an artifact boundary where it would arrive untrusted.
    ticket, where = await ticket_for(ticket, scm)
    print(where)
    changeset = read_changeset(artifact)
    verdict = read_verdict(artifact)

    if not changeset.changes:
        # An empty artifact means the fix failed: `fix/from-ticket` stages only when its reproducer
        # went red and then green. Open the next `ai-generated` ticket for another attempt rather
        # than leaving the bug with nothing said — bounded by the same cap implement escalates on.
        failure = "The automated fix did not reproduce the bug and turn it green."
        opened = await escalate(tickets, await tickets.get(ticket), failure, max_attempts=ctx.max_attempts)
        reason = "fix.not_fixed" if opened is not None else "fix.attempts_exhausted"
        if opened is not None:
            print(f"escalated {opened.key}")
        return Outcome(status=Status.FAILED, reason=reason, value=opened)

    if verdict is not None and verdict.red:
        # A fix that made its own reproducer pass and broke something else is still a failure, and
        # it used to be the one failure this verb could not see: it opened ready for review on the
        # strength of the reproducer alone. Same escalation implement makes, for the same reason —
        # the suite ran and disagreed, so another attempt is the honest next move.
        failure = (
            f"The fix passed its reproducer but the suite went red: "
            f"{verdict.failed} of {verdict.total} failed."
        )
        opened = await escalate(tickets, await tickets.get(ticket), failure, max_attempts=ctx.max_attempts)
        reason = "fix.suite_red" if opened is not None else "fix.attempts_exhausted"
        if opened is not None:
            print(f"escalated {opened.key}")
        return Outcome(status=Status.FAILED, reason=reason, value=opened)

    # Ready only when the whole suite agrees with the reproducer. Without a verdict — no Test verb
    # bound, or a runner that never started — this opens a draft: the reproducer passing is a fact
    # about the bug, and nobody has checked the rest of the repository.
    ready = verdict is not None and verdict.green
    change = await open_reviewable(
        scm,
        changeset,
        ready=ready,
        title=changeset.summary or f"Fix {ticket}",
        body=fix_body(changeset, verdict),
        ticket=ticket,
        workflow="fix",
        run_id=ctx.run_id,
    )
    # Fetched at the call, the way `implement/propose`'s empty-changeset branch does. `comment`
    # takes a `Ticket`, and the name that used to be here was never bound in this function — so
    # every successful fix opened its pull request and then died with a NameError before saying so
    # on the ticket, recording the run as errored (#196).
    await tickets.comment(
        await tickets.get(ticket),
        f"`/fix` opened {change.url or change.branch} as "
        f"{'ready for review' if ready else 'a draft — the suite has not confirmed it'}. "
        "Nobody has read it yet.",
    )
    print(f"change    {change.url or change.branch}")
    return Outcome(status=Status.SUCCEEDED, value=change)


async def fix_report(ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm) -> Outcome[None]:
    """Say on the ticket that the run failed, when the half that would have said so never ran.

    `fix/propose` answers on every outcome it sees — a change opened, no change staged, tests
    failed. It only sees the outcomes that reach it, and a strategy refusing in its first phase
    never gets there: the work job exits non-zero, `needs:` skips propose, and the person who typed
    `/fix` is left watching a thread that never replies.

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
    record = last_unsuccessful(ctx, key, "fix/")

    if record is None:
        body = (
            "`/fix`: no run for this ticket reached the ledger. Nothing was staged and "
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
            f"`/fix` did not produce a change — the run {what}.{spent} "
            f"Nothing was staged and no pull request was opened.{detail}"
        )

    await tickets.comment(source, body)
    print(f"commented {key}")
    # SUCCEEDED: this job's job was to say what happened, and it did. Failing here would put a
    # second red mark on a run whose failure is already recorded, and hide whether the answer
    # actually reached the ticket.
    return Outcome(status=Status.SUCCEEDED, reason=None)


def register() -> None:
    """Claim the `fix/*` workflow ids for this module's implementations.

    Called by an adopter's `lockstep.py`, never on import. Raises `DuplicateWorkflow` if the
    module already defines one of these ids itself, which is the correct and informative failure:
    a repository that ejected the source and then also called this asked for two different things
    under one name.
    """
    workflow(id="fix/from-ticket")(fix_from_ticket)
    workflow(id="fix/propose")(fix_propose)
    workflow(id="fix/report")(fix_report)
