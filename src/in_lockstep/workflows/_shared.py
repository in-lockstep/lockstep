"""What both processes need, defined once.

`init --implement --fix` used to append this function twice, byte-identically, because each
scaffold block stood alone. `_without_duplicate_definitions` was written to delete the second copy
after the fact; with one definition here there is no second copy to delete.
"""

from __future__ import annotations

from typing import Any

from ..core.context import RunContext
from ..core.outcome import Status


async def pointed_at(target: str, ticket: str, tickets: Any, scm: Any) -> tuple[Any, Any, str, str]:
    """The ports and the ticket key a run is about, for the repository it is FOR -- with lines to
    print. Returns `(tickets, scm, key, notes)`.

    What a fork needs. Its checkout is the fork and the work is the parent's: the ticket to
    implement, the review of the last attempt and the thread to answer are all over there, and
    unless it is said, each is read wherever the host tool infers -- which from a fork is the
    parent by luck of forkhood, until the `GH_REPO` pin a fork needs takes even that away.

    The narrowing and the resolution are ONE function because their order is not a preference.
    `ticket_for` asks the host whether a number is a change request, and asking that before the
    adapter knows which repository it is about asks the wrong repository -- on GitHub, where issue
    and pull-request numbers share a sequence, that returns a confident wrong answer rather than
    an error. Two statements a caller writes in either order would eventually be written in the
    wrong one.

    An empty target returns exactly what it was handed, so a repository that is not a fork never
    reaches an adapter method that did not exist before #373. A target that names something
    narrows BOTH ports: reading the ticket from one repository and answering on another is worse
    than either alone.

    Raises `Unsupported` when a bound port cannot be pointed anywhere -- a Jira source, a GitLab
    project, plain git -- naming which one refused. Never a quiet fallback to this repository: a
    run that read the wrong ticket implements work nobody asked for, and nothing downstream would
    say so.
    """
    from ..platform.conversation import ticket_for

    if target:
        tickets, scm = _narrowed(tickets, target, "tickets"), _narrowed(scm, target, "scm")
    key, where = await ticket_for(ticket, scm)
    return tickets, scm, key, f"target    {target}\n{where}" if target else where


async def described(ctx: Any, ticket: Any, changeset: Any, verdict: Any) -> Any:
    """The reviewer-facing account of a staged change, or None when nothing wrote one.

    Called where the provider credential is -- the work job -- because the job that opens the
    change holds a write token and no provider key, and `implement.yml` says so at the line that
    grants federation: "gate and propose call no model". So this runs beside `verdict_over_staged`
    and travels in the same artifact, for the same reason the verdict does.

    **What it is given is the point.** The ticket, a DIFF of the staged change, and the framework's
    own measured line -- and nothing of the session that produced any of it. A session's summary is
    addressed to the framework at the end of its turn, and everything it omits is exactly what a
    reader is missing; a describer whose situation is the reader's cannot make that mistake. #389
    opened a pull request whose first paragraph was a model reasoning about its own test mocks.

    None on every failure, and never a raised one: no Describe bound (most repositories), a ceiling
    that refused the turn, a reply that did not parse. The change is the run's product and this is
    an account of it, so a body that falls back to the run's own cover note is worse and a run that
    failed for want of one would be absurd.
    """
    from ..adapters.ai.describe import Describe
    from ..adapters.worktree import staged_diff
    from ..platform.report import verdict_line

    if not getattr(ctx, "container", None) or not ctx.container.has(Describe):
        return None
    diff = await staged_diff(ctx.repo.root, changeset)
    outcome = await ctx.do(
        Describe(
            key=str(getattr(ticket, "key", "") or ""),
            title=str(getattr(ticket, "title", "") or ""),
            ticket=str(getattr(ticket, "description", "") or ""),
            diff=diff,
            verdict=verdict_line(verdict),
        )
    )
    if outcome.status is not Status.SUCCEEDED or outcome.value is None:
        print(f"describe  none ({outcome.reason or outcome.status.value}); the run's own note stands")
        return None
    return outcome.value


def _narrowed(port: Any, target: str, named: str) -> Any:
    from ..core.ports import Unsupported

    narrow = getattr(port, "for_repo", None)
    if narrow is None:
        # A port that predates the narrowing, or a third party's. Absent is not "yes": the rule
        # `TicketSource`'s refusing defaults are written on.
        raise Unsupported(
            f"the bound {named} ({type(port).__name__}) cannot be pointed at {target}: it has no for_repo()"
        )
    return narrow(target)


def last_unsuccessful(ctx: RunContext, ticket: str, family: str) -> dict[str, Any] | None:
    """The newest recorded run of THIS family, for this ticket, that did not succeed.

    Matched on the `ticket` the record carries rather than on the run id, because a run id is a
    string a person would have to parse and the field exists for exactly this.

    `ctx` is passed rather than reached for: this is framework code now, so there is no
    module-level `lockstep` to close over, and the container it needs is the run's own.

    `family` is the prefix of the record's `workflow` — "implement/" or "fix/". Without it this
    matched on ticket and status alone, so a `/fix` report could find an `implement/` run and
    quote its reason as though it were the fix attempt. Both verbs answer on the same ticket, so
    the two are routinely present together.

    Passed as an argument rather than defaulted per copy, and that is load-bearing rather than
    stylistic: `init --implement --fix` appends both blocks, and the de-duplicator drops the
    second copy of a shared definition only when it is byte-identical to the first. A per-copy
    default would make them differ, so both would be emitted and the module would carry a
    redefinition again.

    A blocked run is included, deliberately. It did not succeed and a person waiting on the ticket
    needs to know it stopped — what must not happen is calling it a failure, which is the caller's
    job to get right.
    """
    from in_lockstep.platform.ledger import store_for

    store = store_for(ctx.container)
    reader = getattr(store, "records", None)
    if reader is None:
        return None
    wanted = {ticket, ticket.lstrip("#"), "#" + ticket.lstrip("#")}
    mine = [
        r
        for r in reader()
        if str((r.get("args") or {}).get("ticket", r.get("ticket", ""))) in wanted
        and r.get("status") != "succeeded"
        and str(r.get("workflow") or r.get("kind") or "").startswith(family)
    ]
    mine.sort(key=lambda r: str(r.get("ts", "")))
    return mine[-1] if mine else None
