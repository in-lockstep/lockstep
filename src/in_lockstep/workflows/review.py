"""The required review check as one run: the lenses that gate a pull request, fanned out.

`review/all-lenses` is what `lockstep.yml` invokes on every pull request here. Until Phase 5 the
check was one `review` invocation looping four `--aspect` flags in sequence, and the file named
the lenses; now the branches are declared from the adapter's own map (`GATE-REVIEW-5`: a lens the
framework declares and the check never runs is a lens shipped on our word, and a list written in
YAML is exactly what goes stale), run at once under one joint budget, one tape and one kill
switch (`GATE-COST-6`, `GATE-ASYNC-3b`), and joined -- the worst verdict is the run's, every
branch's findings are the run's findings, and one record with four steps is written where four
records were.

Since #449 those branches are the lenses that GATE, which is not the same set as the lenses that
EXIST. `AiReview(lenses=...)` answers the second and is what `/review <lens>` on a thread resolves
against; `register(gating=...)` answers the first. One map used to answer both, so the only lever
for keeping a lens off the required check was dropping it -- which also put it out of reach of the
comment asking for it. The two are orthogonal now: a lens bound but not gating is available on the
thread and not run by the check.

Here all four gate, by decision and not by default. O10 exists so the shipped lenses are not
things we ask adopters to trust on our word, and a subset in THIS repository would be that defect
returning (`GATE-REVIEW-9`).

A lens's comment body is still one file per lens under the directory the trampoline names, so
the `publish` job posts one sticky comment per gating lens a reader can tell apart
(`GATE-REVIEW-6`).
"""

from __future__ import annotations

from collections.abc import Collection
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


def _named(names: Collection[str]) -> str:
    return ", ".join(sorted(names))


def _refused(reason: str, message: str) -> Outcome[Any]:
    """A configuration this check will not guess at, as the run's own outcome.

    `blocked` rather than `failed`, because nothing ran and nothing spent and that is §4.3's own
    category. It is still red where it matters: `EXIT_BLOCKED` is 3, so a required check whose
    lens selection is wrong fails rather than reading as a green check that reviewed nothing.
    """
    return Outcome.blocked_by(
        reason,
        findings=(Finding(id=reason, message=message, severity=Severity.ERROR, blocking=True),),
    )


async def review_all_lenses(
    ctx: RunContext,
    base: str,
    head: str,
    comments: str = "",
    diff: str = "",
    *,
    gating: Collection[str] | None = None,
) -> Outcome[Any]:
    """Every lens that gates this change, at once, as one run.

    `comments` is a directory: one body per lens lands in it for the job that holds the write
    token to post. `diff` is for a replay whose tape was recorded against a diff git no longer
    has (the shipped fixture), and is otherwise empty.

    `gating` comes from `register(gating=...)` and from nowhere else. Keyword-only, and absent
    from the callable that `register` actually registers, because `in-lockstep run` builds its
    `--arg` surface by introspecting that callable's signature -- and a lens list reachable from
    the trampoline is the question `GATE-REVIEW-5` closed.
    """
    declared = bound_lenses(ctx)
    if not declared:
        return _refused(
            "review.no_lenses",
            "no Review adapter declaring its lenses is bound; nothing was reviewed. "
            "`in-lockstep run` binds the shipped AiReview when a module binds none, so this "
            "means a module bound an adapter with no `compositions()`.",
        )

    lenses = declared
    if gating is not None:
        # Resolved against what the adapter declares rather than trusted, and resolved HERE --
        # after the binding exists, before the fan-out. `register` cannot do it: a module is free
        # to call it before it binds `Review`, and there is no container at registration time.
        asked = tuple(dict.fromkeys(gating))
        if not asked:
            return _refused(
                "review.gating_empty",
                "`register(gating=...)` names no lens at all, so nothing would gate and this "
                "check could not fail for any reason. Omit the argument to gate on every bound "
                f"lens ({_named(declared)}), or name the ones that should.",
            )
        unknown = tuple(name for name in asked if name not in declared)
        if unknown:
            return _refused(
                "review.gating_unknown",
                f"`register(gating=...)` names {_named(unknown)}, which the bound Review adapter "
                f"does not declare. It declares {_named(declared)}. Refused rather than dropped: "
                "a typo that quietly gates on fewer lenses than somebody meant is a check that "
                "goes green for the wrong reason, which is the one failure a required check must "
                "not have.",
            )
        lenses = tuple(name for name in declared if name in set(asked))
        # Said out loud on every run, because the difference between the bound set and the gating
        # set is invisible in the check's output otherwise -- and a reader looking at four sticky
        # comments where five lenses are bound should not have to open `lockstep.py` to know why.
        print(f"gating    {len(lenses)} of {len(declared)} bound lenses: {_named(lenses)}")

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


def register(*, gating: Collection[str] | None = None) -> None:
    """Claim the `review/*` ids, and say which of the bound lenses gate a pull request.

    Called by an adopter's `lockstep.py`, never on import.

    `gating` lives here and not beside `AiReview(lenses=...)` because it is a property of the
    CHECK rather than of the adapter. The adapter's map says which lenses this repository HAS, and
    `chatops.aspect_from` resolves `/review <lens>` against exactly that map -- so narrowing it to
    keep a lens off the required check would also put that lens out of reach of the comment asking
    for it. That is the lever #449 was filed about being the wrong one, and this is the right one.

    `None`, the default, gates on every bound lens: a repository that says nothing is unaffected.
    A named lens the bound adapter does not declare refuses the run by name, at run time and
    before the fan-out, so nothing has spent.
    """
    selection = None if gating is None else tuple(gating)

    async def all_lenses(
        ctx: RunContext, base: str, head: str, comments: str = "", diff: str = ""
    ) -> Outcome[Any]:
        return await review_all_lenses(ctx, base, head, comments, diff, gating=selection)

    # Re-declared rather than wrapped with `functools.wraps`. `wraps` sets `__wrapped__` and
    # `inspect.signature` follows it, which would put `gating` among the arguments `in-lockstep
    # run` accepts as `--arg` -- a lens list reachable from the trampoline YAML, i.e. exactly what
    # `GATE-REVIEW-5` closed. The four parameters above are the whole callable surface.
    #
    # `__qualname__` is left as the closure's own: two calls read as ONE declaration to
    # `_same_declaration` (same module, same qualname), so re-executing a `lockstep.py` reinstalls
    # its selection rather than raising `DuplicateWorkflow` against itself.
    all_lenses.__doc__ = review_all_lenses.__doc__
    workflow(id=ALL_LENSES)(all_lenses)


__all__ = ["ALL_LENSES", "bound_lenses", "register", "review_all_lenses"]
