"""Reducing a pull request's review into something an agent can act on.

What this keeps, drops and truncates is the interesting behaviour: it is the boundary where a
reviewer's free text becomes input to a model that will change code.
"""

from __future__ import annotations

import json

from click.testing import CliRunner
from pipeline_exec.cli import main
from pipeline_exec.feedback import group_by_path, normalize

REVIEWS = [
    {"state": "CHANGES_REQUESTED", "body": "Use the existing retry helper.", "user": {"login": "ana"}},
    {"state": "COMMENTED", "body": "", "user": {"login": "bo"}},
]
COMMENTS = [
    {"path": "src/app.py", "line": 42, "body": "This swallows the error.", "user": {"login": "ana"}},
    {"path": "src/app.py", "line": None, "original_line": 7, "body": "Naming.", "user": {"login": "bo"}},
    {"path": "src/other.py", "line": 3, "body": "Unrelated.", "user": {"login": "ana"}},
    {"path": "src/x.py", "body": "   ", "user": {"login": "ana"}},
]
DISCUSSION = [
    {"body": "/fix APP-1", "user": {"login": "ana"}},
    {"body": "Please also update the changelog.", "user": {"login": "bo"}},
]


def test_inline_comments_keep_the_file_and_line():
    """ "This is wrong" means nothing to an agent without the location."""
    assert normalize(REVIEWS, COMMENTS, [])["inline"][0] == {
        "path": "src/app.py",
        "line": 42,
        "body": "This swallows the error.",
        "author": "ana",
    }


def test_a_comment_falls_back_to_its_original_line():
    """A comment on a line the diff has since moved still points somewhere useful."""
    assert normalize([], COMMENTS, [])["inline"][1]["line"] == 7


def test_empty_comments_are_dropped():
    feedback = normalize(REVIEWS, COMMENTS, [])
    assert all(entry["body"].strip() for entry in feedback["inline"])
    assert all(entry["body"].strip() for entry in feedback["reviews"])


def test_a_command_invocation_is_not_treated_as_feedback():
    """The comment that started the run is not a review of the work it is about to do."""
    assert [e["body"] for e in normalize([], [], DISCUSSION)["discussion"]] == [
        "Please also update the changelog."
    ]


def test_requested_changes_is_surfaced_as_a_flag():
    assert normalize(REVIEWS, [], [])["requested_changes"] is True
    assert normalize([{"state": "APPROVED", "body": "ok"}], [], [])["requested_changes"] is False


def test_a_long_comment_is_truncated():
    """A reviewer pasting a log must not push the work item out of the model's context."""
    long_comment = [{"path": "a.py", "line": 1, "body": "x" * 9000, "user": {}}]
    assert len(normalize([], long_comment, [])["inline"][0]["body"]) == 4000


def test_the_number_of_comments_is_capped():
    many = [{"path": "a.py", "line": n, "body": f"c{n}", "user": {}} for n in range(200)]
    assert len(normalize([], many, [])["inline"]) == 60


def test_the_count_covers_every_kind_of_feedback():
    feedback = normalize(REVIEWS, COMMENTS, DISCUSSION)
    assert feedback["count"] == sum(len(feedback[key]) for key in ("inline", "reviews", "discussion"))


# --- routing feedback to the right leg --------------------------------------


def test_inline_comments_group_by_the_file_they_concern():
    """A pipeline fanning out over many items needs to know which leg a comment is about."""
    grouped = group_by_path(normalize([], COMMENTS, []))
    assert set(grouped) == {"src/app.py", "src/other.py"}
    assert len(grouped["src/app.py"]) == 2


def test_grouping_an_empty_review_yields_nothing():
    assert group_by_path(normalize([], [], [])) == {}


# --- the command -----------------------------------------------------------


def run(*args):
    return CliRunner().invoke(main, ["pr-feedback", *args])


def test_the_command_writes_what_the_next_run_reads(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "reviews.json").write_text(json.dumps(REVIEWS), encoding="utf-8")
    (fixtures / "comments.json").write_text(json.dumps(COMMENTS), encoding="utf-8")
    (fixtures / "issue_comments.json").write_text(json.dumps(DISCUSSION), encoding="utf-8")
    output = tmp_path / "feedback.json"

    result = run("--pr=7", "--repo=o/r", f"--output={output}", f"--from-dir={fixtures}")
    assert result.exit_code == 0
    assert "requested_changes=true" in result.output
    assert json.loads(output.read_text())["inline"][0]["path"] == "src/app.py"


def test_by_path_grouping_is_opt_in(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "comments.json").write_text(json.dumps(COMMENTS), encoding="utf-8")
    output = tmp_path / "feedback.json"

    run("--pr=7", "--repo=o/r", f"--output={output}", f"--from-dir={fixtures}")
    assert "by_path" not in json.loads(output.read_text())

    run("--pr=7", "--repo=o/r", f"--output={output}", f"--from-dir={fixtures}", "--by-path")
    assert "src/app.py" in json.loads(output.read_text())["by_path"]


def test_a_first_run_has_no_pull_request_and_that_is_not_an_error(tmp_path):
    output = tmp_path / "feedback.json"
    result = run("--pr=", "--repo=o/r", f"--output={output}")
    assert result.exit_code == 0
    assert "no pull request yet" in result.output
    assert json.loads(output.read_text())["count"] == 0
