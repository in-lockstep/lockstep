"""What a reviewer says on a pull request has to reach the next run.

The gap this closes was a quiet one. A person could comment on the issue and the next `/implement`
would read it, and a person could comment on the pull request — where reviewing actually happens,
and where the most specific instruction anyone ever gives is pinned to a file and a line — and
nothing would read it at all. From the reviewer's chair the two look identical: you type, you run
it again, and one of them changes what happens.

These are the properties that make closing it safe rather than merely useful: only the framework's
own change requests are gathered, everything gathered is untrusted, the framework's own marker
never reaches a model, and a host that cannot read a conversation loses the context rather than the
run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from in_lockstep.ai.context import Provenance
from in_lockstep.platform.conversation import with_review
from in_lockstep.platform.scm import GitHubScm, GitLocal
from in_lockstep.platform.scm.base import MAX_REMARKS, Remark, branch_for, is_run_branch_for
from in_lockstep.platform.tickets import Ticket


def _run(coro):
    return asyncio.run(coro)


# -- what reaches the prompt ---------------------------------------------------------------


def test_gate_review_1_a_reviewers_note_reaches_the_prompt_tagged_untrusted() -> None:
    """GATE-REVIEW-1, second half. `Implement.ticket` calls `as_context` "the only route in".

    A review comment is a person writing at a model, which is the same category of input as the
    ticket body. Arriving by a second channel that forgot to say so is the failure the single
    tagging point exists to prevent — so the review rides on the ticket and is tagged there.
    """
    ticket = Ticket(key="#218", title="t", description="d", review=("@dana: iterate the entries",))
    items = ticket.as_context()
    review = [i for i in items if i.kind == "review"]
    assert len(review) == 1
    assert review[0].provenance is Provenance.UNTRUSTED_EXTERNAL
    assert "iterate the entries" in review[0].content


def test_a_review_remark_is_distinguishable_from_the_original_request() -> None:
    """Both untrusted, and not interchangeable: a reviewer objecting is answering work that exists.

    A model that cannot tell the two apart re-litigates the ticket instead of addressing the note.
    """
    ticket = Ticket(key="#218", title="t", comments=("me too",), review=("@dana: not like that",))
    by_path = {i.path: i.kind for i in ticket.as_context()}
    assert by_path["#218#comment"] == "ticket"
    assert by_path["#218#review"] == "review"


def test_a_ticket_with_no_review_is_exactly_what_it_was() -> None:
    assert [i.kind for i in Ticket(key="#1", title="t").as_context()] == ["ticket"]


# -- which change requests count as ours ---------------------------------------------------


def test_gate_review_1_only_branches_this_framework_wrote_are_gathered() -> None:
    """GATE-REVIEW-1, first half: the property that makes it safe to put the result in a prompt.

    Anyone can open a pull request that says "fixes #218". If that were enough to have its
    conversation gathered as a review of *our* change, an outsider would have a writable channel
    into an agent that holds write tools — reached by opening a pull request nobody merges.
    """
    ours = branch_for("fix", "run-abc", ticket="#218")
    assert is_run_branch_for(ours, "#218")
    assert is_run_branch_for(ours, "218"), "the leading # is not part of the key"

    assert not is_run_branch_for("feature/fixes-218", "#218"), "a stranger's branch is not ours"
    assert not is_run_branch_for("in-lockstep/fix/2180/r", "#218"), "218 is not a prefix match"
    assert not is_run_branch_for(branch_for("fix", "r", ticket="#9"), "#218")
    assert not is_run_branch_for("", "#218")
    assert not is_run_branch_for(ours, ""), "no key matches nothing, rather than everything"


# -- reading GitHub -------------------------------------------------------------------------


def _github(tmp_path: Path, *, prs: object = None, view: object = None, notes: object = None):
    scm = GitHubScm(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_json(*args: str):
        calls.append(args)
        if args[:2] == ("pr", "list"):
            return prs
        if args[:2] == ("pr", "view"):
            return view
        if isinstance(notes, Exception):
            raise notes
        return notes

    scm._gh_json = fake_json  # type: ignore[method-assign]
    return scm, calls


def test_changes_for_matches_the_branch_and_ignores_a_lookalike(tmp_path: Path) -> None:
    scm, _ = _github(
        tmp_path,
        prs=[
            {
                "number": 219,
                "url": "u/219",
                "title": "ours",
                "headRefName": branch_for("fix", "r1", ticket="#218"),
                "isDraft": False,
            },
            {
                "number": 300,
                "url": "u/300",
                "title": "fixes #218 honest",
                "headRefName": "dana/fix-218",
                "isDraft": False,
            },
        ],
    )
    changes = _run(scm.changes_for("#218"))
    assert [c.number for c in changes] == [219]


def test_changes_for_returns_the_newest_attempt_first(tmp_path: Path) -> None:
    """The most recent pull request is the one a reviewer was looking at, so a cap must not drop
    it in favour of whatever order the host happened to return."""
    scm, _ = _github(
        tmp_path,
        prs=[
            {"number": 219, "url": "u", "title": "", "headRefName": branch_for("fix", "a", ticket="#218")},
            {"number": 224, "url": "u", "title": "", "headRefName": branch_for("fix", "b", ticket="#218")},
            {"number": 221, "url": "u", "title": "", "headRefName": branch_for("fix", "c", ticket="#218")},
        ],
    )
    assert [c.number for c in _run(scm.changes_for("#218"))] == [224, 221, 219]


def test_changes_for_asks_only_for_open_ones(tmp_path: Path) -> None:
    """Closing a pull request is how a person says "start over, ignore that thread" — a control
    they already have, spelled with a button they already know."""
    scm, calls = _github(tmp_path, prs=[])
    _run(scm.changes_for("#218"))
    listed = next(c for c in calls if c[:2] == ("pr", "list"))
    assert "--state" in listed and listed[listed.index("--state") + 1] == "open"


def test_remarks_gathers_the_thread_the_verdict_and_the_line_note(tmp_path: Path) -> None:
    """Three places, because a reviewer says three different kinds of thing and the third — a note
    pinned to a file and a line — is the one that carries an actual instruction."""
    scm, _ = _github(
        tmp_path,
        view={
            "comments": [{"author": {"login": "dana"}, "body": "thanks"}],
            "reviews": [{"author": {"login": "sam"}, "body": "not yet", "state": "CHANGES_REQUESTED"}],
        },
        notes=[
            {
                "user": {"login": "sam"},
                "body": "iterate the entries",
                "path": "actions/save/action.yml",
                "line": 29,
            }
        ],
    )
    remarks = _run(scm.remarks(219))
    assert [r.kind for r in remarks] == ["comment", "review", "line"]

    rendered = [r.as_text(where="#219") for r in remarks]
    assert rendered[0].startswith("@dana commented on #219:")
    assert rendered[1].startswith("@sam requested changes on #219:")
    assert rendered[2].startswith("@sam reviewed actions/save/action.yml:29 on #219:")
    assert "iterate the entries" in rendered[2]


def test_an_empty_commented_review_is_not_a_remark(tmp_path: Path) -> None:
    """A bodyless COMMENTED review is the envelope around line notes and says nothing itself.
    A bodyless APPROVED is a verdict, and does."""
    scm, _ = _github(
        tmp_path,
        view={"comments": [], "reviews": [{"author": {"login": "a"}, "body": "", "state": "COMMENTED"}]},
        notes=[],
    )
    assert _run(scm.remarks(1)) == ()

    scm, _ = _github(
        tmp_path,
        view={"comments": [], "reviews": [{"author": {"login": "a"}, "body": "", "state": "APPROVED"}]},
        notes=[],
    )
    assert [r.as_text(where="#1") for r in _run(scm.remarks(1))] == ["@a approved on #1."]


def test_the_frameworks_own_marker_never_reaches_a_prompt(tmp_path: Path) -> None:
    """Its own review comment IS gathered — it is what the human was reading when they replied, and
    dropping it leaves "the second one is right" pointing at nothing. The invisible marker is not:
    it means something only to `upsert_comment`, and carrying it into a prompt would teach a model
    a token it must never emit.
    """
    scm, _ = _github(
        tmp_path,
        view={
            "comments": [
                {
                    "author": {"login": "github-actions"},
                    "body": (
                        "## in-lockstep review — security\n\nfindings\n\n<!-- in-lockstep:review:security -->"
                    ),
                }
            ],
            "reviews": [],
        },
        notes=[],
    )
    (remark,) = _run(scm.remarks(219))
    assert "in-lockstep review" in remark.body, "the findings are the point of gathering it"
    assert "<!--" not in remark.body
    assert "review:security" not in remark.body


def test_line_notes_that_went_outdated_keep_the_line_they_were_written_against(tmp_path: Path) -> None:
    """GitHub nulls `line` once a comment goes stale. Where it was written is what a reader wants
    either way, and a note with no location at all is an opinion rather than an instruction."""
    scm, _ = _github(
        tmp_path,
        view={"comments": [], "reviews": []},
        notes=[{"user": {"login": "sam"}, "body": "here", "path": "a.py", "line": None, "original_line": 12}],
    )
    (remark,) = _run(scm.remarks(1))
    assert remark.line == 12


def test_losing_the_line_notes_does_not_lose_the_thread(tmp_path: Path) -> None:
    """A token without `pull-requests: read` reaches the conversation through the issues endpoint
    and fails on the pulls one. Returning what was gathered beats returning none of it."""
    scm, _ = _github(
        tmp_path,
        view={"comments": [{"author": {"login": "dana"}, "body": "still wrong"}], "reviews": []},
        notes=RuntimeError("gh api failed: Resource not accessible by integration"),
    )
    assert [r.body for r in _run(scm.remarks(1))] == ["still wrong"]


# -- joining the two ------------------------------------------------------------------------


class _Host:
    def __init__(self, changes=(), remarks=()) -> None:
        self._changes, self._remarks = changes, remarks

    async def changes_for(self, ticket: str):
        if isinstance(self._changes, Exception):
            raise self._changes
        return self._changes

    async def remarks(self, number: int):
        if isinstance(self._remarks, Exception):
            raise self._remarks
        return self._remarks


class _Change:
    def __init__(self, number: int) -> None:
        self.number, self.branch = number, "b"


def test_with_review_puts_the_conversation_on_the_ticket() -> None:
    ticket = Ticket(key="#218", title="t")
    host = _Host(changes=(_Change(219),), remarks=(Remark(author="@dana", body="iterate them"),))
    reviewed, note = _run(with_review(ticket, host))
    assert reviewed.review == ("@dana commented on #219:\niterate them",)
    assert note == "review    1 remark(s) from #219"
    assert ticket.review == (), "the ticket handed in is not mutated"


def test_a_host_that_cannot_read_a_conversation_loses_the_context_not_the_run(tmp_path: Path) -> None:
    """Plain git has no pull requests. A run that can still read the issue is worth more than a run
    that refuses over context it would merely have been nice to have — and it says so, because
    context that silently did not arrive is discovered six rounds later."""
    ticket = Ticket(key="#218", title="t")
    reviewed, note = _run(with_review(ticket, GitLocal(tmp_path)))
    assert reviewed is ticket
    assert "unavailable" in note and "GitLocal" in note


def test_a_host_that_fails_to_answer_loses_the_context_not_the_run() -> None:
    ticket = Ticket(key="#218", title="t")
    reviewed, note = _run(with_review(ticket, _Host(changes=RuntimeError("gh: boom"))))
    assert reviewed is ticket
    assert note.startswith("review    unavailable") and "gh: boom" in note


def test_no_open_change_request_is_a_fact_worth_printing() -> None:
    _reviewed, note = _run(with_review(Ticket(key="#218", title="t"), _Host(changes=())))
    assert note == "review    none (no open change request for #218)"


def test_a_ticket_with_no_key_cannot_be_matched_to_a_branch() -> None:
    _reviewed, note = _run(with_review(Ticket(key="", title="t"), _Host(changes=(_Change(1),))))
    assert "skipped" in note


def test_the_cap_drops_the_oldest_remarks() -> None:
    """A thread reads newest last, so the cap has to bite at the old end: the sentence a reviewer
    wrote most recently is the one they expect acted on, and dropping the tail would discard it."""
    many = tuple(Remark(author="@d", body=f"note {i}") for i in range(MAX_REMARKS + 10))
    reviewed, _ = _run(with_review(Ticket(key="#1", title="t"), _Host(changes=(_Change(2),), remarks=many)))
    assert len(reviewed.review) == MAX_REMARKS
    assert f"note {MAX_REMARKS + 9}" in reviewed.review[-1]
    assert "note 0" not in " ".join(reviewed.review)
