"""The promoted corpus, judged: every rubric put to the bound judge over the answer its case was
recorded with, and every verdict kept beside the case.

One workflow, `judge/corpus`, which `eval run --judge` dispatches and `in-lockstep run judge/corpus`
runs the same way. It is a workflow and not a branch of `eval run` because it spends: a judge is
a model call, and a model call here is a run -- routed, priced, recorded, under a ceiling, with a
ledger record -- or it does not happen (O4). Plain `eval run` stays the offline settle
`make check` calls, and the flag is the whole difference.

What is sent is the improver's question, not this module's: `corpus_rubrics` grades the half a
script can settle first and asks about no case that failed it (`GATE-JUDGE-2`), and
`known_verdicts` hands `Judge` what was already decided so a rubric judged once is replayed rather
than paid for (`GATE-JUDGE-3`). This module asks, keeps, and prints.
"""

from __future__ import annotations

from typing import Any

from ..adapters.ai import Judge
from ..core.context import RunContext
from ..core.improve import Improver
from ..core.outcome import Outcome, Status
from ..core.workflow import workflow
from ..platform.verdicts import append_verdicts

CORPUS = "judge/corpus"


async def judge_corpus(ctx: RunContext, improver: Improver) -> Outcome[Any]:
    """Asks -> judge -> sidecar -> summary. Writes nothing but the sidecars."""
    asks = improver.corpus_rubrics()
    if not asks:
        print("rubrics   —  no promoted case states a rubric its recorded answer could be judged on")
        return Outcome(status=Status.SUCCEEDED, value=improver.judged(()), decided=False)
    known = improver.known_verdicts()
    print(f"rubrics   {len(asks)} to judge, {len(known)} verdict(s) already kept")

    judged = await ctx.do(Judge(asks=asks, known=known))
    report = judged.value
    verdicts = tuple(getattr(report, "verdicts", ()) or ())
    replayed = set(getattr(report, "replayed", ()) or ())
    fresh = tuple(v for v in verdicts if f"{v.case}/{v.arm}" not in replayed)
    # Kept before the verdict on the run is read, so a ceiling that stopped the corpus half-judged
    # still leaves the half that was paid for where the next run finds it.
    for path in append_verdicts(improver, fresh):
        print(f"kept      {path}")
    print(f"judged    {len(fresh)} asked, {len(replayed)} replayed, {len(asks) - len(verdicts)} unsettled")
    if judged.status is not Status.SUCCEEDED:
        # A refusal or an error keeps its own reason; nothing here would say it better.
        return judged

    summary = improver.judged(verdicts)
    for line in summary.get("lines", ()):
        print(line)
    rate = summary.get("pass_rate")
    print(f"pass rate {'n/a — nothing decided' if rate is None else f'{rate:.0%}'}")
    return Outcome(
        status=Status.SUCCEEDED,
        value=summary,
        findings=judged.findings,
        cost=judged.cost,
        # Decided only when every rubric the corpus asked about got a rung; an unsettled one is
        # still outstanding, and outstanding is not a decision (GATE-OUT-2).
        decided=judged.decided,
        reason=judged.reason,
    )


def register() -> None:
    """Claim the `judge/*` ids. Called by an adopter's `lockstep.py`, never on import."""
    workflow(id=CORPUS)(judge_corpus)
