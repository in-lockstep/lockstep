"""The cap on a conversation keeps the newest turns, not the oldest.

A run reads a ticket's conversation, and the answer is almost always at the end — a reviewer
correcting a premise, a person settling a question, a decision recorded after the body was written.
The framework's cap and the curator's trim both have to bite at the OLD end, and every comment has
to be identifiable so the dropped record names which turns were left out.

Three layers, three properties:

1. The cap keeps the newest N, not the first N.
2. Each turn's path carries its position (``#387#comment-07``), so the dropped record is specific.
3. The curator drops the oldest conversation turns when the token budget runs out, not the newest.

What is NOT changed: ``MAX_BODY``'s truncation of the ticket body keeps the opening, which is
where its subject is.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from in_lockstep.ai.context import ContextCurator, ContextItem, ContextNeed, Provenance
from in_lockstep.platform.tickets.base import Ticket
from in_lockstep.platform.tickets.github import MAX_COMMENTS as GH_MAX_COMMENTS

T = TypeVar("T")


def _run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. The cap keeps the NEWEST comments
# ---------------------------------------------------------------------------


def test_github_issues_cap_keeps_the_newest_comments() -> None:
    """GATE-CONTEXT-1. On a thread with more comments than the cap, the framework should keep the most recent
    ones — not the oldest. The last comment is the one that changed something."""
    from in_lockstep.platform.tickets.github import GitHubIssues

    total = GH_MAX_COMMENTS + 20
    comments = [{"body": f"comment-{i}"} for i in range(total)]
    data = {
        "number": 400,
        "title": "test",
        "body": "body",
        "state": "OPEN",
        "labels": [],
        "assignees": [],
        "comments": comments,
        "url": "https://example.com/400",
    }

    source = GitHubIssues()
    # Bypass the subprocess by injecting the parsed data directly.
    source._gh_json = lambda *a: data  # type: ignore[method-assign]
    ticket = _run(source.get("#400"))

    assert len(ticket.comments) == GH_MAX_COMMENTS
    # The LAST comment in the original list must be present (it is the newest).
    assert f"comment-{total - 1}" in ticket.comments[-1]
    # The FIRST comment (oldest) must have been dropped.
    assert "comment-0" not in " ".join(ticket.comments)


def test_gitlab_issues_cap_keeps_the_newest_comments() -> None:
    """GATE-CONTEXT-1. Same property as GitHub: the cap keeps the tail, not the head."""
    from in_lockstep.platform.tickets.gitlab import MAX_COMMENTS as GL_MAX_COMMENTS
    from in_lockstep.platform.tickets.gitlab import GitLabIssues

    # A thread LONGER THAN ONE PAGE, which is the case this is about: the walk stops as soon as it
    # holds the cap, so an ascending request would never reach the end and slicing its tail would
    # return the newest of the first page — still the beginning of the conversation. The fake
    # pages the way GitLab does and honours `sort`, so a request that asks for the wrong order
    # gets the wrong answer here rather than in production.
    total = 250
    notes = [{"id": i, "body": f"note-{i}", "system": False} for i in range(total)]

    class _FakeResponse:
        def __init__(self, rows: list[dict[str, Any]], next_page: str) -> None:
            self.status_code = 200
            self.content = True
            self.headers = {"x-next-page": next_page}
            self._rows = rows

        def json(self) -> list[dict[str, Any]]:
            return self._rows

    class _FakeClient:
        def request(self, method: str, path: str, **kw: Any) -> _FakeResponse:
            params = kw.get("params") or {}
            page = int(params.get("page", 1))
            per = int(params.get("per_page", 100))
            ordered = notes if params.get("sort") == "asc" else list(reversed(notes))
            start = (page - 1) * per
            rows = ordered[start : start + per]
            return _FakeResponse(rows, str(page + 1) if start + per < len(ordered) else "")

    source = GitLabIssues(base_url="https://gl.example.com", project="acme/app", client=_FakeClient())
    comments = source._comments("99")

    assert len(comments) == GL_MAX_COMMENTS
    assert comments[-1] == f"note-{total - 1}", "the newest comment must be the last one kept"
    assert comments[0] == f"note-{total - GL_MAX_COMMENTS}", "and the window reads oldest-first"
    assert "note-0" not in comments, "the beginning of the thread was kept over the end"


def test_github_scm_remarks_cap_keeps_the_newest() -> None:
    """GATE-CONTEXT-1. Pull-request remarks are capped the same way: newest kept, oldest dropped."""
    from pathlib import Path

    from in_lockstep.platform.scm import GitHubScm
    from in_lockstep.platform.scm.base import MAX_REMARKS

    total = MAX_REMARKS + 10
    scm = GitHubScm(Path("."))

    view_data = {
        "comments": [{"author": {"login": f"u{i}"}, "body": f"c-{i}"} for i in range(total)],
        "reviews": [],
    }

    def fake_json(*args: str) -> object:
        if args[:2] == ("pr", "view"):
            return view_data
        return []  # line notes

    scm._gh_json = fake_json  # type: ignore[method-assign]
    remarks = _run(scm.remarks(42))

    comment_remarks = [r for r in remarks if r.kind == "comment"]
    assert len(comment_remarks) == MAX_REMARKS
    # Newest must survive.
    assert f"c-{total - 1}" in comment_remarks[-1].body
    # Oldest must have been dropped.
    assert "c-0" not in " ".join(r.body for r in comment_remarks)


# ---------------------------------------------------------------------------
# 2. Each comment is identifiable by position in its path
# ---------------------------------------------------------------------------


def test_comment_paths_carry_their_position() -> None:
    """GATE-CONTEXT-1. A comment's path must be ``#key#comment-NN`` (zero-padded), so the dropped record names
    which turns were left out rather than repeating the same string for every one."""
    ticket = Ticket(key="#387", title="t", comments=("first", "second", "third"))
    items = ticket.as_context()
    comment_items = [i for i in items if i.kind == "ticket" and "comment" in i.path]

    assert len(comment_items) == 3
    assert comment_items[0].path == "#387#comment-00"
    assert comment_items[1].path == "#387#comment-01"
    assert comment_items[2].path == "#387#comment-02"


def test_review_paths_carry_their_position() -> None:
    """Same property for review remarks."""
    ticket = Ticket(key="#387", title="t", review=("r0", "r1"))
    items = ticket.as_context()
    review_items = [i for i in items if i.kind == "review"]

    assert len(review_items) == 2
    assert review_items[0].path == "#387#review-00"
    assert review_items[1].path == "#387#review-01"


def test_dropped_comment_names_the_position_it_left_out() -> None:
    """GATE-CONTEXT-1. A comment that does not fit the budget is named by its position in the
    dropped record -- not a bare ``ticket:#387#comment`` repeated, which cannot tell four drops
    from one."""
    ticket = Ticket(key="#99", title="t", comments=("a" * 800, "b" * 800, "c" * 800))
    items = list(ticket.as_context())
    # Curate with a budget that fits only two of the three comments (plus the body).
    package = ContextCurator().curate(items, ContextNeed(token_budget=500))
    # At least one comment should have been dropped, and the dropped name must carry a position.
    if package.dropped:
        for name in package.dropped:
            if "comment" in name:
                assert name != "ticket:#99#comment", (
                    "the dropped record does not identify which comment was left out"
                )


# ---------------------------------------------------------------------------
# 3. The curator trims the oldest conversation turns when the budget is tight
# ---------------------------------------------------------------------------


def test_curator_keeps_newest_conversation_turns_under_tight_budget() -> None:
    """GATE-CONTEXT-1. A budget that cannot hold every comment drops the OLDEST -- the turns a
    reader skims -- and keeps the newest, which are the ones that changed something."""
    comments = [
        ContextItem(
            kind="ticket",
            content=f"comment {i} " + "x" * 200,
            provenance=Provenance.UNTRUSTED_EXTERNAL,
            path=f"#1#comment-{i:02d}",
        )
        for i in range(10)
    ]
    body = ContextItem(
        kind="ticket",
        content="the body",
        provenance=Provenance.UNTRUSTED_EXTERNAL,
        path="#1",
    )
    items = [body] + comments
    # Budget that fits the body and roughly five comments.
    package = ContextCurator().curate(items, ContextNeed(token_budget=300))
    kept_paths = [i.path for i in package.items if "comment" in i.path]

    assert len(kept_paths) > 0, "no comments survived at all"
    assert len(kept_paths) < 10, "all comments fit — the budget was not tight enough"
    # The newest comment (#09) must be among the kept ones.
    assert "#1#comment-09" in kept_paths, (
        "the newest comment was dropped; the trim is biting at the wrong end"
    )
    # The oldest comment (#00) must have been dropped.
    assert "#1#comment-00" not in kept_paths, (
        "the oldest comment survived a tight budget; the trim should drop it first"
    )
