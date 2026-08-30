"""Opening a change so a human sees it at the right moment.

An AI change lands as a draft — out of the review queue — and is marked ready only once its tests
pass. A change nobody could verify green stays a draft: the reviewer still sees it, but it is not
asking for the sign-off a passing change is. The two propose workflows call this so the rule lives
in one place rather than being re-derived in each scaffold.
"""

from __future__ import annotations

from typing import Any

#: The label that marks a bug ticket as free for an agent to work at any time.
AI_GENERATED = "ai-generated"
#: The prefix of the label that counts how many automated attempts a ticket has already had.
ATTEMPT_PREFIX = "ai-attempt-"


async def open_reviewable(scm: Any, changeset: Any, *, ready: bool, **kwargs: Any) -> Any:
    """Open `changeset` as a draft pull request, and mark it ready only when `ready`.

    Draft by default, so an unverified or red change does not enter a human's review queue; ready
    when its tests passed, because a green change awaiting a human is exactly what is asking for
    review. `kwargs` are `open_change`'s (`title`, `body`, `ticket`, `workflow`, `run_id`, `base`).
    A host with no draft concept opens a plain request and `mark_ready` is a no-op, so this is safe
    everywhere.
    """
    change = await scm.open_change(changeset, draft=True, **kwargs)
    if ready:
        await scm.mark_ready(change)
    return change


def attempt_of(labels: Any) -> int:
    """How many automated attempts a ticket's labels record. A human-filed ticket has none (0); an
    `ai-generated` ticket this loop opened carries `ai-attempt-N`. The highest N wins, so a stray
    duplicate label cannot lower the count."""
    counts = [
        int(rest)
        for label in labels or ()
        if (rest := str(label)[len(ATTEMPT_PREFIX) :])
        and str(label).startswith(ATTEMPT_PREFIX)
        and rest.isdigit()
    ]
    return max(counts, default=0)


async def escalate(tickets: Any, source: Any, failure: str, *, max_attempts: int) -> Any:
    """A run failed its tests: open the next `ai-generated` bug ticket, or stop at the cap.

    The attempt count rides on the source ticket's labels, so the loop is bounded without any store:
    each new ticket is `ai-attempt-(n+1)`, and once that would exceed `max_attempts` no ticket is
    opened — a comment says a human is needed, and because nothing new gets the `ai-generated`
    label, the loop stops on its own. Returns the new ticket, or None when capped.
    """
    from .tickets.base import TicketDraft, TicketType

    attempt = attempt_of(getattr(source, "labels", ()))
    if attempt >= max_attempts:
        await tickets.comment(
            source,
            f"Automated fix attempts are exhausted ({attempt}/{max_attempts}). A human is needed — "
            f"no further `{AI_GENERATED}` ticket will be opened for this bug.",
        )
        return None
    draft = TicketDraft(
        title=f"Automated fix failed for {source.key} (attempt {attempt + 1})",
        description=(
            f"An automated attempt to fix {source.key} did not produce a change that passes its "
            f"tests:\n\n{failure}\n\nThis ticket is free for an agent to pick up. It is attempt "
            f"{attempt + 1} of at most {max_attempts}."
        ),
        type=TicketType.BUG,
        labels=(AI_GENERATED, f"{ATTEMPT_PREFIX}{attempt + 1}"),
    )
    return await tickets.create(draft)
