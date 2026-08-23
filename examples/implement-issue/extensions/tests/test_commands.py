"""The extension's own tests.

`pr-feedback` gets the most attention: it is the step that turns a reviewer's free text into input
for a model that writes code, so what it keeps, drops and truncates is the interesting behaviour.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from issue_ext.commands import issue_fetch, normalize, pr_feedback

REVIEWS = [
    {"state": "CHANGES_REQUESTED", "body": "Use the existing retry helper.", "user": {"login": "ana"}},
    {"state": "COMMENTED", "body": "", "user": {"login": "bo"}},
]
COMMENTS = [
    {"path": "src/app.py", "line": 42, "body": "This swallows the error.", "user": {"login": "ana"}},
    {"path": "src/app.py", "line": None, "original_line": 7, "body": "Naming.", "user": {"login": "bo"}},
    {"path": "src/x.py", "body": "   ", "user": {"login": "ana"}},
]
DISCUSSION = [
    {"body": "/implement APP-1", "user": {"login": "ana"}},
    {"body": "Please also update the changelog.", "user": {"login": "bo"}},
]


# --- what feedback collection keeps ----------------------------------------


def test_inline_comments_keep_the_file_and_line():
    """"This is wrong" means nothing to a code-writing agent without the location."""
    feedback = normalize(REVIEWS, COMMENTS, [])
    assert feedback["inline"][0] == {
        "path": "src/app.py",
        "line": 42,
        "body": "This swallows the error.",
        "author": "ana",
    }


def test_an_inline_comment_falls_back_to_its_original_line():
    """A comment on a line the diff has since moved still points somewhere useful."""
    assert normalize([], COMMENTS, [])["inline"][1]["line"] == 7


def test_empty_comments_are_dropped():
    assert all(entry["body"].strip() for entry in normalize(REVIEWS, COMMENTS, [])["inline"])
    assert all(entry["body"].strip() for entry in normalize(REVIEWS, COMMENTS, [])["reviews"])


def test_a_command_invocation_is_not_treated_as_feedback():
    """The comment that started the run is not a review of the code it is about to write."""
    discussion = normalize([], [], DISCUSSION)["discussion"]
    assert [entry["body"] for entry in discussion] == ["Please also update the changelog."]


def test_requested_changes_is_surfaced_as_a_flag():
    assert normalize(REVIEWS, [], [])["requested_changes"] is True
    assert normalize([{"state": "APPROVED", "body": "ok"}], [], [])["requested_changes"] is False


def test_a_long_comment_is_truncated():
    """A reviewer pasting a log must not push the issue itself out of the model's context."""
    long_comment = [{"path": "a.py", "line": 1, "body": "x" * 9000, "user": {"login": "ana"}}]
    assert len(normalize([], long_comment, [])["inline"][0]["body"]) == 4000


def test_the_number_of_comments_is_capped():
    many = [{"path": "a.py", "line": n, "body": f"c{n}", "user": {}} for n in range(200)]
    assert len(normalize([], many, [])["inline"]) == 60


def test_the_count_covers_every_kind_of_feedback():
    feedback = normalize(REVIEWS, COMMENTS, DISCUSSION)
    assert feedback["count"] == len(feedback["inline"]) + len(feedback["reviews"]) + len(feedback["discussion"])


# --- the command surface ---------------------------------------------------


def test_pr_feedback_writes_what_the_next_run_reads(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "reviews.json").write_text(json.dumps(REVIEWS), encoding="utf-8")
    (fixtures / "comments.json").write_text(json.dumps(COMMENTS), encoding="utf-8")
    (fixtures / "issue_comments.json").write_text(json.dumps(DISCUSSION), encoding="utf-8")
    output = tmp_path / "feedback.json"

    result = CliRunner().invoke(
        pr_feedback, ["--pr=7", "--repo=o/r", f"--output={output}", f"--from-dir={fixtures}"]
    )
    assert result.exit_code == 0
    assert "requested_changes=true" in result.output
    assert json.loads(output.read_text())["inline"][0]["path"] == "src/app.py"


def test_a_pull_request_with_no_feedback_is_not_a_failure(tmp_path):
    output = tmp_path / "feedback.json"
    result = CliRunner().invoke(
        pr_feedback, ["--pr=7", "--repo=o/r", f"--output={output}", f"--from-dir={tmp_path}"]
    )
    assert result.exit_code == 0
    assert json.loads(output.read_text())["count"] == 0


def test_issue_fetch_reduces_an_issue_to_what_an_agent_needs(tmp_path):
    source = tmp_path / "raw.json"
    source.write_text(
        json.dumps(
            {
                "key": "APP-412",
                "fields": {
                    "summary": "Orders with no price fail",
                    "description": "Long description",
                    "issuetype": {"name": "Story"},
                    "components": [{"name": "orders"}],
                    "customfield_10101": "Given an item with no price\nThen the total skips it",
                },
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "issue.json"
    result = CliRunner().invoke(issue_fetch, ["--issue=APP-412", f"--output={output}", f"--from-file={source}"])

    assert result.exit_code == 0
    document = json.loads(output.read_text())
    assert document["key"] == "APP-412"
    assert document["components"] == ["orders"]
    assert document["acceptance_criteria"][0].startswith("Given an item")
