"""The `/fix` process: reproduce the bug, fix it, stage it, say what happened.

Shipped by the framework rather than copied into an adopter's module, which is what `init` used to
do. The copy was the defect: every fix had to be made twice — once in the scaffold string literal,
once in this repository's own module — and could never reach a repository that had already run
`init`. Eleven commits in this repository edited both halves; #253's ticket-comment bug is frozen
forever in any tree scaffolded before it.

Nothing here registers on import. `@workflow` raises `DuplicateWorkflow` on a repeated id, so a
module carrying its own copy of these — hand-written, or ejected by a version that offered it —
    would fail to load ENTIRELY the moment the framework
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
from ..core.ports import Unsupported
from ..core.workflow import workflow
from ..platform.artifacts import (
    ATTEMPT,
    FIX_CHANGESET,
    read_changeset,
    read_description,
    read_validation,
    read_verdict,
    write_changeset,
)
from ..platform.conversation import with_review
from ..platform.propose import escalate, open_reviewable
from ..platform.report import fix_body
from ..platform.scm import Scm, TargetRefused
from ..platform.tickets import TicketSource
from ._shared import described, last_unsuccessful, pointed_at


async def fix_from_ticket(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, target: str = ""
) -> Outcome[Any]:
    """Read the bug and the review of the last attempt, reproduce it, fix it, leave it staged.

    Writes nothing to the tree. A fix that did not go green stages nothing — a broken fix must not
    travel — and the propose half says so on the ticket rather than opening a pull request.

    `target` names the repository the run is FOR, when that is not the checkout's own -- a fork
    proposing to what it forked from. Empty is this repository, which is every run before #373.

    `with_review` gathers what people said on the open pull request this workflow opened last time,
    so replying to a reviewer is running the verb again rather than explaining yourself twice.
    """
    try:
        tickets, scm, key, where = await pointed_at(target, ticket, tickets, scm)
    except Unsupported as e:
        # Refused before anything is read, and by name: a bound port that cannot be pointed at the
        # target would otherwise answer about this repository, which is the confusion the flag
        # exists to end.
        print(f"refused   {e}")
        return Outcome(status=Status.FAILED, reason="scm.target_unsupported")
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
        # As implement does, and for the same reason: the propose job cannot call a model.
        description = await described(ctx, source, report.changeset, verdict)
        # As implement does, and for the same reason: the propose job cannot re-run the checks.
        written = write_changeset(
            FIX_CHANGESET,
            report.changeset,
            verdict=verdict,
            validation=report.validation,
            description=description,
        )
        print(f"staged    reproducer + fix -> {written}")
    return outcome


async def fix_propose(
    ctx: RunContext,
    ticket: str,
    tickets: TicketSource,
    scm: Scm,
    artifact: str = FIX_CHANGESET,
    target: str = "",
) -> Outcome[Any]:
    """Open the verified fix from the staged artifact, and say on the ticket what happened.

    Runs in the job that holds a write token and no provider credential. What it reads came from
    another job, so none of it is trusted: `Scm.open_change` runs `ChangeGuard` over the set before
    it writes a byte, and refuses any branch outside the run-scoped prefix.
    """
    # The same resolution the unprivileged half did, run again rather than threaded between
    # jobs: both halves are handed the number the comment was left on, and a fact both can
    # derive is not one to carry across an artifact boundary where it would arrive untrusted.
    try:
        tickets, scm, ticket, where = await pointed_at(target, ticket, tickets, scm)
    except Unsupported as e:
        # Refused before anything is read, and by name: a bound port that cannot be pointed at the
        # target would otherwise answer about this repository, which is the confusion the flag
        # exists to end.
        print(f"refused   {e}")
        return Outcome(status=Status.FAILED, reason="scm.target_unsupported")
    print(where)
    changeset = read_changeset(artifact)
    verdict = read_verdict(artifact)
    validation = read_validation(artifact)

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

    if verdict is not None and verdict.only_elsewhere:
        # Red, and every failure is in a file this change did not touch. Escalating here files a
        # bug report about somebody else's failure and spends an attempt on it; discarding the
        # change destroys work that is complete. So it travels, as a DRAFT -- `ready` below is
        # false on any red verdict -- with the count in the body, and a person decides whether the
        # suite was already broken or this environment broke it (#405).
        print(f"suite     {verdict.failed} failure(s), none in the files this change staged")
    elif verdict is not None and verdict.red:
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
    # Green AND clean. A change whose lint failed is precisely a change that should not be asking
    # for a person's time, and the repository already said what it wants to be judged by. An
    # unchecked change (`validation is None`: nothing bound, or a validator that could not report)
    # is not blocked by this -- absent is not failing, the same reading a missing verdict gets.
    ready = verdict is not None and verdict.green and (validation is None or validation.clean)
    # Fetched before the change is opened, because the title comes from it now.
    issue = await tickets.get(ticket)
    try:
        change = await open_reviewable(
            scm,
            changeset,
            ready=ready,
            # The ticket's title, the way `implement/propose` does it and for the reason its
            # comment gives: a title wants a person's one line about the bug, and the model's
            # summary is prose of any length. `Fix #319` is what this read when the summary was
            # empty (#343) -- the fallback of a fallback, and the one a reader actually saw.
            title=issue.title or changeset.summary or f"Fix {ticket}",
            body=fix_body(changeset, verdict, validation, read_description(artifact)),
            ticket=ticket,
            workflow="fix",
            run_id=ctx.run_id,
            # Where the branch and the pull request are created, named here rather than inherited
            # from the narrowing above: the write is the act that must be unambiguous.
            target=target,
        )
    except TargetRefused as e:
        # Nothing was pushed -- the credential was checked first -- so the work is in the artifact
        # and a person with write access can finish it. Said on the ticket rather than only in a
        # job log, because the person who asked for this is reading the thread.
        await tickets.comment(
            issue,
            f"`/fix` staged a change but could not open it on `{target}`: {e}\n\n"
            f"The change is in this run's artifact. Someone with write access there can open it "
            f"with `in-lockstep apply --from-artifact <the artifact> --target {target}`.",
        )
        print(f"refused   {e}")
        return Outcome(status=Status.FAILED, reason=e.reason)
    # Fetched at the call, the way `implement/propose`'s empty-changeset branch does. `comment`
    # takes a `Ticket`, and the name that used to be here was never bound in this function — so
    # every successful fix opened its pull request and then died with a NameError before saying so
    # on the ticket, recording the run as errored (#196).
    await tickets.comment(
        issue,
        f"`/fix` opened {change.url or change.branch} as "
        f"{'ready for review' if ready else 'a draft — the suite has not confirmed it'}. "
        "Nobody has read it yet.",
    )
    print(f"change    {change.url or change.branch}")
    return Outcome(status=Status.SUCCEEDED, value=change)


async def fix_report(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, target: str = ""
) -> Outcome[None]:
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
    try:
        tickets, scm, key, where = await pointed_at(target, ticket, tickets, scm)
    except Unsupported as e:
        # Refused before anything is read, and by name: a bound port that cannot be pointed at the
        # target would otherwise answer about this repository, which is the confusion the flag
        # exists to end.
        print(f"refused   {e}")
        return Outcome(status=Status.FAILED, reason="scm.target_unsupported")
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
        # somebody made. `errored` is infrastructure breaking, which is the class transport retry targets
        # and not something the change under review did wrong.
        #
        # Written as a map rather than a two-way branch so the statuses this does NOT special-case
        # are visible instead of implied: `Status` has five members and a boolean covers two.
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
        # What was staged is counted from every finding, not the five shown: run 34129809277
        # listed two `fix.staged` lines under a sentence saying nothing was staged (#312). A
        # change that was staged and not proposed is in the run's artifact, and saying so is
        # the difference between "it did nothing" and "it did something a person can read".
        staged = sum(
            1
            for f in (record.get("findings") or {}).get("items", [])
            if isinstance(f, dict) and str(f.get("id", "")).endswith(".staged")
        )
        outcome = (
            f"{staged} change(s) were staged and not proposed -- the run's artifact holds them -- "
            f"and no pull request was opened."
            if staged
            else "Nothing was staged and no pull request was opened."
        )
        body = f"`/fix` did not open a pull request — the run {what}.{spent} {outcome}{detail}"

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
    a repository carrying its own copy of one of these and also calling this asked for two different
    things under one name.
    """
    workflow(id="fix/from-ticket")(fix_from_ticket)
    workflow(id="fix/propose")(fix_propose)
    workflow(id="fix/report")(fix_report)
