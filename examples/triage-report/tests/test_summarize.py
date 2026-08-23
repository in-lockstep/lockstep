"""The numbers in a published report are arithmetic, so they get tested like arithmetic."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "summarize", Path(__file__).parent.parent / "scripts" / "summarize.py"
)
assert spec and spec.loader
summarize = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summarize)

NOW = datetime(2026, 8, 23, tzinfo=UTC)


def issue(key, *, days=1, type="Bug", priority="High", labels=None, components=None):
    return {
        "key": key,
        "type": type,
        "priority": priority,
        "labels": labels if labels is not None else ["x"],
        "components": components if components is not None else ["orders"],
        "created": (NOW - timedelta(days=days)).isoformat(),
    }


def test_totals_are_counted_not_estimated():
    result = summarize.summarize([issue("A"), issue("B"), issue("C")], NOW)
    assert result["total"] == 3


def test_issues_are_grouped_by_the_things_triage_turns_on():
    issues = [issue("A", type="Bug"), issue("B", type="Story"), issue("C", type="Bug")]
    assert summarize.summarize(issues, NOW)["by_type"] == {"Bug": 2, "Story": 1}


def test_an_issue_with_no_component_is_counted_as_unassigned():
    """Otherwise the component totals silently disagree with the overall total."""
    result = summarize.summarize([issue("A", components=[])], NOW)
    assert result["by_component"] == {"unassigned": 1}
    assert result["no_component"] == ["A"]


def test_unlabelled_issues_are_named_so_the_report_can_ask_about_them():
    assert summarize.summarize([issue("A", labels=[]), issue("B")], NOW)["unlabelled"] == ["A"]


def test_stale_issues_are_those_past_the_threshold():
    issues = [issue("OLD", days=40), issue("NEW", days=2), issue("EDGE", days=14)]
    result = summarize.summarize(issues, NOW)
    assert set(result["stale"]) == {"OLD", "EDGE"}
    assert result["stale_threshold_days"] == 14


def test_stale_issues_are_ordered_oldest_first():
    issues = [issue("A", days=20), issue("B", days=90), issue("C", days=30)]
    assert summarize.summarize(issues, NOW)["stale"] == ["B", "C", "A"]


def test_an_unparseable_timestamp_is_recorded_rather_than_guessed():
    """Silently treating it as fresh would understate the backlog in a published report."""
    broken = {**issue("A"), "created": "not a date"}
    result = summarize.summarize([broken], NOW)
    assert result["undated"] == ["A"]
    assert result["stale"] == []


def test_an_empty_backlog_summarizes_cleanly():
    result = summarize.summarize([], NOW)
    assert result["total"] == 0
    assert result["oldest_days"] == 0
    assert result["stale"] == []
