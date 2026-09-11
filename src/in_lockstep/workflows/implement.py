"""The `/implement` process: read a ticket, write the change, stage it, say what happened.

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
from ..core.ports import Unsupported
from ..core.workflow import workflow
from ..platform.artifacts import (
    ATTEMPT,
    CHANGESET,
    read_assessment,
    read_changeset,
    read_description,
    read_validation,
    read_verdict,
    write_changeset,
)
from ..platform.conversation import with_review
from ..platform.propose import escalate, open_reviewable, update_reviewable
from ..platform.report import implement_body
from ..platform.scm import Scm, TargetRefused
from ..platform.tickets import TicketSource
from ._shared import described, last_unsuccessful, pointed_at


async def implement_from_ticket(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, actor: str = "", target: str = ""
) -> Outcome[Any]:
    """Read the ticket and the review of the last attempt, implement it, leave it staged.

    `tickets` and `scm` arrive from the bindings above — the signature names the ports, the
    dispatcher fills them. Writes nothing. The change set travels to the job that holds a write
    token, and crosses the guard again when it gets there.

    `target` names the repository the run is FOR, when that is not the checkout's own -- a fork
    proposing to what it forked from. Empty is this repository, which is every run before #373.

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
        # Written HERE, in the job that has a provider credential, and carried in the artifact:
        # the job that opens the change holds a write token and no provider key, so a body it
        # composed would be composed from the run's own cover note -- which is a note addressed to
        # the framework at the end of a session, and is what #389 published (#398).
        description = await described(ctx, source, report.changeset, verdict)
        # What the repository's own checks said travels with the change, because the job that
        # decides whether to ask for review holds a write token and no provider credential: it
        # cannot re-run anything, so an answer that did not travel is one it has to do without.
        written = write_changeset(
            CHANGESET,
            report.changeset,
            verdict=verdict,
            validation=report.validation,
            # What the second reader said about the ticket's criteria. It travels for the reason
            # the verdict and the validation do: the propose job holds a write token and no
            # provider credential, so it cannot re-run an assessment, and the decision to ask a
            # person for review is made from what came with the change or not at all.
            assessment=report.assessment,
            description=description,
        )
        print(f"staged    {len(report.changeset.changes)} change(s) -> {written}")
    return outcome


async def implement_propose(
    ctx: RunContext,
    ticket: str,
    tickets: TicketSource,
    scm: Scm,
    artifact: str = CHANGESET,
    target: str = "",
) -> Outcome[Any]:
    """Open a change from a staged artifact, and say on the ticket what happened.

    Runs in the job that holds a write token and no provider credential. Everything it reads came
    from another job, so none of it is trusted: `Scm.open_change` runs `ChangeGuard` over the set
    before it writes a byte, and refuses any branch outside the run-scoped prefix.
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
    assessment = read_assessment(artifact)

    if not changeset.changes:
        # Still a comment. A trigger that answers only on success leaves somebody watching a
        # thread that never got a reply, and "it found nothing to change" is an answer.
        await tickets.comment(await tickets.get(ticket), "`/implement` staged no change.")
        return Outcome(status=Status.FAILED, reason="implement.no_changes")

    if verdict is not None and verdict.only_elsewhere:
        # Red, and every failure is in a file this change did not touch. Escalating here files a
        # bug report about somebody else's failure and spends an attempt on it; discarding the
        # change destroys work that is complete. So it travels, as a DRAFT -- `ready` below is
        # false on any red verdict -- with the count in the body, and a person decides whether the
        # suite was already broken or this environment broke it (#405).
        print(f"suite     {verdict.failed} failure(s), none in the files this change staged")
    elif verdict is not None and verdict.red:
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
    # Green AND clean. A change whose lint failed is precisely a change that should not be asking
    # for a person's time, and the repository already said what it wants to be judged by. An
    # unchecked change (`validation is None`: nothing bound, or a validator that could not report)
    # is not blocked by this -- absent is not failing, the same reading a missing verdict gets.
    # And assessed, where anything assessed it. A change whose own second reader said it does not
    # do what the ticket asked is the clearest possible case for not putting it in somebody's
    # review queue as finished -- and it travels as a draft rather than being destroyed, because
    # four criteria of five is work (#452). Absent is not met, the third time this file makes that
    # reading: nothing bound, no criteria on the ticket, or an assessor that could not report all
    # arrive as None and none of them is a pass.
    ready = (
        verdict is not None
        and verdict.green
        and (validation is None or validation.clean)
        and (assessment is None or assessment.met)
    )
    # Fetched before the change is opened, because the title comes from it now.
    issue = await tickets.get(ticket)

    # --- Check for an existing change request opened by a prior run on the same ticket. ---
    # When one exists, a second `/implement` updates it rather than opening a second pull
    # request — same URL, same review threads, same number. The changeset is still built over
    # HEAD (the ordering property in `prepared` is not weakened; see #422), and force-pushed
    # to the existing branch.
    existing_cr = await _existing_change_for(ticket, scm)

    if existing_cr is not None:
        # A branch carrying a person's commit is refused before anything is written. Every
        # framework commit carries an `In-Lockstep-Run` trailer; a commit without one is a
        # person's, and overwriting it silently is the failure that would end trust fastest.
        person_commits = _person_commits_on(existing_cr, scm)
        if person_commits:
            shas = ", ".join(f"`{c.sha[:7]}`" for c in person_commits)
            subjects = "; ".join(f"`{c.sha[:7]}` {c.subject}" for c in person_commits)
            await tickets.comment(
                issue,
                f"`/implement` found commits on {existing_cr.url or existing_cr.branch} that "
                f"were not written by the framework: {subjects}. Overwriting them is refused.\n\n"
                f"To retry: close the pull request (or drop the commits {shas}), then ask again.",
            )
            print(f"refused   person commits on {existing_cr.branch}: {shas}")
            return Outcome(status=Status.FAILED, reason="implement.person_commits_on_branch")
        try:
            change = await update_reviewable(
                scm,
                existing_cr,
                changeset,
                ready=ready,
                title=issue.title or changeset.summary or f"Implement {ticket}",
                body=implement_body(changeset, verdict, validation, read_description(artifact)),
                ticket=ticket,
                workflow="implement",
                run_id=ctx.run_id,
            )
        except TargetRefused as e:
            await tickets.comment(
                issue,
                f"`/implement` staged a change but could not update {existing_cr.url or existing_cr.branch}"
                f": {e}\n\nThe change is in this run's artifact.",
            )
            print(f"refused   {e}")
            return Outcome(status=Status.FAILED, reason=e.reason)
        await tickets.comment(
            issue,
            f"`/implement` updated {change.url or change.branch} as "
            f"{'ready for review' if ready else 'a draft — its tests have not passed'}. "
            "Nobody has read it yet.",
        )
        print(f"updated   {change.url or change.branch}")
        return Outcome(status=Status.SUCCEEDED, value=change)

    # --- No existing CR: open a new one, the original path. ---
    try:
        change = await open_reviewable(
            scm,
            changeset,
            ready=ready,
            # The ticket's own title, not the model's `summary`. A summary is free prose: run
            # 33578430422 put a thousand characters of the model's running commentary here, and
            # the host refused the pull request after the work was done and green. The issue title
            # is a person's one-line statement of the same thing, which is what a title wants.
            title=issue.title or changeset.summary or f"Implement {ticket}",
            body=implement_body(changeset, verdict, validation, read_description(artifact)),
            ticket=ticket,
            workflow="implement",
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
            f"`/implement` staged a change but could not open it on `{target}`: {e}\n\n"
            f"The change is in this run's artifact. Someone with write access there can open it "
            f"with `in-lockstep apply --from-artifact <the artifact> --target {target}`.",
        )
        print(f"refused   {e}")
        return Outcome(status=Status.FAILED, reason=e.reason)
    await tickets.comment(
        issue,
        f"`/implement` opened {change.url or change.branch} as "
        f"{'ready for review' if ready else 'a draft — its tests have not passed'}. "
        "Nobody has read it yet.",
    )
    print(f"change    {change.url or change.branch}")
    return Outcome(status=Status.SUCCEEDED, value=change)


async def implement_report(
    ctx: RunContext, ticket: str, tickets: TicketSource, scm: Scm, target: str = ""
) -> Outcome[None]:
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
        body = f"`/implement` did not open a pull request — the run {what}.{spent} {outcome}{detail}"

    await tickets.comment(source, body)
    print(f"commented {key}")
    # SUCCEEDED: this job's job was to say what happened, and it did. Failing here would put a
    # second red mark on a run whose failure is already recorded, and hide whether the answer
    # actually reached the ticket.
    return Outcome(status=Status.SUCCEEDED, reason=None)


# ---------------------------------------------------------------------------
# Helpers for the update-existing-CR path (#443).
# ---------------------------------------------------------------------------


async def _existing_change_for(ticket: str, scm: Any) -> Any:
    """The newest open change request the framework opened for `ticket`, or None.

    Accessed via `hasattr` because `changes_for` is a host capability — plain `GitLocal` cannot
    list pull requests. When the host does not support it, the answer is None and the flow falls
    through to `open_change`, which is what happened before this path existed.
    """
    if not hasattr(scm, "changes_for"):
        return None
    try:
        changes = await scm.changes_for(ticket)
    except (RuntimeError, OSError):
        # A host that errored listing changes is not a reason to open a second PR — but neither
        # is it a reason to refuse. Fall through to the new-PR path, the same thing that would
        # happen if the host had no `changes_for` at all.
        return None
    if not changes:
        return None
    # Newest first, which is what `changes_for` returns.
    return changes[0]


def _person_commits_on(change: Any, scm: Any) -> list[Any]:
    """Commits on `change`'s branch that lack an `In-Lockstep-Run` trailer — a person's work.

    Detection uses machinery that exists: every framework commit carries the trailer, and
    `commits_between` already parses trailers off a range. A commit without the trailer is a
    person's. No heuristic, no author-name matching.

    Returns an empty list when the host cannot enumerate commits — the safe side is to proceed,
    because the alternative is refusing every update on a host that does not expose commit
    metadata.
    """
    if not hasattr(scm, "commits_between"):
        return []
    try:
        commits = scm.commits_between("HEAD", change.branch)
    except (RuntimeError, OSError):
        return []
    return [c for c in commits if "In-Lockstep-Run" not in (c.trailers or {})]


def register() -> None:
    """Claim the `implement/*` workflow ids for this module's implementations.

    Called by an adopter's `lockstep.py`, never on import. Raises `DuplicateWorkflow` if the
    module already defines one of these ids itself, which is the correct and informative failure:
    a repository carrying its own copy of one of these and also calling this asked for two different
    things under one name.
    """
    workflow(id="implement/from-ticket")(implement_from_ticket)
    workflow(id="implement/propose")(implement_propose)
    workflow(id="implement/report")(implement_report)
