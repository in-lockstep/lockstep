"""What an earlier run tried, and what the suite said about it.

A failed run keeps its change now, and nothing read it back: the next `/implement` on the same
ticket started from a blank page, having already been paid for a diff that existed. Meanwhile the
person reading that failure knew something the model did not — which of the 13 tests failed, or
which of two readings of the ticket was meant — and could say so in a comment, but the model had to
rebuild the change from nothing in order to act on it.

## The verdict is what makes this work rather than anchor

Handed only its own diff, with no reason attached, a model defends it. Handed the diff AND the
tests it failed, it has a debugging session — and that is exactly the feedback the green phase
never gets, because a model cannot run this repository's suite (`WorktreeRunner` points `run_script`
at HEAD, in a container with no project dependencies).

So an attempt arrives as TWO items and not one, at two paths. The same separation `Ticket.as_context`
already draws between a ticket and a review remark, for the same stated reason: they are not
interchangeable to a reader, and a model that cannot tell "what you tried" from "what was asked"
will do the wrong thing with both.

## Provenance is GENERATED

This framework produced it. It is the one piece of context in an implementing session that no
stranger wrote, and tagging it `UNTRUSTED_EXTERNAL` alongside the ticket would make the egress rule
and the tool-shrink rules read a run resuming its own work as a run reading a fork's diff.
"""

from __future__ import annotations

from typing import Any

from ...ai.context import ContextItem, Provenance

#: How much of one attempt's diff travels. A resumed session carries the ticket, the comments, the
#: review remarks and now the attempts, and the attempts are the part with no natural ceiling — a
#: model that rewrote a thousand-line file staged a thousand lines. The curator drops whole items
#: when a budget is tight, so a bound here is what keeps "the attempt" from being the item that
#: costs the ticket its place.
MAX_ATTEMPT_CHARS = 12_000


def attempt_items(attempts: tuple[tuple[Any, Any], ...], *, key: str = "") -> list[ContextItem]:
    """`(changeset, verdict)` pairs as context, oldest first.

    Oldest first so the sequence reads as a history. An approach tried, abandoned and tried again
    in a worse form is a thing a reader can see across two diffs and cannot see in one — which is
    the only reason to carry more than the most recent.

    Numbering counts back from the newest: `#attempt-1` is the last thing that happened, whatever
    depth was asked for. A model told "attempt 1" should not have to know how many there were to
    work out which one it is looking at.
    """
    items: list[ContextItem] = []
    for index, (changeset, verdict) in enumerate(attempts):
        # Newest is 1. `attempts` is oldest first, so the last entry gets the lowest number.
        number = len(attempts) - index
        items.append(
            ContextItem(
                kind="attempt",
                content=_files(changeset),
                provenance=Provenance.GENERATED,
                path=f"{key}#attempt-{number}",
            )
        )
        items.append(
            ContextItem(
                kind="verdict",
                content=_verdict(verdict),
                provenance=Provenance.GENERATED,
                path=f"{key}#verdict-{number}",
            )
        )
    return items


def _files(changeset: Any) -> str:
    """The staged files, whole where they fit.

    Whole rather than a diff, because a diff needs a base to be read against and the model is being
    shown what it wrote, not what changed. Truncated per file so one enormous file cannot silently
    cost the others their place in the message.
    """
    changes = getattr(changeset, "changes", ()) or ()
    if not changes:
        return "(the attempt staged nothing)"
    blocks: list[str] = []
    budget = MAX_ATTEMPT_CHARS
    for change in changes:
        path = getattr(change, "path", "?")
        contents = getattr(change, "contents", None)
        if contents is None:
            blocks.append(f"{path}: deleted")
            continue
        text = str(contents)
        if len(text) > budget:
            text = text[: max(budget, 0)] + "\n…[truncated]"
        budget = max(0, budget - len(text))
        blocks.append(f"{path}:\n{text}")
    return "You staged this on an earlier attempt:\n\n" + "\n\n".join(blocks)


def _verdict(verdict: Any) -> str:
    """What the suite said, in the words a model can act on.

    A verdict that is ABSENT says so, and does not read as a pass. "No verdict" and "it passed" are
    the same shape of mistake the ledger spends its whole design refusing, and here it would tell a
    model its last attempt worked.
    """
    if verdict is None:
        return (
            "The suite was never run against that attempt, so nothing is known about whether it "
            "worked. This is not a pass."
        )
    total = getattr(verdict, "total", 0)
    if not total:
        return (
            "NOTHING WAS COLLECTED when that attempt was tested, so nothing was decided. This is "
            "not a pass — check that the file and class names match what this repository collects."
        )
    failed = getattr(verdict, "failed", 0)
    head = (
        f"That attempt was tested: {getattr(verdict, 'passed', 0)} passed, {failed} failed, "
        f"{getattr(verdict, 'skipped', 0)} skipped of {total}."
    )
    if not failed:
        return f"{head} Everything that ran, passed."
    names = [
        f"  {getattr(case, 'id', '?')}"
        for case in getattr(verdict, "cases", ()) or ()
        if getattr(case, "outcome", "") in ("failed", "error")
    ]
    listed = "\n".join(names[:50]) or "  (the report named no individual failures)"
    return f"{head}\n\nThese failed:\n{listed}"
