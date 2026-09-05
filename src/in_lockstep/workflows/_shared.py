"""What both processes need, defined once.

`init --implement --fix` used to append this function twice, byte-identically, because each
scaffold block stood alone. `_without_duplicate_definitions` was written to delete the second copy
after the fact; with one definition here there is no second copy to delete.
"""

from __future__ import annotations

from typing import Any

from ..core.context import RunContext


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
