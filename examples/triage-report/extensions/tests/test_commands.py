"""What the search carries forward, and what it drops."""

from __future__ import annotations

import json

from click.testing import CliRunner

from triage_ext.commands import jql_search, reduce_issue

RAW = {
    "key": "APP-11",
    "fields": {
        "summary": "Checkout fails on empty cart",
        "description": "x" * 4000,
        "issuetype": {"name": "Bug"},
        "priority": {"name": "High"},
        "status": {"name": "Needs Triage"},
        "reporter": {"displayName": "Ana"},
        "labels": ["checkout"],
        "components": [{"name": "orders"}],
        "worklog": {"worklogs": ["…enormous…"]},
        "watches": {"watchCount": 12},
    },
}


def test_a_triage_decision_gets_what_it_turns_on():
    reduced = reduce_issue(RAW)
    assert reduced["key"] == "APP-11"
    assert reduced["type"] == "Bug"
    assert reduced["priority"] == "High"
    assert reduced["components"] == ["orders"]


def test_everything_a_triage_decision_does_not_turn_on_is_dropped():
    """Every field carried forward costs context the model could spend reasoning."""
    reduced = reduce_issue(RAW)
    assert "worklog" not in reduced
    assert "watches" not in reduced


def test_descriptions_are_truncated():
    assert len(reduce_issue(RAW)["description"]) == 1500


def test_missing_fields_become_stated_defaults_not_crashes():
    """A tracker with no priority set is normal; a KeyError halfway through a search is not."""
    reduced = reduce_issue({"key": "APP-2", "fields": {}})
    assert reduced["priority"] == "unset"
    assert reduced["type"] == "unknown"
    assert reduced["labels"] == []


def test_search_writes_what_the_next_step_reads(tmp_path):
    source = tmp_path / "raw.json"
    source.write_text(json.dumps({"issues": [RAW]}), encoding="utf-8")
    output = tmp_path / "issues.json"

    result = CliRunner().invoke(
        jql_search, ["--jql=project = APP", f"--output={output}", f"--from-file={source}"]
    )
    assert result.exit_code == 0
    assert "count=1" in result.output
    assert json.loads(output.read_text())[0]["key"] == "APP-11"


def test_the_limit_is_honoured(tmp_path):
    source = tmp_path / "raw.json"
    source.write_text(json.dumps({"issues": [RAW] * 10}), encoding="utf-8")
    output = tmp_path / "issues.json"
    CliRunner().invoke(
        jql_search, ["--jql=x", f"--output={output}", f"--from-file={source}", "--limit=3"]
    )
    assert len(json.loads(output.read_text())) == 3


def test_an_empty_result_is_not_a_failure(tmp_path):
    source = tmp_path / "raw.json"
    source.write_text(json.dumps({"issues": []}), encoding="utf-8")
    output = tmp_path / "issues.json"
    result = CliRunner().invoke(jql_search, ["--jql=x", f"--output={output}", f"--from-file={source}"])
    assert result.exit_code == 0
    assert json.loads(output.read_text()) == []
