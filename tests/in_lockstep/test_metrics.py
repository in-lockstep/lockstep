"""What the ledger adds up to, and what it refuses to claim.

The arithmetic is the easy half. The half worth testing is the discipline around it: a number
averaged over whatever records happened to carry the field, printed as though it described all of
them, is how a dashboard becomes quietly wrong — and quietly wrong is worse than missing, because
somebody makes a decision with it.

So most of these are about denominators, about `blocked` not being a failure, and about the module
refusing to turn a `True` into a duration.
"""

from __future__ import annotations

from typing import Any

import pytest

from in_lockstep import metrics
from in_lockstep.metrics import Measured, as_html, as_text, build, delivery


def _record(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "r1",
        "kind": "review",
        "status": "succeeded",
        "decided": True,
        "cost_usd": 0.02,
        "wall_seconds": 4.0,
        "tokens": 1000,
    }
    base.update(over)
    return base


# -- absent is not zero -------------------------------------------------------------------------


def test_a_field_no_record_carries_renders_as_a_dash_with_its_denominator() -> None:
    """The rule this whole module exists to keep. A `0` here would be a claim that somebody
    measured something and it came to nothing."""
    report = build([_record(), _record()])
    assert report.turns_by_strategy == []
    rendered = "\n".join(as_text(report))
    assert "—" in rendered
    assert Measured(None, 0, 2).render() == "—  (0 of 2)"


def test_a_partial_field_says_how_many_records_it_came_from() -> None:
    """One of ten is still a useful number and a dangerous one to print bare.

    The denominator has to be every run of that strategy, not every run that recorded a turn count
    — otherwise `of` and `total` are the same number by construction, `complete` is always True,
    and the label that exists to warn you is never printed.
    """
    records = [_record(kind="implement", strategy="implement/tdd", turns=6)]
    records += [_record(kind="implement", strategy="implement/tdd") for _ in range(9)]
    report = build(records)

    ((name, turns),) = report.turns_by_strategy
    assert name == "implement/tdd"
    assert turns.value == 6.0
    assert not turns.complete
    assert "from 1 of 10" in turns.render()


def test_a_verb_that_never_records_turns_is_left_out_rather_than_dashed() -> None:
    """Reviews do not have turns and never will. A row per verb saying so is noise, not honesty —
    the dash is for a number somebody might have expected to see."""
    report = build([_record(kind="review") for _ in range(3)])
    assert report.turns_by_strategy == []


def test_a_complete_field_does_not_carry_a_denominator_it_does_not_need() -> None:
    """The label is for a number that might mislead. On a complete one it is noise."""
    report = build([_record(), _record()])
    assert report.cost_total.complete
    assert "of" not in report.cost_total.render()


# -- the traps ----------------------------------------------------------------------------------


def test_a_boolean_is_never_read_as_a_number() -> None:
    """`True` is an `int` in Python. A `decided: true` counted as one second of wall time is the
    kind of wrong that survives review, because a plausible number looks like a measured one."""
    report = build([_record(wall_seconds=True, cost_usd=False, tokens=True)])
    assert report.seconds_median.value is None, "a bool is not a duration"
    assert report.cost_total.value is None, "a bool is not an amount"
    assert report.tokens_total.value is None


def test_blocked_is_counted_apart_and_is_never_a_failure() -> None:
    """A run a budget ceiling or an approval gate stopped is the control working. Folding it into
    a failure rate makes every ceiling look like a bug, and the number people would act on is the
    one that says the framework is broken."""
    report = build([_record(status="blocked"), _record(), _record(status="failed")])
    assert report.blocked == 1
    assert report.failure_rate.value == pytest.approx(1 / 3), "only the failed one"


def test_a_run_that_settled_nothing_is_its_own_number() -> None:
    """`decided` is separate from `status` on purpose — a run can succeed and settle nothing — so
    the honesty metric has to be separate too."""
    report = build([_record(decided=False), _record(decided=True)])
    assert report.undecided_rate.value == pytest.approx(0.5)
    assert report.failure_rate.value == 0.0


def test_injection_signals_are_counted_apart_from_findings() -> None:
    """One is a fact about the code and the other is a fact about somebody trying to talk to the
    model through it. A total that mixes them hides both."""
    report = build(
        [
            _record(
                findings={
                    "count": 3,
                    "items": [
                        {"id": "review.security"},
                        {"id": "review.security"},
                        {"id": "injection.exfil_token_names"},
                    ],
                }
            )
        ]
    )
    assert report.injection_signals == 1
    assert dict(report.top_findings) == {"review.security": 2}


def test_p90_is_a_duration_some_run_actually_took() -> None:
    """Nearest-rank, not interpolation. With four measurements an interpolated p90 is a number no
    run took, and "the slowest run was 40s" should be a claim somebody can check."""
    report = build([_record(wall_seconds=s) for s in (1.0, 2.0, 3.0, 40.0)])
    assert report.seconds_p90.value in (1.0, 2.0, 3.0, 40.0)
    assert report.seconds_p90.value == 40.0


def test_a_record_missing_the_grouping_key_is_named_rather_than_dropped() -> None:
    """ "Nine runs, four from something that did not say what it was" is a fact about the ledger
    worth seeing. Dropping them silently shrinks the denominator nobody is watching."""
    rows = {g.name: g.runs for g in build([_record(), _record(kind=None)]).runs_by_kind}
    assert rows == {"review": 1, "(unrecorded)": 1}


def test_an_empty_ledger_says_so_rather_than_reporting_zeroes() -> None:
    report = build([])
    assert report.records == 0
    assert "no records yet" in as_text(report)[0]


# -- delivery, which is somebody else's evidence ------------------------------------------------


def test_delivery_measures_only_what_the_host_timed() -> None:
    pulls = [
        {"created_at": "2026-09-01T00:00:00Z", "merged_at": "2026-09-01T02:00:00Z"},
        {"created_at": "2026-09-01T00:00:00Z", "merged_at": None},
    ]
    issues = [
        {
            "created_at": "2026-09-01T00:00:00Z",
            "closed_at": "2026-09-02T00:00:00Z",
            "first_response_at": "2026-09-01T01:00:00Z",
        },
        {"created_at": "2026-09-01T00:00:00Z", "closed_at": None, "first_response_at": None},
    ]
    result = delivery(pulls, issues)

    assert (result.pulls, result.merged) == (2, 1)
    assert result.hours_to_merge.value == pytest.approx(2.0)
    assert result.hours_to_merge.of == 1 and result.hours_to_merge.total == 2
    assert result.hours_to_close.value == pytest.approx(24.0)
    assert result.hours_to_first_response.value == pytest.approx(1.0)
    assert result.merge_rate == pytest.approx(0.5)


def test_an_unanswered_issue_lowers_the_denominator_rather_than_the_median() -> None:
    """Counting "never answered" as zero hours would report the fastest possible response time for
    the issue nobody looked at, which is the exact inversion of the truth."""
    result = delivery([], [{"created_at": "2026-09-01T00:00:00Z", "first_response_at": None}])
    assert result.hours_to_first_response.value is None
    assert result.hours_to_first_response.total == 1


def test_delivery_survives_a_timestamp_it_cannot_read() -> None:
    result = delivery([{"created_at": "not a date", "merged_at": "2026-09-01T00:00:00Z"}], [])
    assert result.pulls == 1 and result.merged == 1
    assert result.hours_to_merge.value is None


# -- the page -----------------------------------------------------------------------------------


def test_the_page_fetches_nothing_from_anywhere() -> None:
    """One self-contained file. A report that needed a CDN would stop rendering the day the CDN
    does, and this is a document about evidence — it has to render on a laptop with no network,
    years from now, out of somebody's email."""
    html = as_html(build([_record(findings={"count": 1, "items": [{"id": "review.security"}]})]))
    for forbidden in ("http://", "https://", "<script", "@import", "src="):
        assert forbidden not in html, f"the page reaches for {forbidden!r}"


def test_the_page_charts_degrade_to_a_stated_note_rather_than_an_empty_axis() -> None:
    """An axis with nothing on it reads as "zero runs". A sentence saying there is not enough data
    yet reads as what it is."""
    html = as_html(build([_record(ts="2026-09-01T00:00:00+00:00")]))
    assert "needs at least two weeks" in html
    assert "<polyline" not in html


def test_the_page_draws_the_line_once_there_are_two_weeks() -> None:
    html = as_html(
        build(
            [
                _record(ts="2026-08-20T00:00:00+00:00"),
                _record(ts="2026-09-01T00:00:00+00:00"),
            ]
        )
    )
    assert "<polyline" in html and "<svg" in html


def test_the_page_uses_the_published_sites_palette() -> None:
    """So a report looks like the rest of the project rather than like a different product."""
    html = as_html(build([_record()]))
    assert metrics.INK in html and metrics.MINT in html


def test_finding_text_cannot_close_a_tag() -> None:
    """Finding messages and run ids are model- and ticket-derived text. It reaches this page, so
    it is escaped here — the page is written to disk and opened in a browser."""
    html = as_html(build([_record(findings={"count": 1, "items": [{"id": "</style><script>x"}]})]))
    assert "<script>x" not in html
    assert "&lt;/style&gt;" in html


def test_the_delivery_section_appears_only_when_the_host_was_asked() -> None:
    """It is the one part of the page this framework did not write itself, so its absence has to be
    an absence rather than a row of zeroes."""
    from dataclasses import replace

    plain = build([_record()])
    assert "Delivery" not in as_html(plain)

    asked = replace(plain, delivery=delivery([{"created_at": "a", "merged_at": "b"}], []))
    page = as_html(asked)
    assert "Delivery" in page
    assert "Asked of the host" in page, "labelled as somebody else's evidence"


# -- attempts per ticket ------------------------------------------------------------------------


def test_a_ticket_in_four_records_shows_four_runs_and_summed_cost() -> None:
    """The core metric: how many runs one ticket actually took and what they cost."""
    records = [
        _record(ticket="#139", cost_usd=30.0),
        _record(ticket="#139", cost_usd=25.0),
        _record(ticket="#139", cost_usd=30.0),
        _record(ticket="#139", cost_usd=24.73),
    ]
    report = build(records)
    assert len(report.attempts_by_ticket) == 1
    name, runs, cost = report.attempts_by_ticket[0]
    assert name == "#139"
    assert runs == 4
    assert cost.value == pytest.approx(109.73)


def test_both_ticket_spellings_count_as_the_same_ticket() -> None:
    """A record carrying `args: {"ticket": "#7"}` and one carrying `ticket: "#7"` are the same
    ticket, not two rows."""
    records = [
        _record(args={"ticket": "#7"}, cost_usd=1.0),
        _record(ticket="#7", cost_usd=2.0),
    ]
    report = build(records)
    assert len(report.attempts_by_ticket) == 1
    name, runs, cost = report.attempts_by_ticket[0]
    assert name == "#7"
    assert runs == 2
    assert cost.value == pytest.approx(3.0)


def test_a_record_with_no_ticket_is_skipped_not_a_row_named_empty() -> None:
    """A record with no ticket in either place is not about a ticket and must not become a row
    named `""`."""
    records = [_record(), _record(ticket="#10", cost_usd=1.0)]
    report = build(records)
    assert len(report.attempts_by_ticket) == 1
    assert report.attempts_by_ticket[0][0] == "#10"
    # No row for the ticketless record
    assert all(name != "" for name, _, _ in report.attempts_by_ticket)


def test_a_ticket_with_no_cost_renders_as_a_dash_with_denominator() -> None:
    """A ticket whose runs never recorded a cost renders as a dash carrying its denominator, never
    $0.0000 — the rule the whole module exists for."""
    records = [
        _record(ticket="#42", cost_usd=None),
        _record(ticket="#42", cost_usd=None),
    ]
    report = build(records)
    assert len(report.attempts_by_ticket) == 1
    name, runs, cost = report.attempts_by_ticket[0]
    assert name == "#42"
    assert runs == 2
    assert cost.value is None
    assert cost.total == 2
    # When rendered through _amount, should be a bare dash, not $0.0000
    rendered = "\n".join(as_text(report))
    assert "$0.0000" not in rendered


def test_attempts_ordered_most_attempted_first_then_by_ticket_name() -> None:
    """Rows are ordered most-attempted first, ties broken by ticket name, so the output is stable."""
    records = [
        _record(ticket="#B", cost_usd=1.0),
        _record(ticket="#A", cost_usd=1.0),
        _record(ticket="#C", cost_usd=1.0),
        _record(ticket="#C", cost_usd=1.0),
        _record(ticket="#A", cost_usd=1.0),
    ]
    report = build(records)
    names = [name for name, _, _ in report.attempts_by_ticket]
    # #A and #C both have 2 runs; #A sorts before #C alphabetically. #B has 1 run.
    assert names == ["#A", "#C", "#B"]


def test_attempts_empty_on_empty_ledger_and_report_still_renders() -> None:
    """An empty ledger should yield an empty list and the report should still render."""
    report = build([])
    assert report.attempts_by_ticket == []
    text = as_text(report)
    assert len(text) > 0  # "no records yet" message


def test_attempts_section_appears_in_text_output() -> None:
    """The 'attempts per ticket' section must appear in as_text() output."""
    records = [_record(ticket="#99", cost_usd=5.0)]
    rendered = "\n".join(as_text(build(records)))
    assert "attempts per ticket" in rendered
    assert "#99" in rendered
    assert "1 run(s)" in rendered


def test_attempts_bar_row_appears_in_html_output() -> None:
    """A bar row for attempts must appear in as_html() output."""
    records = [
        _record(ticket="#50", cost_usd=5.0),
        _record(ticket="#50", cost_usd=5.0),
    ]
    html = as_html(build(records))
    assert "#50" in html
    # The _bars helper creates <div class=bar> elements
    assert "Attempts per ticket" in html or "attempts per ticket" in html.lower()
