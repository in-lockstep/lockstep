"""Reviewing a pull request through several lenses.

The whole example. There is no spec directory, no manifest, and nothing generated from this — it
is imported and run.

The shape worth noticing: **an aspect is an agent, not a data row.** Four lenses are four
strategies, not one prompt with a parameter, because that is what makes each one independently
budgetable, independently measurable, and independently something a team can override without
touching the others.
"""

from in_lockstep import Lockstep, Policy, workflow
from in_lockstep.adapters.ai.review import AiReview, Review, ReviewSpec
from in_lockstep.core.spend import Budget

lockstep = Lockstep.detect()

# The four lenses share a budget, because they share a run. Fan-out multiplies spend, so the
# ceiling is joint rather than per-branch — even before fan-out exists to make that literal.
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
async def review_all(ctx, base: str, head: str):
    """Every lens over one change.

    Sequential here. When fan-out lands these become declared branches over the same joint budget,
    and the only thing that changes is that they run at once — which is why `ctx.call` exists
    separately from `ctx.do` already.
    """
    reports = {}
    for aspect in ASPECTS:
        outcome = await ctx.do(Review, ReviewSpec(base=base, head=head, aspect=aspect))
        reports[aspect] = outcome
        # A blocked lens stops the run: it means a ceiling was hit or a control refused, and the
        # remaining lenses would hit the same one.
        if outcome.blocked:
            break
    return reports
