"""The retry loop remembers what the last attempt tried.

`lockstep.max_attempts` is 3, and a failed `/fix` opens an `ai-generated` ticket an agent picks up
and tries again. Each of those attempts started from nothing: `escalate` carried a summary of the
failure — "Tests failed: 13 of 1644" — and never the diff. So attempt 2 rebuilt the change from the
ticket text and could reproduce attempt 1's mistake exactly, and attempt 3 could do it again. Three
paid runs, no memory between them, unattended by design.

This is the most expensive place in the framework for that to be true, because it is the one loop
that repeats on purpose with nobody watching.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from in_lockstep.platform.propose import (
    AI_GENERATED,
    ATTEMPT_PREFIX,
    RESUME_MARKER,
    escalate,
    resumes_automatically,
    resumes_from,
)


class _Source:
    def __init__(self, key: str = "#42", labels: tuple[str, ...] = ()) -> None:
        self.key, self.labels = key, labels


class _Tickets:
    """Records what was created and what was said, and nothing else."""

    def __init__(self) -> None:
        self.created: list[Any] = []
        self.said: list[str] = []

    async def create(self, draft: Any) -> Any:
        self.created.append(draft)
        return _Source(key=f"#{100 + len(self.created)}", labels=tuple(draft.labels))

    async def comment(self, ticket: Any, body: str) -> None:
        self.said.append(body)


def _escalate(source: _Source, tickets: _Tickets, *, max_attempts: int = 3) -> Any:
    return asyncio.run(escalate(tickets, source, "Tests failed: 13 of 1644.", max_attempts=max_attempts))


# -- the escalated ticket carries what produced it ----------------------------------------------


def test_an_escalated_ticket_names_the_work_its_attempt_belongs_to() -> None:
    """Without this the next attempt has the failure summary and no way to reach the diff."""
    tickets = _Tickets()
    _escalate(_Source(key="#42"), tickets)

    (draft,) = tickets.created
    assert resumes_from(draft.description) == "#42"


def test_it_points_at_the_original_work_not_at_the_escalation_chain() -> None:
    """Attempt 3 resumes from what attempt 2 staged for the same underlying bug. A chain of
    escalation tickets is not where anybody filed work."""
    tickets = _Tickets()
    first = _escalate(_Source(key="#42"), tickets)
    _escalate(_Source(key=first.key, labels=first.labels), tickets)

    assert [resumes_from(d.description) for d in tickets.created] == ["#42", first.key]


def test_the_failure_summary_is_still_carried() -> None:
    """The marker is added beside what was already there, not instead of it — a person reading the
    ticket still learns why it exists without following a reference."""
    tickets = _Tickets()
    _escalate(_Source(), tickets)
    (draft,) = tickets.created
    assert "Tests failed: 13 of 1644." in draft.description


def test_a_description_with_no_marker_resumes_from_nothing() -> None:
    """A human-filed bug is the ordinary case, and it must start clean rather than fail."""
    assert resumes_from("Just a bug report.") == ""
    assert resumes_from("") == ""


def test_the_marker_is_read_the_way_it_is_written() -> None:
    """Its own function for the reason `branch_key` is: two spellings of one format is one of them
    drifting, and the failure is silent — a retry that never finds the attempt it should continue."""
    assert resumes_from(f"words\n{RESUME_MARKER} #7\nmore") == "#7"
    assert resumes_from(f"{RESUME_MARKER}") == "", "a marker with no key names nothing"


# -- the divergence from /implement, which is the point -----------------------------------------


def test_a_ticket_this_loop_filed_resumes_without_anybody_asking() -> None:
    """The deliberate inversion of `/implement`'s opt-in rule.

    A person resuming is opt-in because a model handed its own wrong diff will defend it, and a
    clean start is sometimes right — a judgement only a human makes. Nobody is watching an
    `ai-generated` run, so there is no one to type the flag, and the alternative to resuming is
    provably repeating the same failure at full price.
    """
    assert resumes_automatically((AI_GENERATED, f"{ATTEMPT_PREFIX}1")) is True
    assert resumes_automatically((AI_GENERATED, f"{ATTEMPT_PREFIX}2")) is True


def test_a_human_filed_ticket_does_not_resume_by_itself() -> None:
    """The `/implement` rule is unchanged for the attended path: a person asks, or it starts clean."""
    assert resumes_automatically(()) is False
    assert resumes_automatically(("bug", AI_GENERATED)) is False, "labelled, but no attempt behind it"


def test_the_reason_for_the_divergence_is_written_down() -> None:
    """Two paths with opposite defaults and no recorded argument is how one of them later gets
    "fixed" to match the other. This asserts the argument exists where somebody will read it."""
    assert resumes_automatically.__doc__ is not None
    doc = resumes_automatically.__doc__
    assert "DIVERGES" in doc and "opt-in" in doc


# -- the cap still bounds the loop ---------------------------------------------------------------


def test_the_attempt_cap_still_stops_it_and_asks_for_a_person() -> None:
    """This makes each attempt better informed, not more numerous."""
    tickets = _Tickets()
    opened = _escalate(_Source(labels=(f"{ATTEMPT_PREFIX}3",)), tickets, max_attempts=3)

    assert opened is None
    assert tickets.created == [], "nothing new carries the label, so the trigger stops firing"
    assert "human is needed" in tickets.said[0]


@pytest.mark.parametrize("attempt", [0, 1, 2])
def test_below_the_cap_it_keeps_going_and_keeps_counting(attempt: int) -> None:
    tickets = _Tickets()
    labels = (f"{ATTEMPT_PREFIX}{attempt}",) if attempt else ()
    _escalate(_Source(labels=labels), tickets, max_attempts=3)

    (draft,) = tickets.created
    assert f"{ATTEMPT_PREFIX}{attempt + 1}" in draft.labels
    assert AI_GENERATED in draft.labels
