"""The learning loop: read the record, draft a change to one prompt body, measure it, propose it.

Two workflows, split the way `implement` is split and for the same reason. `improve/measure`
holds the provider credential: it reads the ledger for a qualifying trend, drafts a change to the
one body that trend is attributed to, re-asks every promoted case that body is evidence for, and
stages the change with its scorecard. `improve/propose` holds a write token and no credential: it
reads the staged change, enforces the open-proposal ceiling where the proposal is opened, and
opens it. They cannot be one function because they must not be one process.

Everything that decides what a run MEANS is a refusal before the first model call, and each is a
BLOCKED outcome with a reason a person can act on:

  * no finding clears the thresholds, or the ones that do answer to no declared body;
  * the body is writable by omission rather than by grant (`GATE-IMPROVE-3`);
  * no promoted case carries the body as it stands; or every one that does passes, in which case
    a draft could only stay level or fall, and nothing is spent to learn that.

What "better" means here is deliberately narrow and stated: a draft that passes a case the
current body fails, and fails none the current body passes. A rubric is put to the bound judge
on both arms, one ask each, sharing the run's budget and tape (`GATE-JUDGE-3`); a rubric the
judge did not answer, or one no judge is bound for, stays `outstanding` on both arms, and the
person on the pull request is the judge of last resort the loop declares.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..adapters.ai import Draft, Judge, Measure
from ..adapters.ai.strategy import blocked
from ..core.context import RunContext
from ..core.human import HumanBoundary, Resumption
from ..core.improve import IMPROVED, Improver, Scorecard
from ..core.outcome import Finding, Outcome, Severity, Status
from ..core.types import ChangeSet, FileChange
from ..core.workflow import workflow
from ..platform.artifacts import ATTEMPT, CHANGESET, read_changeset, read_scorecard, write_changeset
from ..platform.ledger import store_for
from ..platform.propose import open_reviewable
from ..platform.report import improve_body, scorecard_lines
from ..platform.scm import Scm
from ..platform.verdicts import append_verdicts

MEASURE = "improve/measure"
PROPOSE = "improve/propose"
AFTER_REVIEW = "improve/after-review"


async def improve_measure(ctx: RunContext, improver: Improver) -> Outcome[Any]:
    """Trend -> body -> grant -> baseline -> draft -> measure -> stage. Writes nothing but artifacts."""
    store: Any = store_for(ctx.container, ctx.repo.root)
    reader = getattr(store, "records", None)
    if reader is None:
        return blocked("improve.ledger_unreadable", f"{type(store).__name__} cannot list records")
    attribution = improver.attribute(reader(), ctx.improvable)
    if attribution.body is None:
        print(f"trend     —  {attribution.reason}")
        return blocked("improve.no_trend", attribution.reason)
    body = attribution.body
    print(
        f"trend     {attribution.finding}  ({attribution.runs} of {attribution.considered} run(s), "
        f"{attribution.billed_runs} billed, {attribution.weeks} week(s))"
    )
    print(f"body      {body.body}  ({body.label})")

    # GATE-IMPROVE-3, before a cent is spent. Two questions of the guard, because "nothing refuses
    # this path" and "something granted this path" are different facts and only the second is
    # permission. The grant is checked for the PROPOSING workflow, which is the one that writes.
    guard = ctx.guard
    if guard is None:
        return blocked(
            "improve.no_guard",
            "this run carries no guard, so nothing can say whether the body is writable by grant",
        )
    bare = guard.check_path(body.body)
    if bare is None:
        return blocked(
            "improve.permitted_by_omission",
            f"{body.body} is writable because no tier names it, not because anything granted it. In "
            f"lockstep.py, put its directory in `deny_unless_granted` and grant it to {PROPOSE!r}: "
            f"PathPolicy(deny_unless_granted=(*DENY_UNLESS_GRANTED, '<dir>/'), "
            f"grants=frozenset({{'<dir>/'}}), "
            f"granted_to_workflow={PROPOSE!r})",
        )
    granted = guard.check_path(body.body, workflow_id=PROPOSE)
    if granted is not None:
        return blocked(
            "improve.body_not_granted",
            f"{body.body} is tier {granted.tier} ({granted.rule}) and {PROPOSE!r} holds no grant for it; "
            f"a tier-1 path can never be granted, and a tier-2 one needs `grants` and `granted_to_workflow`",
        )
    print(f"guard     granted to {PROPOSE}  (tier {bare.tier}, {bare.rule})")

    current = (Path(ctx.repo.root) / body.body).read_text()
    baseline = improver.baseline(body, current)
    print(
        f"corpus    {baseline.arm.measured} of {baseline.corpus} case(s) carry this body as it stands"
        + (f"; {baseline.unattributable} do not" if baseline.unattributable else "")
    )
    if baseline.arm.measured == 0:
        return blocked(
            "improve.corpus_unattributable",
            f"no promoted case carries {body.label} as it stands, so nothing could measure a change to it; "
            f"promote a case recorded against this body (`evidence/README.md`)",
        )
    print(
        f"before    {baseline.arm.passed} passed, {baseline.arm.failed} failed, "
        f"{baseline.arm.outstanding or '—'} outstanding"
    )
    if baseline.unqualified:
        # Before the drafter is paid. The after arm routes each case back to the registration it
        # was recorded on, and a case whose model carries no registration name cannot be routed
        # anywhere: the shipped factory refused it with a credential error after a paid draft, on
        # this repository's own promoted case (#310).
        named = ", ".join(baseline.unqualified)
        return blocked(
            "improve.model_unqualified",
            f"{named}: the recorded model names no provider, so the after arm cannot re-ask it. "
            f"Re-harvest from a tape recorded through the registry (`eval harvest`), or set "
            f"`harvested.model` to `<provider>:<model>` -- the registration the case was recorded on",
        )
    if baseline.arm.failed == 0:
        # The pre-spend refusal, and the honest heart of the loop. A harvested case passes against
        # the answer it was harvested with by construction, so a corpus nobody has tightened is a
        # corpus at its ceiling: a draft can stay level or fall, never improve. Spending to learn
        # that would be spending to learn nothing.
        return blocked(
            "improve.nothing_to_improve",
            f"every attributable case passes against {body.label} as it stands, so no draft can improve "
            f"the measurement and nothing was spent. A case's expectations say what a correct answer "
            f"would have contained: tighten one to what the recorded answer missed, or add a rubric "
            f"and bind a judge",
        )

    evidence = tuple(f"{f.case}: {f.check} {f.detail}".rstrip() for f in baseline.arm.failures)
    drafted = await ctx.do(
        Draft(
            body=body.body,
            label=body.label,
            current=current,
            finding=attribution.finding,
            runs=attribution.runs,
            considered=attribution.considered,
            evidence=evidence,
        )
    )
    if drafted.status is not Status.SUCCEEDED or drafted.value is None:
        # A refusal or an error keeps its own reason; nothing here would say it better.
        return drafted
    report = drafted.value
    probes = improver.probes(body, current, report.text)
    measured = await ctx.do(Measure(probes=probes))
    if measured.status is not Status.SUCCEEDED:
        return measured
    answers = tuple(measured.value or ())
    # The rubric half, after the deterministic half and only where a judge is bound. One step
    # sharing the run's `Spend`, its reconciliation and its tape, so the measurement's bill is
    # the measurement's. An unbound `Judge` is not a refusal: the rubrics stay outstanding, the
    # scorecard says so, and the loop measures what it can, as it did before a judge existed.
    verdicts: tuple[Any, ...] = ()
    asks = improver.rubrics(baseline, answers)
    if asks and ctx.container.has(Judge):
        judged = await ctx.do(Judge(asks=asks, known=improver.known_verdicts()))
        verdicts = tuple(getattr(judged.value, "verdicts", ()) or ())
        replayed = set(getattr(judged.value, "replayed", ()) or ())
        # Kept whatever the judge's status: a verdict paid for is a verdict, and the next
        # measurement replays it rather than buying it again.
        append_verdicts(improver, tuple(v for v in verdicts if f"{v.case}/{v.arm}" not in replayed))
        if judged.status is not Status.SUCCEEDED:
            # A ceiling firing mid-judgement is the control working; the run stops here rather
            # than scoring a comparison the judge only half-answered.
            return judged
        print(f"judged    {len(verdicts)} rubric verdict(s), {len(replayed)} replayed")
    elif asks:
        print(f"judged    —  {len(asks)} rubric(s) outstanding: no Judge is bound")
    scorecard = improver.score(baseline, answers, verdicts=verdicts)
    for line in scorecard_lines(scorecard):
        print(line)

    # GATE-IMPROVE-2: one change, to the declared body, built here from the drafter's TEXT. The
    # model never names a path; the path is the declaration's, so there is nothing to refuse.
    changeset = ChangeSet(
        changes=(FileChange(path=body.body, contents=report.text),),
        summary=(
            f"feat(prompts): revise {body.label} against {attribution.finding}\n\n{report.rationale}"
        ).rstrip(),
    )
    if scorecard.verdict != IMPROVED:
        # GATE-IMPROVE-4: not opened. Kept as evidence, on the path `propose` never reads.
        written = write_changeset(ATTEMPT, changeset, scorecard=scorecard.as_record())
        print(f"attempt   {scorecard.verdict}, not proposed -> {written}")
        return Outcome(
            status=Status.FAILED,
            reason=f"improve.{scorecard.verdict}",
            value=scorecard,
            findings=(
                Finding(
                    id=f"improve.{scorecard.verdict}",
                    message=(
                        f"the draft {scorecard.verdict} the measurement over {len(scorecard.cases)} case(s)"
                    ),
                    severity=Severity.WARNING,
                ),
            ),
        )
    written = write_changeset(CHANGESET, changeset, scorecard=scorecard.as_record())
    print(f"staged    {body.body} -> {written}")
    return Outcome(status=Status.SUCCEEDED, value=scorecard)


async def improve_propose(ctx: RunContext, scm: Scm, artifact: str = CHANGESET) -> Outcome[Any]:
    """Open the staged proposal, if the ceiling has room. Everything read here is untrusted."""
    changeset = read_changeset(artifact)
    if not changeset.changes:
        return Outcome(status=Status.FAILED, reason="improve.no_proposal")
    raw = read_scorecard(artifact)
    if raw is None:
        return blocked(
            "improve.unmeasured",
            "the staged change carries no scorecard, so it was not measured before it reached the job "
            "that can open it; nothing is opened on a number nobody can read back",
        )
    scorecard = Scorecard.from_record(raw)
    if scorecard.verdict != IMPROVED:
        return blocked(
            "improve.not_improved", f"the scorecard says {scorecard.verdict}; only an improvement is opened"
        )

    # GATE-IMPROVE-2, again at the open: the artifact came from another job. Exactly one change,
    # to a body this lifecycle declares, and not a deletion.
    declared = {b.body: b for b in ctx.improvable}
    paths = changeset.paths()
    if len(paths) != 1 or paths[0] not in declared or changeset.changes[0].deleted:
        return blocked(
            "improve.not_one_body",
            f"a proposal changes exactly one declared Improvable body; this one names {list(paths)} and "
            f"the lifecycle declares {sorted(declared) or '—'}",
        )
    body = declared[paths[0]]

    # GATE-IMPROVE-8: the ceiling, where the proposal is opened. The same posture as `gate`: a
    # host that cannot count is refused, because an uncounted ceiling is not an empty one.
    listing = getattr(scm, "open_changes_by_workflow", None)
    if listing is None:
        return blocked(
            "improve.ceiling_unread",
            f"{type(scm).__name__} cannot list change requests; an uncounted ceiling is not an empty one",
        )
    try:
        open_now = tuple(listing(PROPOSE))
    except Exception as error:  # noqa: BLE001 - every failure to count is the same answer
        return blocked("improve.ceiling_unread", str(error) or type(error).__name__)
    if len(open_now) >= ctx.max_open_proposals:
        named = ", ".join(getattr(c, "url", "") or getattr(c, "branch", "") for c in open_now)
        return blocked(
            "improve.ceiling_reached",
            f"{len(open_now)} proposal(s) already open (max {ctx.max_open_proposals}): {named}",
        )
    print(f"proposals {len(open_now)} open  (max {ctx.max_open_proposals})")

    title = changeset.summary.splitlines()[0] if changeset.summary else f"feat(prompts): revise {body.label}"
    rationale = changeset.summary.partition("\n\n")[2]
    change = await open_reviewable(
        scm,
        changeset,
        # A draft, and the loop never marks it ready. The deterministic arms are a floor check --
        # nothing the current body passed was lost -- and not a judgment that the new body is
        # better; that judgment is the person's, and a proposal entering their review queue as
        # "ready" would be the loop making it for them. Marking it ready is the reviewer's act.
        ready=False,
        title=title,
        body=improve_body(scorecard, rationale, run_id=ctx.run_id, label=body.label),
        workflow=PROPOSE,
        run_id=ctx.run_id,
    )
    print(f"change    {change.url or change.branch}")
    # Then wait for the person (§13): the proposal entered their queue as a draft, and what the
    # loop learns from it -- accepted, changed, declined -- is a human event, not a run. On a
    # SHARED store the run parks on the pull request's review and `improve/after-review` is
    # what their event starts; on a LOCAL store nothing can park (`GATE-OUT-6`), and the
    # proposal is still open, so that is said and the run succeeds as it always did.
    number = getattr(change, "number", None)
    store = ctx.ledger
    if number is None or store is None or getattr(store, "scope", "local") != "shared":
        why = (
            "no change request number"
            if number is None
            else f"the ledger is {type(store).__name__ if store else 'none'}"
        )
        print(f"parked    not: {why}; the proposal waits in the review queue without a barrier")
        return Outcome(status=Status.SUCCEEDED, value=change)
    return await ctx.park(
        HumanBoundary.pr_review(int(number)),
        resume=AFTER_REVIEW,
        payload={"change": change.url or change.branch, "number": int(number), "label": body.label},
    )


async def improve_after_review(ctx: RunContext, r: Resumption) -> Outcome[Any]:
    """The continuation a person's review of a proposal starts (§13.4).

    What the loop does with a verdict is small on purpose: the proposal is a pull request and
    the person merged it or did not; this run records which, by whom, as the run that closed the
    lifecycle the parked one opened, so `history --explain` shows the chain and `report` counts
    the outcome. It does not re-measure, re-open or argue.
    """
    verdict = "accepted" if r.affirmative else "declined"
    print(f"proposal  {r.payload.get('change', '?')} {verdict} by {r.actor} ({r.verdict})")
    return Outcome(
        status=Status.SUCCEEDED if r.affirmative else Status.FAILED,
        reason=f"human.{r.verdict}",
        value={
            "change": r.payload.get("change"),
            "actor": r.actor,
            "verdict": r.verdict,
            "parent": r.parent_run_id,
        },
    )


def register() -> None:
    """Claim the `improve/*` ids. Called by an adopter's `lockstep.py`, never on import."""
    workflow(id=MEASURE)(improve_measure)
    workflow(id=PROPOSE)(improve_propose)
    workflow(id=AFTER_REVIEW)(improve_after_review)
