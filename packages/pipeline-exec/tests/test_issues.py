"""Reducing one GitHub issue into something an implementing agent can build from.

The interesting behaviour is where the acceptance criteria come from. A tracker keeps them in a
named field; GitHub keeps them wherever the author put them, and the two places authors actually
put them are a task list and a heading. Reading neither means an agent implements a title.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from pipeline_exec.cli import main
from pipeline_exec.issues import criteria_from, reduce_issue

BODY = """\
Totalling an order containing an item with a null price raises a TypeError.

## Acceptance criteria

- Given an item with no price
- Then the total skips it
* And a warning is logged

## Notes

- Not in scope: currency conversion
"""

TASKS = """\
Some prose.

- [ ] Reject a null price
- [x] Add the regression test
- not a task
"""

ISSUE = {
    "number": 412,
    "title": "Order total fails when an item has no price",
    "body": BODY,
    "state": "open",
    "html_url": "https://github.com/acme/web/issues/412",
    "labels": [{"name": "bug"}, {"name": "area/billing"}],
    "assignees": [{"login": "ana"}],
}
COMMENTS = [
    {"user": {"login": "bo"}, "body": "It is the `/orders/total` endpoint specifically."},
    {"user": {"login": "ana"}, "body": "Agreed."},
]


# --- where the criteria come from -------------------------------------------


def test_a_criteria_heading_is_read_as_criteria():
    assert criteria_from(BODY) == [
        "Given an item with no price",
        "Then the total skips it",
        "And a warning is logged",
    ]


def test_the_section_stops_at_the_next_heading():
    """Otherwise 'Not in scope' arrives as a requirement, which is the opposite of what it says."""
    assert "Not in scope: currency conversion" not in criteria_from(BODY)


def test_a_task_list_is_read_when_there_is_no_heading():
    assert criteria_from(TASKS) == ["Reject a null price", "Add the regression test"]


def test_a_checked_box_is_still_a_requirement():
    """In an issue being implemented, checked usually means agreed rather than already built."""
    assert "Add the regression test" in criteria_from(TASKS)


def test_a_heading_wins_over_a_task_list():
    """A task list can be anything — affected files, a rollout plan. A section was written to be this."""
    both = BODY + "\n- [ ] unrelated checklist item\n"
    assert "unrelated checklist item" not in criteria_from(both)


@pytest.mark.parametrize("heading", ["Acceptance Criteria", "AC", "Definition of done"])
def test_the_headings_people_actually_write(heading):
    assert criteria_from(f"## {heading}\n\n- One\n") == ["One"]


def test_prose_under_the_heading_is_not_dropped():
    """Somebody writes a paragraph instead of bullets; silently returning nothing is worse."""
    assert criteria_from("## Acceptance criteria\n\nThe total must skip priced-less items.\n") == [
        "The total must skip priced-less items."
    ]


def test_an_issue_with_no_criteria_says_so_by_being_empty():
    assert criteria_from("Just a sentence.") == []


# --- the document ------------------------------------------------------------


def test_the_shared_keys_match_what_an_analyst_already_reads():
    """`key`, `summary`, `description`, `acceptance_criteria` — the same four a tracker emits."""
    document = reduce_issue(ISSUE, [], repo="acme/web")
    assert document["key"] == "#412"
    assert document["summary"].startswith("Order total fails")
    assert document["description"].startswith("Totalling an order")
    assert len(document["acceptance_criteria"]) == 3


def test_labels_travel_verbatim_rather_than_becoming_a_type():
    """Mapping labels onto an issue type would be this runtime deciding what a repo's labels mean."""
    assert reduce_issue(ISSUE, [])["labels"] == ["bug", "area/billing"]


def test_labels_from_a_webhook_payload_are_strings_not_objects():
    assert reduce_issue({"number": 1, "labels": ["bug"]}, [])["labels"] == ["bug"]


def test_the_discussion_is_included_because_that_is_where_the_requirement_ends_up():
    document = reduce_issue(ISSUE, COMMENTS)
    assert document["discussion"][0]["author"] == "bo"
    assert "/orders/total" in document["discussion"][0]["body"]


def test_a_pasted_log_cannot_push_the_issue_out_of_context():
    document = reduce_issue(
        {"number": 1, "body": "x" * 50_000},
        [{"user": {"login": "a"}, "body": "y" * 50_000}] * 200,
    )
    assert len(document["description"]) == 12000
    assert len(document["discussion"]) == 30
    assert len(document["discussion"][0]["body"]) == 4000


# --- the command -------------------------------------------------------------


@pytest.fixture
def fixtures(tmp_path):
    directory = tmp_path / "gh"
    directory.mkdir()
    (directory / "issue.json").write_text(json.dumps(ISSUE), encoding="utf-8")
    (directory / "comments.json").write_text(json.dumps(COMMENTS), encoding="utf-8")
    return directory


def run(*args):
    return CliRunner().invoke(main, ["gh-issue-fetch", *args])


def test_the_command_writes_the_document(tmp_path, fixtures):
    output = tmp_path / "issue.json"
    result = run("--issue=412", "--repo=acme/web", f"--output={output}", f"--from-dir={fixtures}")
    assert result.exit_code == 0, result.output
    document = json.loads(output.read_text())
    assert document["number"] == 412
    assert document["repo"] == "acme/web"
    assert len(document["discussion"]) == 2


def test_the_counts_are_published_for_a_later_step(tmp_path, fixtures):
    result = run(
        "--issue=412", "--repo=acme/web", f"--output={tmp_path / 'i.json'}", f"--from-dir={fixtures}"
    )
    assert "number=412" in result.output
    assert "state=open" in result.output
    assert "criteria=3" in result.output


def test_the_discussion_can_be_left_out(tmp_path, fixtures):
    output = tmp_path / "issue.json"
    run(
        "--issue=412",
        "--repo=acme/web",
        f"--output={output}",
        f"--from-dir={fixtures}",
        "--no-discussion",
    )
    assert json.loads(output.read_text())["discussion"] == []


@pytest.mark.parametrize(
    "given",
    ["412", "#412", "acme/web#412", "https://github.com/acme/web/issues/412", "  412 "],
)
def test_every_way_somebody_names_an_issue(tmp_path, fixtures, given):
    result = run(
        f"--issue={given}", "--repo=acme/web", f"--output={tmp_path / 'i.json'}", f"--from-dir={fixtures}"
    )
    assert result.exit_code == 0, result.output


def test_something_that_is_not_an_issue_number_fails_loudly(tmp_path, fixtures):
    """A model handed an empty document will write something, and it will look plausible."""
    result = run(
        "--issue=the-login-one",
        "--repo=acme/web",
        f"--output={tmp_path / 'i.json'}",
        f"--from-dir={fixtures}",
    )
    assert result.exit_code == 1
    assert "cannot read an issue number" in result.output


def test_an_empty_fixture_directory_fails_rather_than_writing_an_empty_issue(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run("--issue=1", "--repo=acme/web", f"--output={tmp_path / 'i.json'}", f"--from-dir={empty}")
    assert result.exit_code == 1
    assert "no issue found" in result.output
