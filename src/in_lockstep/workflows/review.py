"""The required review check as one run: every lens the bound adapter declares, fanned out.

`review/all-lenses` is what `lockstep.yml` invokes on every pull request here. Until Phase 5 the
check was one `review` invocation looping four `--aspect` flags in sequence, and the file named
the lenses; now the branches are declared from the adapter's own map (`GATE-REVIEW-5`: a lens the
framework declares and the check never runs is a lens shipped on our word, and a list written in
YAML is exactly what goes stale), run at once under one joint budget, one tape and one kill
switch (`GATE-COST-6`, `GATE-ASYNC-3b`), and joined -- the worst verdict is the run's, every
branch's findings are the run's findings, and one record with four steps is written where four
records were.

A lens's comment body is still one file per lens under the directory the trampoline names, so
the `publish` job posts four sticky comments a reader can tell apart (`GATE-REVIEW-6`).
"""

from __future__ import annotations

from typing import Any

from ..adapters.ai.review import Review
from ..core.context import RunContext
from ..core.outcome import Finding, Outcome, Severity
from ..core.workflow import workflow
from ..platform.report import write_review_comments

ALL_LENSES = "review/all-lenses"


def bound_lenses(ctx: RunContext) -> tuple[str, ...]:
    """The lens names the bound `Review` adapter declares, read off `compositions()` -- the
    declared inspection surface, qualified `review/<lens>` -- and nothing else. Empty when no
    adapter is bound or the bound one does not declare: a set nobody stated is not one to run."""
    if not ctx.container.has(Review):
        return ()
    adapter: Any = ctx.container.resolve(Review)
    compositions = getattr(adapter, "compositions", None)
    if not callable(compositions):
        return ()
    return tuple(sorted(str(label).rsplit("/", 1)[-1] for label in compositions()))


async def review_all_lenses(
    ctx: RunContext, base: str, head: str, comments: str = "", diff: str = ""
) -> Outcome[Any]:
    """Every lens over one change, at once, as one run.

    `comments` is a directory: one body per lens lands in it for the job that holds the write
    token to post. `diff` is for a replay whose tape was recorded against a diff git no longer
    has (the shipped fixture), and is otherwise empty.
    """
    lenses = bound_lenses(ctx)
    if not lenses:
        return Outcome.blocked_by(
            "review.no_lenses",
            findings=(
                Finding(
                    id="review.no_lenses",
                    message=(
                        "no Review adapter declaring its lenses is bound; nothing was reviewed. "
                        "`in-lockstep run` binds the shipped AiReview when a module binds none, so "
                        "this means a module bound an adapter with no `compositions()`."
                    ),
                    severity=Severity.ERROR,
                    blocking=True,
                ),
            ),
        )
    join = await ctx.fan_out(
        branches={
            lens: ctx.call(Review(base=base, head=head, aspect=lens, diff=diff), step=lens) for lens in lenses
        }
    )
    for lens in lenses:
        outcome = join[lens]
        print(
            f"review/{lens:<12} {outcome.status.value}" + (f"  ({outcome.reason})" if outcome.reason else "")
        )
    if comments:
        written = write_review_comments(comments, {lens: join[lens] for lens in lenses})
        print(f"comment   wrote {len(written)} bod{'y' if len(written) == 1 else 'ies'} under {comments}")
    return join.as_outcome()


def register() -> None:
    """Claim the `review/*` ids. Called by an adopter's `lockstep.py`, never on import."""
    workflow(id=ALL_LENSES)(review_all_lenses)


__all__ = ["ALL_LENSES", "bound_lenses", "register", "review_all_lenses"]
