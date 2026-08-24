"""Reviewing a pull request, promoted out of `examples/pr-review`.

These moved with the code they cover. Three properties decide whether a review bot is one people
keep or one they mute, and each has a section below: it must not send more diff than a reviewer can
hold, it must not review the same commit twice, and when the branch does move it must revise what it
said rather than say it again underneath.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from pipeline_exec.cli import main
from pipeline_exec.reviews import (
    MAX_PATCH,
    assemble,
    commits_since,
    inline_comments,
    is_generated,
    marker_for,
    plan,
    previous_reviews,
    render_review,
    review_payload,
)

# --- from test_commands.py --------------------------------------------

PULL = {
    "number": 7,
    "title": "Skip priceless items when totalling",
    "body": "Fixes APP-412.",
    "base": {"ref": "main"},
    "head": {"sha": "abc123"},
}


def file(path, patch="@@ -1 +1 @@\n-a\n+b", status="modified"):
    return {"filename": path, "patch": patch, "status": status, "additions": 1, "deletions": 1}


def test_the_diff_carries_what_a_review_needs():
    diff = assemble([file("src/app.py")], PULL)
    assert diff["title"].startswith("Skip priceless")
    assert diff["head_sha"] == "abc123"
    assert diff["files"][0]["path"] == "src/app.py"


def test_lockfiles_are_not_reviewed():
    """Large, mechanical, and never what a review is about."""
    assert is_generated("uv.lock")
    assert is_generated(".github/workflows/aw-x.lock.yml")
    assert not is_generated("src/app.py")


def test_what_was_skipped_is_named_rather_than_dropped():
    """A reviewer must know what it did not see."""
    diff = assemble([file("uv.lock"), file("src/app.py")], PULL)
    assert [f["path"] for f in diff["files"]] == ["src/app.py"]
    assert any("uv.lock" in entry for entry in diff["not_reviewed"])
    assert diff["truncated"] is True


def test_a_binary_file_is_named_not_omitted():
    diff = assemble([{"filename": "logo.png", "status": "added"}], PULL)
    assert diff["files"] == []
    assert any("logo.png" in entry for entry in diff["not_reviewed"])


def test_an_enormous_patch_is_truncated_visibly():
    """Truncating deliberately beats truncating implicitly by running out of context."""
    diff = assemble([file("src/big.py", patch="x" * (MAX_PATCH + 5000))], PULL)
    assert "patch truncated" in diff["files"][0]["patch"]


def test_the_total_budget_is_enforced_across_files():
    files = [file(f"src/f{n}.py", patch="x" * 20_000) for n in range(20)]
    diff = assemble(files, PULL)
    assert sum(len(f["patch"]) for f in diff["files"]) <= 180_000
    assert diff["not_reviewed"]


def test_a_small_pull_request_is_not_marked_truncated():
    assert assemble([file("src/app.py")], PULL)["truncated"] is False


def test_the_command_writes_what_the_reviewer_reads(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "files.json").write_text(json.dumps([file("src/app.py")]), encoding="utf-8")
    (fixtures / "pull.json").write_text(json.dumps(PULL), encoding="utf-8")
    output = tmp_path / "diff.json"

    result = CliRunner().invoke(
        main, ["pr-diff", "--pr=7", "--repo=o/r", f"--output={output}", f"--from-dir={fixtures}"]
    )
    assert result.exit_code == 0
    assert "head_sha=abc123" in result.output
    assert json.loads(output.read_text())["files"][0]["path"] == "src/app.py"


# --- from test_state.py -----------------------------------------------

ASPECTS = [{"key": "security"}, {"key": "intent"}]
AVAILABLE = "security,intent,performance,tests"
COMMITS = [
    {"sha": "aaa11111", "commit": {"message": "Add endpoint\n\nbody"}},
    {"sha": "bbb22222", "commit": {"message": "Validate the path"}},
    {"sha": "ccc33333", "commit": {"message": "Add a test"}},
]


def review(aspect, sha, *, id=1, body_extra=""):
    return {"id": id, "body": marker_for(aspect, sha) + "\n## Review\n" + body_extra}


# --- finding what was said before -------------------------------------------


def test_the_bots_own_reviews_are_found_by_their_marker():
    found = previous_reviews([review("security", "aaa11111"), {"id": 9, "body": "a human comment"}])
    assert set(found) == {"security"}
    assert found["security"]["sha"] == "aaa11111"


def test_the_latest_review_for_an_aspect_wins():
    """A review revised several times leaves several entries; only the last describes now."""
    found = previous_reviews([review("security", "aaa11111", id=1), review("security", "ccc33333", id=2)])
    assert found["security"]["id"] == 2
    assert found["security"]["sha"] == "ccc33333"


def test_a_human_review_is_never_mistaken_for_the_bots():
    assert previous_reviews([{"id": 4, "body": "Looks good to me"}]) == {}


# --- deciding what still needs reviewing ------------------------------------


def test_an_unchanged_pull_request_is_not_reviewed_again():
    """A second review saying the same thing buries the human conversation."""
    pending, skipped = plan(ASPECTS, [review("security", "ccc33333")], "ccc33333", COMMITS)
    assert [item["key"] for item in pending] == ["intent"]
    assert [entry["key"] for entry in skipped] == ["security"]
    assert "has not moved" in skipped[0]["reason"]


def test_a_moved_pull_request_is_reviewed_again():
    pending, _ = plan(ASPECTS, [review("security", "aaa11111")], "ccc33333", COMMITS)
    assert [item["key"] for item in pending] == ["security", "intent"]


def test_an_aspect_never_reviewed_is_not_a_revision():
    pending, _ = plan(ASPECTS, [], "ccc33333", COMMITS)
    assert all(item["revision"] is False for item in pending)


def test_a_revision_carries_what_was_said_and_what_moved():
    """This is what a revision is actually about: what these commits changed about the conclusion."""
    pending, _ = plan(ASPECTS, [review("security", "aaa11111", id=7)], "ccc33333", COMMITS)
    security = next(item for item in pending if item["key"] == "security")
    assert security["revision"] is True
    assert security["previous_review_id"] == 7
    assert "## Review" in security["previous_review"]
    assert [c["sha"] for c in security["new_commits"]] == ["bbb22222", "ccc33333"]


def test_force_reviews_again_even_when_nothing_moved():
    pending, skipped = plan(ASPECTS, [review("security", "ccc33333")], "ccc33333", COMMITS, force=True)
    assert [item["key"] for item in pending] == ["security", "intent"]
    assert skipped == []


def test_reviewing_everything_when_nothing_has_ever_been_reviewed():
    pending, skipped = plan(ASPECTS, [], "ccc33333", COMMITS)
    assert len(pending) == 2
    assert skipped == []


# --- what moved -------------------------------------------------------------


def test_commits_since_lists_only_what_came_after():
    assert [c["sha"] for c in commits_since(COMMITS, "aaa11111")] == ["bbb22222", "ccc33333"]


def test_commits_since_the_head_is_empty():
    assert commits_since(COMMITS, "ccc33333") == []


def test_a_force_push_that_erased_the_reviewed_commit_reviews_everything():
    """Pretending nothing changed because the history was rewritten is the wrong answer."""
    assert len(commits_since(COMMITS, "deadbeef")) == len(COMMITS)


def test_only_the_first_line_of_a_commit_message_is_carried():
    assert commits_since(COMMITS, "deadbeef")[0]["message"] == "Add endpoint"


# --- the command ------------------------------------------------------------


@pytest.fixture
def fixtures(tmp_path):
    directory = tmp_path / "fixtures"
    directory.mkdir()
    (directory / "commits.json").write_text(json.dumps(COMMITS), encoding="utf-8")
    return directory


def run(output_dir, fixtures, *extra, requested=""):
    return CliRunner().invoke(
        main,
        [
            "review-state",
            "--pr=7",
            "--repo=o/r",
            f"--requested={requested}",
            f"--available={AVAILABLE}",
            f"--output-dir={output_dir}",
            f"--from-dir={fixtures}",
            "--head=ccc33333",
            *extra,
        ],
    )


def pending_keys(result):
    """The JSON array the reviewing jobs gate on."""
    line = next(x for x in result.output.splitlines() if x.startswith("pending="))
    return json.loads(line.split("=", 1)[1])


# --- resolving what the comment asked for -----------------------------------


def test_the_words_after_the_command_choose_the_reviews(tmp_path, fixtures):
    result = run(tmp_path / "pending", fixtures, requested="security intent")
    assert result.exit_code == 0
    assert pending_keys(result) == ["security", "intent"]


def test_an_empty_request_reviews_everything(tmp_path, fixtures):
    """`/review` with no arguments is a reasonable thing to type."""
    result = run(tmp_path / "pending", fixtures)
    assert sorted(pending_keys(result)) == ["intent", "performance", "security", "tests"]


def test_an_aspect_nobody_reviews_fails_loudly(tmp_path, fixtures):
    """A model asked for a banana review will produce one, and it will look plausible."""
    result = run(tmp_path / "pending", fixtures, requested="security banana")
    assert result.exit_code == 1
    assert "banana" in result.output
    assert "available" in result.output


def test_the_same_aspect_twice_is_one_review(tmp_path, fixtures):
    result = run(tmp_path / "pending", fixtures, requested="security security")
    assert pending_keys(result) == ["security"]


def test_the_gates_json_array_is_accepted(tmp_path, fixtures):
    result = run(tmp_path / "pending", fixtures, requested='["security", "tests"]')
    assert pending_keys(result) == ["security", "tests"]


# --- what each reviewing job reads ------------------------------------------


def test_the_command_writes_only_what_still_needs_reviewing(tmp_path, fixtures):
    (fixtures / "reviews.json").write_text(json.dumps([review("security", "ccc33333")]), encoding="utf-8")
    output_dir = tmp_path / "pending"

    result = run(output_dir, fixtures, requested="security intent")
    assert result.exit_code == 0
    assert pending_keys(result) == ["intent"]
    # The job for an aspect that is not pending never starts, so it never looks for the file.
    assert (output_dir / "intent.json").is_file()
    assert not (output_dir / "security.json").exists()


def test_nothing_to_review_gates_every_reviewer_off(tmp_path, fixtures):
    (fixtures / "reviews.json").write_text(
        json.dumps([review("security", "ccc33333", id=1), review("intent", "ccc33333", id=2)]),
        encoding="utf-8",
    )
    result = run(tmp_path / "pending", fixtures, requested="security intent")
    assert result.exit_code == 0
    assert pending_keys(result) == []


def test_every_pending_file_carries_the_commit_being_reviewed(tmp_path, fixtures):
    output_dir = tmp_path / "pending"
    run(output_dir, fixtures, requested="security intent")
    for path in output_dir.glob("*.json"):
        assert json.loads(path.read_text())["head_sha"] == "ccc33333"


def test_a_revision_file_carries_what_was_said_and_what_moved(tmp_path, fixtures):
    (fixtures / "reviews.json").write_text(
        json.dumps([review("security", "aaa11111", id=9)]), encoding="utf-8"
    )
    output_dir = tmp_path / "pending"
    run(output_dir, fixtures, requested="security")
    item = json.loads((output_dir / "security.json").read_text())
    assert item["revision"] is True
    assert item["previous_review_id"] == 9
    assert [commit["sha"] for commit in item["new_commits"]] == ["bbb22222", "ccc33333"]


# --- publishing -------------------------------------------------------------
#
# This was a shell script driving `jq`. It is code here because a shipped pipeline carries no
# scripts, and rendering is separated from posting so the half where mistakes live can be tested
# without a network.


FINDINGS = {
    "title": "Security",
    "summary": "One way in.",
    "findings": [
        {"path": "src/files.py", "line": 2, "comment": "`name` reaches open() unvalidated"},
        {"path": "src/app.py", "comment": "no line for this one"},
    ],
}


def test_the_marker_carries_the_commit_so_the_next_run_can_find_this_review():
    body = render_review("security", FINDINGS, sha="abc123")
    assert body.startswith(marker_for("security", "abc123"))
    assert previous_reviews([{"id": 1, "body": body}])["security"]["sha"] == "abc123"


def test_a_review_with_no_findings_still_says_something():
    assert "No findings." in render_review("tests", {}, sha="abc123")


def test_every_finding_reaches_the_body_even_without_a_line():
    body = render_review("security", FINDINGS, sha="abc123")
    assert "src/files.py:2" in body
    assert "src/app.py" in body


def test_only_anchored_findings_become_inline_comments():
    """A finding with no line is already in the body; sending it unanchored fails the whole post."""
    comments = inline_comments(FINDINGS)
    assert [c["path"] for c in comments] == ["src/files.py"]
    assert comments[0]["line"] == 2 and comments[0]["side"] == "RIGHT"


def test_a_first_review_is_posted_with_its_inline_comments():
    action, payload = review_payload("security", FINDINGS, sha="abc123")
    assert action == "post"
    assert payload["event"] == "COMMENT"
    assert payload["commit_id"] == "abc123"
    assert len(payload["comments"]) == 1


def test_a_revision_updates_the_body_and_sends_no_comments():
    """A submitted review's body can be updated; its inline comments cannot."""
    action, payload = review_payload("security", FINDINGS, sha="def456", previous_id="9001")
    assert action == "revise"
    assert set(payload) == {"body"}


def test_posting_nothing_when_no_aspect_needed_reviewing(tmp_path):
    empty = tmp_path / "reviews"
    empty.mkdir()
    result = CliRunner().invoke(main, ["post-reviews", "--pr=7", f"--reviews={empty}"])
    assert result.exit_code == 0
    assert "nothing to post" in result.output


def test_posting_with_no_pull_request_is_not_a_failure(tmp_path):
    result = CliRunner().invoke(main, ["post-reviews", "--pr=", f"--reviews={tmp_path}"])
    assert result.exit_code == 0
    assert "no pull request" in result.output


def test_a_reviewer_that_answered_with_junk_does_not_take_the_others_down(tmp_path):
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    (reviews / "security.json").write_text(json.dumps(FINDINGS))
    (reviews / "tests.json").write_text("{not json")
    result = CliRunner().invoke(main, ["post-reviews", "--pr=7", f"--reviews={reviews}", "--dry-run"])
    assert result.exit_code == 0
    assert "not valid JSON" in result.output
    assert "posted or revised 1 review(s)" in result.output


def test_which_review_to_revise_comes_from_the_pipeline_not_the_agent(tmp_path):
    """An agent that forgot to echo it would post beside its own earlier review."""
    reviews, pending = tmp_path / "reviews", tmp_path / "pending"
    reviews.mkdir(), pending.mkdir()
    (reviews / "security.json").write_text(json.dumps(FINDINGS))
    (pending / "security.json").write_text(json.dumps({"key": "security", "previous_review_id": 9001}))
    result = CliRunner().invoke(
        main, ["post-reviews", "--pr=7", f"--reviews={reviews}", f"--pending={pending}", "--dry-run"]
    )
    assert "(revise)" in result.output
