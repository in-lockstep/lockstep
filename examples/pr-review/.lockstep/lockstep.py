"""Reviewing a pull request through several lenses.

The whole example. There is no spec directory, no manifest, and nothing generated from this — it
is imported and run.

The shape worth noticing: **an aspect is an agent, not a data row.** Four lenses are four
strategies, not one prompt with a parameter, because that is what makes each one independently
budgetable, independently measurable, and independently something a team can override without
touching the others.
"""

from typing import Any

from in_lockstep import Lockstep, Outcome, Policy, RunContext, workflow
from in_lockstep.adapters.ai.review import Review
from in_lockstep.core.spend import Budget

lockstep = Lockstep.detect()

# The four lenses share a budget, because they share a run. Fan-out multiplies spend, so the
# ceiling is joint rather than per-branch: `fan_out` reserves every branch against this one ceiling.
lockstep.budget = Budget(usd=1.50, wall_seconds=600)

lockstep.contribute(
    Policy(
        name="review-floor",
        source="example",
        # Reviewing is read-only work. Saying so in policy rather than in prose is the difference
        # between a request and a constraint.
        deny_tools=("write_file", "shell", "apply_patch"),
        scan_input="block",
        max_turns=4,
    )
)

lockstep.models.route("review", "anthropic:claude-sonnet-4-6")

ASPECTS = ("security", "intent", "performance", "tests")


@workflow(id="pr-review/all-aspects")
async def review_all(ctx: RunContext, base: str, head: str) -> Outcome[Any]:
    """Every lens over one change, at once: four branches, one budget, one tape, one kill switch.

    A blocked branch does not stop the others; the join's verdict is the worst branch's, its cost
    the sum, and it is decided only if every branch was. This is the shape of the framework's own
    `review/all-lenses`, which reads the branches off the bound adapter rather than a tuple here:
    `from in_lockstep.workflows import review; review.register()` is the line that gets that one.
    """
    join = await ctx.fan_out(
        branches={a: ctx.call(Review(base=base, head=head, aspect=a), step=a) for a in ASPECTS}
    )
    return join.as_outcome()
