"""Everything a person has already said about a piece of work, from both places they say it.

A developer arguing with an AI on their laptop does it in one window. The same argument, moved
into a repository, is split across two: the ticket, where the work was asked for, and the pull
request, where the work is judged. Only the first reached a prompt, and the consequence was a
surprising one — the most specific instruction a reviewer ever gives ("not like that, like this,
on line 29") was the one instruction the next run could not see. A reviewer would type it, nothing
would change, and the obvious conclusion is that the framework ignored them.

So this joins the two. `with_review` reads the change requests the framework opened for a ticket,
gathers what people said on them, and hands them back on the ticket — where `Ticket.as_context`
tags them untrusted along with everything else a person can write.

Three things it deliberately does not do:

*It does not search.* Change requests are matched on the branch `branch_for` wrote, so a pull
request a stranger opened saying "fixes #218" is not gathered as though a reviewer of our change
had written it.

*It does not trust.* A review comment is a person writing at a model, which is the same category
of input as the ticket body, and it arrives with the same provenance. What a reviewer says is
evidence of what a human wants; it is never an instruction the framework obeys.

*It does not fail the run.* A host that cannot read a conversation — plain git, a token without
`pull-requests: read`, a third-party adapter that never implemented it — leaves the ticket exactly
as it arrived. A run that can still read the issue is worth more than a run that refuses over
context it would merely have been nice to have. But it says so, every time, because context that
silently did not arrive is the kind of thing you discover six rounds later.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .scm.base import MAX_REMARKS

#: What the caller prints. Always something: "none" and "unavailable" are different facts and both
#: are worth a line in a run's output.
Note = str


async def with_review(ticket: Any, scm: Any) -> tuple[Any, Note]:
    """`ticket`, plus what people said on the open change requests opened for it.

    Returns the ticket and one line describing what was gathered, which the caller prints. A tuple
    rather than a print from in here: a library that writes to stdout cannot be used by anything
    that wanted to put the line somewhere else.
    """
    key = str(getattr(ticket, "key", "") or "")
    if not key:
        return ticket, "review    skipped (the ticket has no key to match a branch against)"
    if not (hasattr(scm, "changes_for") and hasattr(scm, "remarks")):
        host = type(scm).__name__ if scm is not None else "none"
        return ticket, f"review    unavailable ({host} does not read review conversation)"

    try:
        changes = await scm.changes_for(key)
    except (RuntimeError, OSError) as e:
        return ticket, f"review    unavailable (could not list change requests: {_short(e)})"

    if not changes:
        return ticket, f"review    none (no open change request for {key})"

    blocks: list[str] = []
    seen: list[str] = []
    for change in changes:
        number = getattr(change, "number", None)
        where = _where(change)
        if number is None:
            continue
        try:
            remarks = await scm.remarks(int(number))
        except (RuntimeError, OSError) as e:
            return ticket, f"review    partial ({where} could not be read: {_short(e)})"
        blocks += [text for r in remarks if (text := r.as_text(where=where))]
        seen.append(where)

    if not blocks:
        return ticket, f"review    none (nothing said on {', '.join(seen)})"

    # Newest last is how a thread reads, and the cap has to bite at the OLD end for the same
    # reason: the sentence a reviewer wrote most recently is the one they expect to be acted on,
    # and a cap that dropped the tail would silently discard exactly it.
    blocks = blocks[-MAX_REMARKS:]
    return replace(ticket, review=tuple(blocks)), f"review    {len(blocks)} remark(s) from {', '.join(seen)}"


def _where(change: Any) -> str:
    number = getattr(change, "number", None)
    return f"#{number}" if number is not None else str(getattr(change, "branch", "") or "a change")


def _short(error: Exception) -> str:
    """One line of an error, because this goes in a run's output beside everything else it did."""
    text = str(error).strip().splitlines()
    return (text[0] if text else type(error).__name__)[:160]
