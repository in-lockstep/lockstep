"""What the ledger adds up to, once there is enough of it to add up.

`report` has always answered one question — runs, failures and spend, grouped — and that is the
question you ask in week one. The questions you ask in month six are different: is this getting
faster, is it getting cheaper, does it decide anything, who is actually using it, and what does it
keep finding. Those were all answerable from records the framework already writes, and nothing
answered them.

Everything here is computed from the ledger, which means it needs no network, no token and no
service, and it describes runs this repository actually did rather than a vendor's idea of
activity. The one thing it cannot see is the pull request and issue timings a host owns; those come
from `platform.scm` and arrive separately, because evidence the framework wrote and evidence it
asked somebody else for should not be added together without saying which is which.

## Absent is not zero, and the denominator travels with the number

This is the ledger's own rule and the reason `Measured` exists rather than a bare float. A record
written before `ts` existed cannot be placed in a week; a review has no `turns`; a repository that
never ran unattended has no approvals. Averaging over what happens to be present and printing the
result as though it described everything is how a dashboard comes to be quietly wrong — and a
metric that is quietly wrong is worse than a missing one, because somebody makes a decision with it.

So every number carries how many records it came from. `mean(turns)` over 3 of 200 records renders
as `— (3 of 200)` and not as a number somebody might put in a slide.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeGuard

#: Statuses the ledger writes for a run that did not produce what it was asked for. `blocked` is
#: deliberately not one of them: a run a control stopped is the control working, and counting it as
#: a failure would make every budget ceiling look like a bug.
FAILED = ("failed", "errored")

#: Every status a record can honestly carry. A record outside this set has no verdict the report
#: can count, and it is counted as exactly that rather than as "not failed": schema-4 workflow
#: records stamped `"completed"` read as 0% failed over eleven red selfchecks (#166).
VERDICTS = ("succeeded", "failed", "errored", "blocked")

#: How many entries a "what does it keep finding" list shows. Long enough to see a pattern, short
#: enough that the tail does not read as though it mattered.
TOP_N = 8

#: Below this, a finding that came back is a coincidence. Five distinct runs, not five
#: occurrences: one review that reports the same thing five times is one review.
MIN_TREND_RUNS = 5

#: And below this it is a week, not a trend. A finding confined to one week is usually a fact
#: about that week — a branch everybody was working on, a dependency that was briefly broken.
MIN_TREND_WEEKS = 2


@dataclass(frozen=True)
class Measured:
    """A number and the evidence behind it. `value is None` means nobody measured it."""

    value: float | None
    of: int = 0
    total: int = 0

    @property
    def complete(self) -> bool:
        """Whether every record considered carried the field. A partial number is still useful
        and has to be labelled, which is what `of`/`total` are for."""
        return self.of == self.total and self.total > 0

    def render(self, unit: str = "", places: int = 1) -> str:
        if self.value is None:
            return f"—  ({self.of} of {self.total})" if self.total else "—"
        shown = f"{self.value:,.{places}f}{unit}" if places else f"{int(self.value):,}{unit}"
        return shown if self.complete else f"{shown}  (from {self.of} of {self.total})"


@dataclass(frozen=True)
class Group:
    """One row of a breakdown: a name, and what the runs under it add up to."""

    name: str
    runs: int
    failures: int
    undecided: int
    cost: Measured
    seconds: Measured
    #: Runs in this row whose status is a verdict. The failure rate's denominator; `runs` is not,
    #: because a record with no verdict is not evidence of success.
    judged: int = 0

    @property
    def failure_rate(self) -> float | None:
        return self.failures / self.judged if self.judged else None


@dataclass(frozen=True)
class Report:
    """Everything the ledger can say about itself."""

    records: int
    window: tuple[str, str] | None = None

    runs_by_kind: list[Group] = field(default_factory=list)
    runs_by_model: list[Group] = field(default_factory=list)
    by_week: list[tuple[str, int, float]] = field(default_factory=list)

    failure_rate: Measured = Measured(None)
    undecided_rate: Measured = Measured(None)
    blocked: int = 0
    #: Records whose status is not a verdict. Never folded into the failure rate's denominator.
    unclassified: int = 0
    top_reasons: list[tuple[str, int]] = field(default_factory=list)

    cost_total: Measured = Measured(None)
    cost_per_run: Measured = Measured(None)
    tokens_total: Measured = Measured(None)
    billed_share: Measured = Measured(None)
    #: What share of the input a run sent was served from cache rather than paid for at full rate.
    #: The number that says whether prompt caching is working, which `tokens` alone cannot: it
    #: counts input plus output and excludes the cache entirely, so a working cache looks exactly
    #: like a smaller task.
    cached_share: Measured = Measured(None)

    seconds_median: Measured = Measured(None)
    seconds_p90: Measured = Measured(None)
    turns_by_strategy: list[tuple[str, Measured]] = field(default_factory=list)

    findings_total: int = 0
    findings_per_run: Measured = Measured(None)
    top_findings: list[tuple[str, int]] = field(default_factory=list)
    injection_signals: int = 0

    people: list[tuple[str, int]] = field(default_factory=list)
    unattended: Measured = Measured(None)
    against_dirty_tree: Measured = Measured(None)

    #: How many runs each ticket took and what they cost. Answers "this one ticket took four
    #: attempts and $110", which per-run averages actively hide.
    attempts_by_ticket: list[tuple[str, int, Measured]] = field(default_factory=list)

    #: Only present when `--scm` asked the host, because it is the one section here that is not
    #: computed from evidence this framework wrote. Kept as its own object rather than folded in
    #: beside the ledger numbers: what we recorded and what we asked somebody else for should not
    #: be added together without the reader being able to see which is which.
    delivery: Delivery | None = None


@dataclass(frozen=True)
class Delivery:
    """What happened to the work after the run finished — the half the ledger cannot see.

    A run ends when the pull request is opened. Whether anybody merged it, and how long an issue
    this framework filed stayed open, are facts the host owns, and they are the ones that answer
    "is this actually delivering anything" rather than "is this actually running".
    """

    pulls: int = 0
    merged: int = 0
    hours_to_merge: Measured = Measured(None)
    issues: int = 0
    closed: int = 0
    hours_to_close: Measured = Measured(None)
    hours_to_first_response: Measured = Measured(None)

    @property
    def merge_rate(self) -> float | None:
        return self.merged / self.pulls if self.pulls else None


# -- reading one field off many records -------------------------------------------------------


@dataclass(frozen=True)
class Trend:
    """One finding id, and everything the ledger can honestly say about how often it comes back.

    Every count here has a denominator beside it, and two of them are separate on purpose.
    `occurrences` and `runs` differ whenever one run reports the same thing twice, which is the
    normal case for a review: this repository's own ledger holds `review.security` 26 times across
    13 runs, and reporting 26 as the recurrence would say it came back twice as often as it did.

    `weeks` is counted over `dated_runs` only. Records written before `ts` existed cannot be placed
    in any week, and there is no honest way to place them: week zero is a week nobody ran in, and
    dropping them silently reports a ledger with fewer runs than it has. So they are counted as
    `undated_runs` and travel beside the number they are missing from.
    """

    finding: str
    #: Items, the number a "what it keeps finding" list already counts.
    occurrences: int
    #: Distinct runs. The number a threshold should read.
    runs: int
    #: Distinct ISO weeks among the dated runs. Zero means nobody could place any of them.
    weeks: int
    #: The denominator for `weeks`.
    dated_runs: int
    #: Runs carrying no `ts`. Absent, not week zero.
    undated_runs: int
    #: Runs where at least one occurrence stopped the run. Per run, because two ceilings hit in one
    #: run is one run stopped.
    blocking_runs: int
    #: The workflows it was found under, so a reader can see whether it is one lens or several.
    workflows: tuple[str, ...]
    #: Records walked to produce this. The denominator the whole census shares.
    considered: int
    #: Whether it cleared both thresholds. Carried rather than filtered on — see `recurring`.
    qualifies: bool


def _numbers(records: list[dict[str, Any]], key: str) -> list[float]:
    """Every numeric value of `key`, skipping records that do not carry it.

    `bool` is excluded on purpose: `True` is an `int` in Python, and a `decided: true` counted as
    a duration of one second is the kind of wrong that survives review because it looks plausible.
    """
    out: list[float] = []
    for record in records:
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(float(value))
    return out


def _measure(values: list[float], total: int, how: str = "sum") -> Measured:
    if not values:
        return Measured(None, 0, total)
    if how == "sum":
        return Measured(sum(values), len(values), total)
    if how == "mean":
        return Measured(statistics.fmean(values), len(values), total)
    if how == "median":
        return Measured(statistics.median(values), len(values), total)
    if how == "p90":
        ordered = sorted(values)
        # Nearest-rank rather than interpolation. With four measurements, an interpolated p90 is a
        # number that no run took, and "the slowest run was 40s" is a claim somebody can check.
        return Measured(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], len(values), total)
    raise ValueError(f"unknown aggregate {how!r}")


def _share(records: list[dict[str, Any]], predicate: Any) -> Measured:
    """The fraction of records matching, as a rate. Every record can be tested, so it is complete."""
    if not records:
        return Measured(None, 0, 0)
    hits = sum(1 for r in records if predicate(r))
    return Measured(hits / len(records), len(records), len(records))


def _group(records: list[dict[str, Any]], by: str) -> list[Group]:
    """Breakdown rows, biggest first. A record missing the key is grouped under its absence rather
    than dropped: "nine runs, four of them from something that did not say what it was" is a fact
    about the ledger worth seeing."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        buckets.setdefault(str(record.get(by) or "(unrecorded)"), []).append(record)
    rows = [
        Group(
            name=name,
            runs=len(group),
            failures=sum(1 for r in group if r.get("status") in FAILED),
            undecided=sum(1 for r in group if r.get("status") in VERDICTS and r.get("decided") is False),
            cost=_measure(_numbers(group, "cost_usd"), len(group)),
            seconds=_measure(_numbers(group, "wall_seconds"), len(group), "median"),
            judged=sum(1 for r in group if r.get("status") in VERDICTS),
        )
        for name, group in buckets.items()
    ]
    rows.sort(key=lambda g: (-g.runs, g.name))
    return rows


def _weeks(records: list[dict[str, Any]]) -> list[tuple[str, int, float]]:
    """Runs and spend per ISO week, oldest first. Records with no `ts` cannot be placed and are
    left out — the same rule `spent_in_window` follows, for the same reason."""
    buckets: dict[str, tuple[int, float]] = {}
    for record in records:
        stamp = record.get("ts")
        if not isinstance(stamp, str):
            continue
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        year, week, _ = when.isocalendar()
        label = f"{year}-W{week:02d}"
        runs, cost = buckets.get(label, (0, 0.0))
        value = record.get("cost_usd")
        spent = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
        buckets[label] = (runs + 1, cost + spent)
    return [(label, runs, cost) for label, (runs, cost) in sorted(buckets.items())]


def _findings(records: list[dict[str, Any]]) -> tuple[int, list[tuple[str, int]], int]:
    """How many findings, which ones recur, and how many were the injection scanner speaking.

    Injection signals are counted apart from review findings for the reason `report.py` already
    separates them on a pull request: one is a fact about the code, the other is a fact about
    somebody trying to talk to the model through it, and a total that mixes them hides both.
    """
    total = 0
    ids: Counter[str] = Counter()
    injections = 0
    for record in records:
        found = record.get("findings")
        if not isinstance(found, dict):
            continue
        count = found.get("count")
        if isinstance(count, int) and not isinstance(count, bool):
            total += count
        for item in found.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("id", ""))
            if not name:
                continue
            if name.startswith("injection."):
                injections += 1
            else:
                ids[name] += 1
    return total, ids.most_common(TOP_N), injections


def _cached(records: list[dict[str, Any]]) -> Measured:
    """Cache reads as a share of all input, over the records that carry the breakdown.

    Per record rather than as one ratio of two sums, so a single enormous run cannot speak for the
    rest — the same reason `_measure` takes a mean of measurements rather than dividing totals.

    A record with no `cache_read_tokens` field is not a record with zero of them: it was written
    before the breakdown was recorded, and counting it as an uncached run would drag the number
    toward "caching is not working" using evidence that says nothing either way.
    """
    shares: list[float] = []
    for record in records:
        read, fresh = record.get("cache_read_tokens"), record.get("input_tokens")
        if not _is_number(read) or not _is_number(fresh):
            continue
        total = float(read) + float(fresh)
        if total:
            shares.append(float(read) / total)
    return _measure(shares, len(records), "mean")


def _is_number(value: Any) -> TypeGuard[float]:
    """Numeric and not a bool, the trap `_numbers` documents at length.

    A `TypeGuard` rather than a plain predicate so a caller's `float(value)` narrows — otherwise
    the choice is between repeating the bool check at every call site and silencing the type
    checker, and the repeated version is how one of the copies eventually loses the `bool` clause.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _people(records: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Who asked for runs. `approval.by` is what a person claimed; `ci_actor` is what the host
    said. Both are counted, and a run carrying neither is nobody's rather than anonymous."""
    who: Counter[str] = Counter()
    for record in records:
        approval = record.get("approval")
        name = ""
        if isinstance(approval, dict):
            name = str(approval.get("by") or "")
        if not name:
            name = str(record.get("ci_actor") or "")
        if name:
            who[name] += 1
    return who.most_common(TOP_N)


def _ticket_of(record: dict[str, Any]) -> str:
    """The ticket a record is about, using the same two-spelling lookup `cli.py` uses.

    `args.ticket` is written by `@workflow`-dispatched runs; top-level `ticket` by the implementing
    verbs. Empty string means neither was present — the caller decides whether to skip it.
    """
    return str((record.get("args") or {}).get("ticket", record.get("ticket", "")))


def _attempts(records: list[dict[str, Any]]) -> list[tuple[str, int, Measured]]:
    """(ticket, runs, total cost), most-attempted first.

    Groups records by ticket and sums their cost. A record with no ticket in either place is
    skipped entirely — it is not a ticket named "". Cost is a `Measured` built the same way
    `_group` builds it, so a ticket whose runs never recorded a cost renders as a dash with its
    denominator rather than as `$0.0000`.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        ticket = _ticket_of(record)
        if not ticket:
            continue
        buckets.setdefault(ticket, []).append(record)
    rows = [
        (ticket, len(group), _measure(_numbers(group, "cost_usd"), len(group)))
        for ticket, group in buckets.items()
    ]
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows


def build(records: list[dict[str, Any]]) -> Report:
    """Everything above, over one list of ledger records."""
    total = len(records)
    if not total:
        return Report(records=0)

    stamps = sorted(str(r["ts"]) for r in records if isinstance(r.get("ts"), str))
    reasons = Counter(str(r["reason"]) for r in records if r.get("reason"))
    findings_total, top_findings, injections = _findings(records)
    # The failure rate is a share of the records that carry a verdict. A record that carries none
    # is not evidence of success, and putting it in the denominator says it is.
    judged = [r for r in records if r.get("status") in VERDICTS]

    return Report(
        records=total,
        window=(stamps[0], stamps[-1]) if stamps else None,
        runs_by_kind=_group(records, "kind"),
        runs_by_model=_group(records, "model"),
        by_week=_weeks(records),
        failure_rate=_share(judged, lambda r: r.get("status") in FAILED),
        # Over the judged records as well: a schema-4 dict-returning workflow record carries
        # `decided: true` as a default nobody measured, not as a fact about the run.
        undecided_rate=_share(judged, lambda r: r.get("decided") is False),
        blocked=sum(1 for r in records if r.get("status") == "blocked"),
        unclassified=total - len(judged),
        top_reasons=reasons.most_common(TOP_N),
        cost_total=_measure(_numbers(records, "cost_usd"), total),
        cost_per_run=_measure(_numbers(records, "cost_usd"), total, "mean"),
        tokens_total=_measure(_numbers(records, "tokens"), total),
        billed_share=_measure(_numbers(records, "billed_fraction"), total, "mean"),
        cached_share=_cached(records),
        seconds_median=_measure(_numbers(records, "wall_seconds"), total, "median"),
        seconds_p90=_measure(_numbers(records, "wall_seconds"), total, "p90"),
        turns_by_strategy=_turns(records),
        findings_total=findings_total,
        findings_per_run=_measure(
            [
                float(f["count"])
                for r in records
                if isinstance(f := r.get("findings"), dict) and isinstance(f.get("count"), int)
            ],
            total,
            "mean",
        ),
        top_findings=top_findings,
        injection_signals=injections,
        people=_people(records),
        unattended=_share(
            records, lambda r: isinstance(r.get("approval"), dict) and r["approval"].get("attended") is False
        ),
        against_dirty_tree=_share(records, lambda r: r.get("dirty") is True),
        attempts_by_ticket=_attempts(records),
    )


def _hours(row: dict[str, Any], start: str, end: str) -> float | None:
    """Hours between two ISO timestamps on one row, or `None` if either is missing or unreadable.

    `None` rather than `0.0`, so a row that cannot be measured lowers `Measured.of` instead of
    dragging an average toward zero — which is the same rule the ledger side follows and the same
    failure it prevents: an average over rows that were never timed, printed as though it were an
    average over all of them.
    """
    began, ended = row.get(start), row.get(end)
    if not isinstance(began, str) or not isinstance(ended, str):
        return None
    try:
        opened = datetime.fromisoformat(began.replace("Z", "+00:00"))
        closed = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (closed - opened).total_seconds() / 3600.0


def delivery(pulls: list[dict[str, Any]], issues: list[dict[str, Any]]) -> Delivery:
    """Timings over rows the host answered with. Pure, like everything else here.

    Takes plain dicts rather than an `Scm`, which is what keeps this module a leaf: the caller does
    the network, the token and the host-specific field names, and hands over something a test can
    write by hand. `pulls` are already filtered to branches this framework opened — that filtering
    is `is_run_branch`'s job and belongs where the branch layout lives.
    """
    merged = [row for row in pulls if isinstance(row.get("merged_at"), str)]
    merge_hours = [h for row in merged if (h := _hours(row, "created_at", "merged_at")) is not None]
    closed = [row for row in issues if isinstance(row.get("closed_at"), str)]
    close_hours = [h for row in closed if (h := _hours(row, "created_at", "closed_at")) is not None]
    answered = [h for row in issues if (h := _hours(row, "created_at", "first_response_at")) is not None]
    return Delivery(
        pulls=len(pulls),
        merged=len(merged),
        hours_to_merge=_measure(merge_hours, len(pulls), "median"),
        issues=len(issues),
        closed=len(closed),
        hours_to_close=_measure(close_hours, len(issues), "median"),
        hours_to_first_response=_measure(answered, len(issues), "median"),
    )


def _turns(records: list[dict[str, Any]]) -> list[tuple[str, Measured]]:
    """Mean turns, per strategy. The answer to "how much back-and-forth does this actually take",
    which is the number that decides whether a turn cap is generous or is the thing biting.

    The denominator is EVERY run of that strategy, not just the ones that recorded a turn count.
    Bucketing only the turn-bearing records — which is what this did first — makes `of` and `total`
    the same number by construction, so `complete` is always True and the label that exists to warn
    you is silently never printed. "6.0 turns" over one of ten runs has to say so.

    A strategy no run of which recorded turns is left out rather than listed as a dash: reviews do
    not have turns and never will, and a row per verb saying so is noise rather than honesty.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        buckets.setdefault(str(record.get("strategy") or record.get("kind") or "(unrecorded)"), []).append(
            record
        )
    rows = [
        (name, measured)
        for name, group in buckets.items()
        if (measured := _measure(_numbers(group, "turns"), len(group), "mean")).value is not None
    ]
    rows.sort(key=lambda row: (-(row[1].of), row[0]))
    return rows


# -- rendering ---------------------------------------------------------------------------------
#
# Both renderers live here rather than in `cli`, and neither writes a file. A report is a string;
# putting it on disk is a redaction sink and belongs to a layer that may reach `privileged`.


def _iso_week(record: dict[str, Any]) -> str:
    """The ISO week a record falls in, or "" if it carries no usable stamp.

    The same recipe `_weeks` uses, including the absence of any timezone normalisation: the stamp's
    own offset decides its week. Spelled twice rather than shared because `_weeks` returns buckets
    and this returns a label, and a helper that did both would take a flag.
    """
    stamp = record.get("ts")
    if not isinstance(stamp, str):
        return ""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    year, week, _ = when.isocalendar()
    return f"{year}-W{week:02d}"


def recurring(
    records: list[dict[str, Any]],
    *,
    min_runs: int = MIN_TREND_RUNS,
    min_weeks: int = MIN_TREND_WEEKS,
) -> list[Trend]:
    """Every finding the ledger has seen, with what recurrence can be claimed for it.

    The whole census, with `qualifies` on each row, rather than the rows that cleared the
    thresholds. A filtered list that comes back empty says "nothing recurs" in exactly the same
    shape as "there is no ledger", and that conflation is the one this module exists to refuse. On
    this repository the filtered form returns nothing at all: 17 finding ids over 27 records, and
    not one of them spans two ISO weeks.

    Injection signals are counted apart the way `_findings` counts them apart, and for a sharper
    reason here: one run on this ledger carries 16 of them, so a census that mixed them in would be
    led by an id that is one run pretending to be eleven — and it is a fact about somebody trying
    to talk to the model, not about the prompt text a proposal would change.
    """
    occurrences: Counter[str] = Counter()
    runs: dict[str, set[str]] = {}
    weeks: dict[str, set[str]] = {}
    dated: dict[str, set[str]] = {}
    blocking: dict[str, set[str]] = {}
    workflows: dict[str, set[str]] = {}

    for index, record in enumerate(records):
        found = record.get("findings")
        if not isinstance(found, dict):
            continue
        # The top-level list only. Since schema 5 a record mirrors its steps' findings up here, so
        # walking `steps[].findings` as well would double every count on every record written
        # since — invisibly, because the ratio between ids would not change.
        items = found.get("items") or []
        # A record with no `run_id` is still one run. Falling back to its position keeps it from
        # merging with every other unidentified record into a single phantom run.
        run = str(record.get("run_id") or f"#{index}")
        week = _iso_week(record)
        where = str(record.get("workflow") or record.get("kind") or "")
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("id", ""))
            if not name or name.startswith("injection."):
                continue
            occurrences[name] += 1
            runs.setdefault(name, set()).add(run)
            if week:
                weeks.setdefault(name, set()).add(week)
                dated.setdefault(name, set()).add(run)
            if where:
                workflows.setdefault(name, set()).add(where)
            if item.get("blocking") is True:
                blocking.setdefault(name, set()).add(run)

    considered = len(records)
    trends = [
        Trend(
            finding=name,
            occurrences=count,
            runs=len(runs[name]),
            weeks=len(weeks.get(name, ())),
            dated_runs=len(dated.get(name, ())),
            undated_runs=len(runs[name]) - len(dated.get(name, ())),
            blocking_runs=len(blocking.get(name, ())),
            workflows=tuple(sorted(workflows.get(name, ()))),
            considered=considered,
            qualifies=len(runs[name]) >= min_runs and len(weeks.get(name, ())) >= min_weeks,
        )
        for name, count in occurrences.items()
    ]
    # Runs first, because runs is what a threshold reads; occurrences breaks the tie, and the id
    # breaks that, so the order is stable over a dict whose insertion order is the ledger's.
    return sorted(trends, key=lambda t: (-t.runs, -t.occurrences, t.finding))


def as_trend_text(
    trends: list[Trend],
    *,
    min_runs: int = MIN_TREND_RUNS,
    min_weeks: int = MIN_TREND_WEEKS,
    limit: int = TOP_N,
) -> list[str]:
    """The census as lines. Returns them; writing is the caller's, because this is a leaf layer
    that may not reach the redaction sink."""
    if not trends:
        return ["trend     no finding recorded yet; a run that finds nothing writes no id"]

    qualified = [t for t in trends if t.qualifies]
    if qualified:
        head = f"trend     {len(qualified)} of {len(trends)} finding id(s) recur across {min_weeks}+ weeks"
    else:
        head = (
            f"trend     nothing recurs yet — no finding id clears both thresholds "
            f"(min_runs {min_runs}, min_weeks {min_weeks})"
        )

    out = [head]
    for trend in trends[:limit]:
        if trend.weeks:
            span = f"{trend.weeks} week(s)"
            if trend.undated_runs:
                span += f"  ({trend.dated_runs} of {trend.runs} dated)"
        else:
            # Not `0 weeks`: that reads as "measured, and it does not recur". Nobody could place
            # any of these runs in a week at all.
            span = f"—  (0 of {trend.runs} dated)"
        mark = "*" if trend.qualifies else " "
        out += [
            f"  {mark} {trend.finding:<28}{trend.runs:>3} run(s)  {trend.occurrences:>3} occurrence(s)"
            f"   {span:<28}{trend.blocking_runs:>3} blocking   {', '.join(trend.workflows)}"
        ]
    if len(trends) > limit:
        out += [f"          {len(trends) - limit} more not shown"]

    out += ["", "A dash is a number nobody measured. It is not a zero."]
    return out


def as_text(report: Report) -> list[str]:
    """The terminal form. One screen, and every number carrying its denominator."""
    if not report.records:
        return ["no records yet; the first run that writes one creates them"]

    out: list[str] = []
    when = f"  {report.window[0][:10]} → {report.window[1][:10]}" if report.window else ""
    out += [f"records   {report.records}{when}", ""]

    out += ["outcomes"]
    out += [f"  failed        {_pct(report.failure_rate)}"]
    out += [f"  decided none  {_pct(report.undecided_rate)}"]
    out += [f"  blocked       {report.blocked}  (a control stopping a run is the control working)"]
    if report.unclassified:
        out += [
            f"  no verdict    {report.unclassified}  (written before schema 5 by a workflow that "
            f"returned no Outcome; not counted as anything)"
        ]
    for reason, count in report.top_reasons:
        out += [f"    {reason:<28} {count}"]

    out += ["", "effort"]
    out += [f"  median run    {report.seconds_median.render('s')}"]
    out += [f"  slowest 10%   {report.seconds_p90.render('s')}"]
    for name, turns in report.turns_by_strategy:
        out += [f"  turns, {name:<15} {turns.render(places=1)}"]
    if not report.turns_by_strategy:
        out += ["  turns         —  (no record carries one; only implementing verbs write it)"]

    out += ["", "spend"]
    out += [f"  total         {_amount(report.cost_total)}"]
    out += [f"  per run       {_amount(report.cost_per_run)}"]
    out += [f"  tokens        {report.tokens_total.render(places=0)}"]
    if report.billed_share.value is not None:
        out += [f"  actually billed {report.billed_share.value:.0%} of runs' tokens (the rest replayed)"]
    if report.cached_share.value is not None:
        out += [
            f"  from cache    {report.cached_share.value:.0%} of input, "
            f"at a tenth of the price  ({report.cached_share.of} of {report.cached_share.total} runs)"
        ]

    out += ["", "what it found"]
    out += [f"  findings      {report.findings_total}  ({report.findings_per_run.render(places=1)} per run)"]
    if report.injection_signals:
        out += [f"  injection     {report.injection_signals}  signals in text somebody wrote at it"]
    for name, count in report.top_findings:
        out += [f"    {name:<28} {count}"]

    out += ["", "by kind"]
    for group in report.runs_by_kind:
        rate = "—" if group.failure_rate is None else f"{group.failure_rate:.0%}"
        unjudged = group.runs - group.judged
        out += [
            f"  {group.name:<14} {group.runs:>4} run(s)   {rate:>4} failed   "
            f"{group.seconds.render('s')} median" + (f"   ({unjudged} with no verdict)" if unjudged else "")
        ]

    out += ["", "attempts per ticket"]
    for ticket, runs, cost in report.attempts_by_ticket:
        out += [f"  {ticket:<18} {runs} run(s)   {_amount(cost)}"]
    if not report.attempts_by_ticket:
        out += ["  —  (no record carries a ticket)"]

    hygiene: list[str] = []
    for name, count in report.people:
        hygiene += [f"  {name:<24} {count} run(s)"]
    if report.unattended.value:
        hygiene += [f"  unattended    {report.unattended.value:.0%} of runs, with nobody watching"]
    if report.against_dirty_tree.value:
        hygiene += [f"  dirty tree    {report.against_dirty_tree.value:.0%} of runs saw uncommitted changes"]
    if hygiene:
        out += ["", "who and how"] + hygiene

    if report.delivery is not None:
        d = report.delivery
        rate = "—" if d.merge_rate is None else f"{d.merge_rate:.0%}"
        out += ["", "delivery  (asked of the host, not read from the ledger)"]
        out += [f"  pull requests {d.pulls}  opened, {d.merged} merged ({rate})"]
        out += [f"  to merge      {d.hours_to_merge.render('h')} median"]
        out += [f"  issues filed  {d.issues}  ({d.closed} closed)"]
        out += [f"  to close      {d.hours_to_close.render('h')} median"]
        out += [f"  first reply   {d.hours_to_first_response.render('h')} median"]

    out += ["", "A dash is a number nobody measured. It is not a zero."]
    return out


def _amount(measured: Measured) -> str:
    """Money, with the currency sign INSIDE the absent case rather than in front of it.

    `f"${m.render()}"` renders an unmeasured cost as `$—`, which reads as a price of nothing rather
    than as an absence. The dash has to be the whole answer for it to mean "nobody measured this".
    """
    if measured.value is None:
        return measured.render()
    return f"${measured.render(places=4)}"


def _pct(measured: Measured) -> str:
    if measured.value is None:
        return "—"
    return f"{measured.value:.0%}  ({int(measured.value * measured.total)} of {measured.total})"


#: The site's palette, so a published report looks like the rest of it rather than like a
#: different product. Duplicated from `site.css` on purpose: this file has to render standalone,
#: with no stylesheet to fetch, because a report is a thing people email to each other.
INK, INK2, LINE, TEXT, TEXT2, TEXT3 = "#0D0E12", "#14161C", "#262A34", "#E8E6E1", "#A7A9AF", "#6E727C"
MINT, GOLD, ALARM = "#74D6C2", "#E9B872", "#E5806B"


def as_html(report: Report, *, title: str = "in-lockstep — what the ledger says") -> str:
    """A standalone page. No stylesheet, no script, no fonts to fetch — one file that renders the
    same on a laptop with no network as it does published.

    Charts are SVG generated here rather than a charting library. A report that needed a CDN would
    be a report that stops rendering the day the CDN does, and this is a document about evidence.
    """
    body = [
        _h(report),
        _cards(report),
        _weeks_chart(report),
        _bars("Where the runs go", [(g.name, g.runs) for g in report.runs_by_kind], MINT, "run"),
        _bars(
            "Attempts per ticket",
            [(ticket, runs) for ticket, runs, _ in report.attempts_by_ticket],
            GOLD,
            "run",
        ),
        _bars("Turns per piece of work", [(n, m.value or 0) for n, m in report.turns_by_strategy], GOLD, ""),
        _bars("What it keeps finding", report.top_findings, MINT, ""),
        _kinds_table(report),
        _delivery(report),
        _foot(report),
    ]
    return _PAGE.format(title=_esc(title), ink=INK, text=TEXT, body="\n".join(b for b in body if b))


def _h(report: Report) -> str:
    when = ""
    if report.window:
        when = f"<p class=sub>{_esc(report.window[0][:10])} to {_esc(report.window[1][:10])}</p>"
    return f"<h1>What the ledger says</h1>{when}"


def _cards(report: Report) -> str:
    """The numbers somebody scrolls to first. A dash where nothing was measured, never a zero."""
    cells = [
        ("runs recorded", f"{report.records:,}", ""),
        ("failed", _pct_only(report.failure_rate), "a run that did not produce what it was asked for"),
        ("decided nothing", _pct_only(report.undecided_rate), "ran, and settled no question"),
        (
            "stopped by a control",
            f"{report.blocked:,}",
            "a budget or a missing sign-off; working as intended",
        ),
        *(
            [("no verdict", f"{report.unclassified:,}", "recorded before schema 5; counted as nothing")]
            if report.unclassified
            else []
        ),
        ("total spend", _money(report.cost_total), ""),
        ("per run", _money(report.cost_per_run), ""),
        ("median run", report.seconds_median.render("s"), "half of them finished faster"),
        ("findings", f"{report.findings_total:,}", report.findings_per_run.render(places=1) + " per run"),
    ]
    inner = "".join(
        f"<div class=card><b>{_esc(value)}</b><span>{_esc(label)}</span>"
        f"{f'<i>{_esc(note)}</i>' if note else ''}</div>"
        for label, value, note in cells
    )
    return f"<div class=cards>{inner}</div>"


def _weeks_chart(report: Report) -> str:
    """Runs per week. The one chart that answers "is this being used", which is the first thing
    anybody asks and the last thing a table makes obvious."""
    if len(report.by_week) < 2:
        return _note(
            "Runs over time needs at least two weeks of records that carry a timestamp. "
            f"{len(report.by_week)} week(s) so far."
        )
    peak = max(runs for _, runs, _ in report.by_week) or 1
    width, height, pad = 720, 200, 28
    step = (width - pad * 2) / max(len(report.by_week) - 1, 1)
    points = " ".join(
        f"{pad + i * step:.1f},{height - pad - (runs / peak) * (height - pad * 2):.1f}"
        for i, (_, runs, _) in enumerate(report.by_week)
    )
    dots = "".join(
        f'<circle cx="{pad + i * step:.1f}" cy="{height - pad - (runs / peak) * (height - pad * 2):.1f}"'
        f' r="3" fill="{MINT}"/>'
        for i, (_, runs, _) in enumerate(report.by_week)
    )
    first, last = report.by_week[0][0], report.by_week[-1][0]
    return f"""<section><h2>Runs per week</h2>
<svg viewBox="0 0 {width} {height}" role="img" aria-label="runs per week, peaking at {peak}">
<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="{LINE}"/>
<polyline points="{points}" fill="none" stroke="{MINT}" stroke-width="2"/>{dots}
<text x="{pad}" y="{height - 8}" fill="{TEXT3}" font-size="11">{_esc(first)}</text>
<text x="{width - pad}" y="{height - 8}" fill="{TEXT3}" font-size="11" text-anchor="end">{_esc(last)}</text>
<text x="{pad}" y="18" fill="{TEXT3}" font-size="11">peak {peak} run(s)</text>
</svg></section>"""


def _bars(heading: str, rows: list[tuple[str, Any]], colour: str, unit: str) -> str:
    """A horizontal bar per row. Horizontal because the labels are words — `implement/tdd`,
    `review.security` — and vertical bars turn those into rotated text nobody reads."""
    usable = [(str(name), float(value)) for name, value in rows if float(value or 0) > 0]
    if not usable:
        return ""
    peak = max(value for _, value in usable)
    bars = []
    for name, value in usable:
        width = max(1.0, (value / peak) * 100)
        shown = f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"
        label = f"{shown} {unit}{'s' if unit and value != 1 else ''}".strip()
        bars.append(
            f"<div class=bar><span>{_esc(name)}</span>"
            f'<i style="width:{width:.1f}%;background:{colour}"></i><b>{_esc(label)}</b></div>'
        )
    return f"<section><h2>{_esc(heading)}</h2><div class=bars>{''.join(bars)}</div></section>"


def _kinds_table(report: Report) -> str:
    if not report.runs_by_kind:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(g.name)}</td><td>{g.runs}</td>"
        f"<td>{'—' if g.failure_rate is None else f'{g.failure_rate:.0%}'}</td>"
        f"<td>{_esc(g.seconds.render('s'))}</td><td>{_esc(_money(g.cost))}</td></tr>"
        for g in report.runs_by_kind
    )
    return f"""<section><h2>By kind of work</h2><div class=scroll><table>
<tr><th>kind</th><th>runs</th><th>failed</th><th>median</th><th>spend</th></tr>{rows}</table></div></section>"""


def _delivery(report: Report) -> str:
    """Its own section, and labelled as somebody else's evidence.

    Gold rather than mint, which is the site's own distinction between what the machine did and
    what the humans around it did. These numbers are about the humans: whether anybody merged the
    work, and how long they left an issue sitting.
    """
    d = report.delivery
    if d is None:
        return ""
    rate = "—" if d.merge_rate is None else f"{d.merge_rate:.0%}"
    cells = [
        ("pull requests opened", f"{d.pulls:,}", "matched on the branch we wrote, not on title text"),
        ("merged", f"{d.merged:,}  ({rate})", ""),
        ("median time to merge", d.hours_to_merge.render("h"), ""),
        ("issues filed", f"{d.issues:,}", f"{d.closed:,} closed"),
        ("median time to close", d.hours_to_close.render("h"), ""),
        ("median first reply", d.hours_to_first_response.render("h"), "how long before a person answered"),
    ]
    inner = "".join(
        f"<div class=card><b style='color:{GOLD}'>{_esc(value)}</b><span>{_esc(label)}</span>"
        f"{f'<i>{_esc(note)}</i>' if note else ''}</div>"
        for label, value, note in cells
    )
    return (
        "<section><h2>Delivery</h2>"
        "<p class=sub>Asked of the host rather than read from the ledger — the only numbers on "
        "this page this framework did not write itself.</p>"
        f"<div class=cards>{inner}</div></section>"
    )


def _foot(report: Report) -> str:
    extra = ""
    if report.injection_signals:
        extra = (
            f"<p class=sub>{report.injection_signals} injection signal(s) were caught in text people "
            f"wrote at it — counted apart from findings, because one is about the code and the other "
            f"is about somebody trying to talk to the model through it.</p>"
        )
    return (
        f"{extra}<p class=sub>Every number here was computed from records this repository's own runs "
        f"wrote. A dash is a number nobody measured — it is not a zero, and the count beside it says "
        f"how many records carried the field.</p>"
    )


def _pct_only(measured: Measured) -> str:
    return "—" if measured.value is None else f"{measured.value:.0%}"


def _money(measured: Measured) -> str:
    return "—" if measured.value is None else f"${measured.value:,.4f}"


def _esc(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:{ink}; color:{text};
  font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  padding: 44px 24px 80px; }}
main {{ max-width: 860px; margin: 0 auto; }}
h1 {{ font-size: clamp(30px,5vw,44px); line-height:1.1; margin:0 0 6px; letter-spacing:-.02em; }}
h2 {{ font-size: 15px; text-transform: uppercase; letter-spacing:.14em; color:#6E727C;
  margin: 44px 0 16px; font-weight: 500; }}
.sub {{ color:#6E727C; font-size:14.5px; margin:0 0 4px; }}
.cards {{ display:grid; gap:1px; background:#262A34; border:1px solid #262A34; border-radius:8px;
  overflow:hidden; grid-template-columns: repeat(auto-fit, minmax(190px,1fr)); margin-top:28px; }}
.card {{ background:#14161C; padding:20px; }}
.card b {{ display:block; font-size:28px; font-weight:500; color:#74D6C2; letter-spacing:-.02em; }}
.card span {{ display:block; font-size:14px; color:#A7A9AF; margin-top:4px; }}
.card i {{ display:block; font-style:normal; font-size:12.5px; color:#6E727C; margin-top:6px; }}
svg {{ width:100%; height:auto; display:block; }}
.bars {{ display:flex; flex-direction:column; gap:9px; }}
.bar {{ display:grid; grid-template-columns: minmax(0,13em) 1fr auto; gap:12px; align-items:center; }}
.bar span {{ font-size:13.5px; color:#A7A9AF; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
.bar i {{ height:9px; border-radius:2px; display:block; min-width:2px; }}
.bar b {{ font-size:13px; color:#6E727C; font-weight:400; white-space:nowrap; }}
.scroll {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:14.5px; }}
th, td {{ text-align:left; padding:9px 12px; border:1px solid #262A34; color:#A7A9AF; }}
th {{ color:#6E727C; font-weight:500; font-size:12px; text-transform:uppercase; letter-spacing:.1em; }}
.note {{ border:1px dashed #262A34; border-radius:6px; padding:14px 16px; color:#6E727C;
  font-size:14.5px; margin-top:16px; }}
</style></head><body><main>
{body}
</main></body></html>
"""


def _note(text: str) -> str:
    return f"<div class=note>{_esc(text)}</div>"
