"""Opening a change so a human sees it at the right moment.

An AI change lands as a draft — out of the review queue — and is marked ready only once its tests
pass. A change nobody could verify green stays a draft: the reviewer still sees it, but it is not
asking for the sign-off a passing change is. The two propose workflows call this so the rule lives
in one place rather than being re-derived in each scaffold.
"""

from __future__ import annotations

from typing import Any


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
