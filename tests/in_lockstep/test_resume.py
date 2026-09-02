"""A rerun can continue instead of restarting.

A failed run keeps its change now, and nothing read it back — so the next `/implement` on the same
ticket began from a blank page having already been paid for a diff that existed. Meanwhile the
person reading that failure knew which of the 13 tests failed and could say so in a comment, but the
model had to rebuild the change from nothing in order to act on it.

Two properties carry the whole design, and both are tested here rather than assumed:

  * **the verdict travels with the diff.** Alone, a diff is an anchor a model defends. With the
    tests it failed, it is a debugging session — and that is exactly the feedback the green phase
    never gets.
  * **nobody resumes by accident.** Absent the flag, a run is what it was before.
"""

from __future__ import annotations

from typing import Any

import pytest

from in_lockstep.adapters.ai.attempts import MAX_ATTEMPT_CHARS, attempt_items
from in_lockstep.ai.context import Provenance
from in_lockstep.core.types import ChangeSet, FileChange
from in_lockstep.platform.chatops import DEFAULT_RESUME_DEPTH, ResumeDepthRefused, resume_depth


class _Verdict:
    def __init__(self, total=0, passed=0, failed=0, skipped=0, cases=()) -> None:
        self.total, self.passed, self.failed, self.skipped, self.cases = total, passed, failed, skipped, cases


class _Case:
    def __init__(self, id: str, outcome: str) -> None:
        self.id, self.outcome = id, outcome


def _changeset(*paths: str) -> ChangeSet:
    return ChangeSet(changes=tuple(FileChange(path=p, contents=f"# {p}\n") for p in paths))


# -- what an attempt looks like to the model ---------------------------------------------------


def test_an_attempt_arrives_as_the_diff_and_the_verdict_separately() -> None:
    """Two items, not one. The same separation `Ticket.as_context` draws between a ticket and a
    review remark, for the reason it states: they are not interchangeable to a reader, and a model
    that cannot tell "what you tried" from "what was asked" will do the wrong thing with both."""
    verdict = _Verdict(total=10, passed=9, failed=1, cases=(_Case("tests/x.py::test_a", "failed"),))
    items = attempt_items(((_changeset("src/a.py"), verdict),), key="#150")

    kinds = [i.kind for i in items]
    assert kinds == ["attempt", "verdict"]
    assert [i.path for i in items] == ["#150#attempt-1", "#150#verdict-1"]
    assert "src/a.py" in items[0].content
    assert "test_a" in items[1].content, "the failing test is named, not just counted"


def test_the_attempt_is_generated_not_untrusted() -> None:
    """It is the one piece of context in an implementing session that no stranger wrote. Tagging it
    `UNTRUSTED_EXTERNAL` would make a run resuming its own work read like a run reading a fork."""
    items = attempt_items(((_changeset("a.py"), None),))
    assert {i.provenance for i in items} == {Provenance.GENERATED}


def test_a_never_tested_attempt_does_not_read_as_a_pass() -> None:
    """ "No verdict" and "it passed" are the same shape of mistake the ledger spends its design
    refusing — and here it would tell a model its last attempt worked."""
    ((_diff, verdict),) = [(i.content, j.content) for i, j in [attempt_items(((_changeset("a.py"), None),))]]
    assert "not a pass" in verdict
    assert "never run" in verdict


def test_an_attempt_that_collected_nothing_does_not_read_as_a_pass() -> None:
    items = attempt_items(((_changeset("a.py"), _Verdict(total=0)),))
    assert "NOTHING WAS COLLECTED" in items[1].content
    assert "not a pass" in items[1].content


def test_a_green_attempt_says_so_plainly() -> None:
    items = attempt_items(((_changeset("a.py"), _Verdict(total=5, passed=5)),))
    assert "Everything that ran, passed" in items[1].content


# -- more than one attempt ----------------------------------------------------------------------


def test_attempts_are_numbered_from_the_newest_and_ordered_oldest_first() -> None:
    """Oldest first so the sequence reads as a history — an approach tried, abandoned and tried
    again in a worse form is visible across two diffs and invisible in one. Numbered from the
    newest so "attempt 1" always means the last thing that happened, whatever depth was asked for.
    """
    older, newer = _changeset("older.py"), _changeset("newer.py")
    items = attempt_items(((older, None), (newer, None)), key="#150")

    assert [i.path for i in items] == [
        "#150#attempt-2",
        "#150#verdict-2",
        "#150#attempt-1",
        "#150#verdict-1",
    ]
    assert "older.py" in items[0].content, "the oldest is rendered first"
    assert "newer.py" in items[2].content


def test_no_attempts_produces_no_items() -> None:
    assert attempt_items(()) == []


def test_one_enormous_file_cannot_cost_the_others_their_place() -> None:
    """The attempts are the part of a resumed prompt with no natural ceiling: a model that rewrote a
    thousand-line file staged a thousand lines."""
    big = ChangeSet(
        changes=(
            FileChange(path="big.py", contents="x" * (MAX_ATTEMPT_CHARS * 2)),
            FileChange(path="small.py", contents="# small\n"),
        )
    )
    (attempt, _verdict) = attempt_items(((big, None),))
    assert "truncated" in attempt.content
    assert "small.py" in attempt.content, "the later file is still named"


def test_a_deletion_is_described_rather_than_rendered_as_empty() -> None:
    changeset = ChangeSet(changes=(FileChange(path="gone.py", contents=None),))
    (attempt, _verdict) = attempt_items(((changeset, None),))
    assert "gone.py: deleted" in attempt.content


# -- what the comment asked for -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "depth"),
    [
        ("/implement", 0),
        ("/implement ", 0),
        ("/implement --resume", DEFAULT_RESUME_DEPTH),
        ("/implement --resume 2", 2),
        ("/implement --resume=3", 3),
        ("/implement --resume   4", 4),
        ("/implement --resume\n", 1),
    ],
)
def test_the_depth_is_read_from_the_comment(body: str, depth: int) -> None:
    """A DEPTH rather than a boolean, or `/implement --resume 2` is unreachable from chat-ops.

    Read here and never in a workflow expression: `test_workflow_triggers.py` holds a trigger to an
    allowlist of statements precisely so lifecycle logic cannot accumulate in YAML, and "which words
    mean resume" is lifecycle logic — the same rule that put ticket resolution in `ticket_for`.
    """
    assert resume_depth(body) == depth


def test_nobody_resumes_by_accident() -> None:
    """The default that matters. A model handed its own wrong diff will defend it, and sometimes the
    right answer is a clean start."""
    assert resume_depth("/implement please fix the resume behaviour") == 0
    assert resume_depth("") == 0


def test_a_depth_that_cannot_mean_anything_is_refused() -> None:
    """Refused rather than rounded: somebody who typed `--resume 0` meant something, and guessing
    between "don't resume" and "resume one" is how a tool teaches people not to trust it."""
    with pytest.raises(ResumeDepthRefused, match="not a number of attempts"):
        resume_depth("/implement --resume 0")


# -- the request, and the run that does not ask ---------------------------------------------------


def _package(request: Any) -> Any:
    from in_lockstep.adapters.ai.oneshot import Oneshot

    return Oneshot(lambda ctx: None)._session(object()).context(request)


class _Ticket:
    key = "#150"
    title = "a ticket"
    description = "do the thing"
    comments: tuple[str, ...] = ()
    review: tuple[str, ...] = ()

    def as_context(self):
        from in_lockstep.platform.tickets.base import Ticket

        return Ticket(key=self.key, title=self.title, description=self.description).as_context()


def test_a_run_that_does_not_resume_is_unchanged() -> None:
    """The whole opt-in promise: absent the flag, the package is what it always was."""
    from in_lockstep.adapters.ai.implement import Implement

    plain = _package(Implement(ticket=_Ticket()))
    assert [i.kind for i in plain.items] == ["ticket"]


def test_a_resumed_run_carries_the_attempt_and_its_verdict() -> None:
    from in_lockstep.adapters.ai.implement import Implement

    request = Implement(ticket=_Ticket(), attempts=((_changeset("src/a.py"), _Verdict(total=1, failed=1)),))
    kinds = [i.kind for i in _package(request).items]

    assert set(kinds) == {"ticket", "attempt", "verdict"}


def test_a_tight_budget_drops_the_attempt_before_the_ticket_and_the_verdict_last() -> None:
    """A session that evicted the request would be implementing nothing in particular, and one that
    kept the diff while dropping the failures would have the anchor without the reason — which is
    the exact shape this feature exists to avoid."""
    from in_lockstep.adapters.ai.implement import Implement

    huge = ChangeSet(changes=(FileChange(path="big.py", contents="x" * 8_000),))
    request = Implement(
        ticket=_Ticket(),
        attempts=((huge, _Verdict(total=1, failed=1, cases=(_Case("t::a", "failed"),))),),
        token_budget=400,
    )
    kinds = [i.kind for i in _package(request).items]

    assert "ticket" in kinds, "the request survives"
    assert "attempt" not in kinds, "the diff is what does not fit"
    assert "verdict" in kinds, "and the failures are what is kept"
